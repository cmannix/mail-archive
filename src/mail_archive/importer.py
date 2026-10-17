"""Imports mail exports dropped in <root>/incoming/<source>/ into the archive.

Accepts .mbox files and .zip files containing them.
Each message is stored unchanged in maildir/cur/, named after a hash of its
Message-ID, so a message already in the archive from any source is skipped.
Every message seen is recorded in sources.jsonl with its source and the
labels its export carried; tags are rebuilt from that file, never stored only in the index.

notmuch must be on PATH, and NOTMUCH_CONFIG must point at a configuration whose
database path is <root>/maildir.
"""

import hashlib
import json
import mailbox
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from pathlib import Path

_headers = BytesHeaderParser()

# mbox files escape a body line starting "From " as ">From ", and an already
# escaped one with one more ">" (the mboxrd convention).
_escaped_from = re.compile(rb"^>(>*From )", re.MULTILINE)


class Archive:
    def __init__(self, root):
        self.root = Path(root)
        self.incoming = self.root / "incoming"
        self.done = self.root / "imported"
        self.failed = self.root / "failed"
        self.maildir = self.root / "maildir"
        self.sources = self.root / "sources.jsonl"
        self.tmp = self.root / "tmp"

    def message_path(self, key):
        return self.maildir / "cur" / f"{key}:2,S"


def message_key(raw, msgid):
    """A stable file name: from the Message-ID, or the contents if it has none."""
    basis = msgid.encode() if msgid else raw
    return hashlib.sha256(basis).hexdigest()[:40]


# Headers in which an export carries each message's labels, as comma-separated
# names.
LABEL_HEADERS = ["X-Gmail-Labels"]


def labels(msg):
    found = []
    for header in LABEL_HEADERS:
        found += [label.strip() for label in msg.get(header, "").split(",") if label.strip()]
    return found


def import_mbox(archive, path, source, log):
    """Stores each new message from one mbox file. Returns (added, seen)."""
    added = seen = 0
    box = mailbox.mbox(path, create=False)
    for key in box.iterkeys():
        raw = _escaped_from.sub(rb"\1", box.get_bytes(key))
        msg = _headers.parsebytes(raw)
        msgid = (msg.get("Message-ID") or "").strip().strip("<>")
        name = message_key(raw, msgid)
        target = archive.message_path(name)
        if not target.exists():
            tmp = archive.maildir / "tmp" / name
            tmp.write_bytes(raw)
            os.replace(tmp, target)
            added += 1
        seen += 1
        log.write(json.dumps({"key": name, "msgid": msgid, "source": source, "labels": labels(msg)}) + "\n")
        # Saved with the message, so a run that stops part-way leaves no stored
        # message without its record.
        log.flush()
    return added, seen


def import_file(archive, path, source, log):
    if path.suffix == ".mbox":
        return import_mbox(archive, path, source, log)
    if path.suffix != ".zip":
        raise ValueError("not an .mbox or .zip")
    added = seen = 0
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory(dir=archive.tmp) as work:
        for member in z.namelist():
            if member.endswith(".mbox"):
                extracted = z.extract(member, work)
                a, s = import_mbox(archive, extracted, source, log)
                added, seen = added + a, seen + s
                os.remove(extracted)
    if seen == 0:
        raise ValueError("no .mbox inside")
    return added, seen


def tag_name(text):
    """A tag made only of characters notmuch's --batch input leaves as they are."""
    return "".join(c if c.isalnum() or c in "-_/.@+" else "-" for c in text.lower())


def read_headers(path):
    """The header block of a stored message, without reading its body."""
    head = b""
    with open(path, "rb") as f:
        while b"\n\n" not in head and b"\r\n\r\n" not in head:
            chunk = f.read(16384)
            if not chunk:
                break
            head += chunk
    return _headers.parsebytes(head)


def delivered_to(archive, key):
    """Every address a mail system recorded delivering the message to. Each hop
    adds its own Delivered-To header, so a forwarded message has several."""
    msg = read_headers(archive.message_path(key))
    return {addr.lower() for _, addr in getaddresses(msg.get_all("Delivered-To", [])) if addr}


def notmuch_ids(archive):
    """notmuch's id for each stored file, by file name key. Asking notmuch,
    rather than deriving ids from headers, covers Message-IDs it parses in its
    own way and messages that have none."""
    out = subprocess.run(
        ["notmuch", "show", "--format=json", "--body=false", "--entire-thread=false", "*"],
        check=True, capture_output=True, text=True,
    ).stdout
    ids = {}

    def walk(node):
        if isinstance(node, dict) and "id" in node:
            files = node.get("filename", [])
            for f in files if isinstance(files, list) else [files]:
                ids[Path(f).name.split(":")[0]] = node["id"]
        if isinstance(node, (list, dict)):
            for child in node.values() if isinstance(node, dict) else node:
                walk(child)

    walk(json.loads(out or "[]"))
    return ids


def add_missing_records(archive):
    """Records any stored message that has no line in sources.jsonl, with source
    "unknown", and returns how many there were."""
    known = set()
    if archive.sources.exists():
        with archive.sources.open() as log:
            known = {json.loads(line)["key"] for line in log}
    missing = sorted(p for p in (archive.maildir / "cur").iterdir() if p.name.split(":")[0] not in known)
    with archive.sources.open("a") as log:
        for path in missing:
            msg = read_headers(path)
            msgid = (msg.get("Message-ID") or "").strip().strip("<>")
            log.write(json.dumps({"key": path.name.split(":")[0], "msgid": msgid, "source": "unknown", "labels": labels(msg)}) + "\n")
    return len(missing)


def retag(archive):
    """Adds a tag for each source and label recorded in sources.jsonl, and for
    each address in the stored message's Delivered-To headers."""
    ids = notmuch_ids(archive)
    tags, unindexed = {}, set()
    with archive.sources.open() as log:
        for line in log:
            record = json.loads(line)
            nid = ids.get(record["key"])
            if nid is None:
                unindexed.add(record["key"])
                continue
            query = 'id:"' + nid.replace('"', '""') + '"'
            if query not in tags:
                tags[query] = {f"delivered/{tag_name(a)}" for a in delivered_to(archive, record["key"])}
            tags[query].add(f"source/{tag_name(record['source'])}")
            tags[query].update(f"label/{tag_name(label)}" for label in record["labels"])
    batch = "".join(f"{' '.join('+' + t for t in sorted(ts))} -- {q}\n" for q, ts in tags.items())
    subprocess.run(["notmuch", "tag", "--batch"], input=batch, text=True, check=True)
    if unindexed:
        print(f"{len(unindexed)} stored files are not in the index (notmuch did not read them as mail)")


def run(archive):
    """Imports everything waiting in incoming/. Returns False if any file failed."""
    for d in (archive.done, archive.failed, archive.tmp, *(archive.maildir / s for s in ("cur", "new", "tmp"))):
        d.mkdir(parents=True, exist_ok=True)
    ok = True
    with archive.sources.open("a") as log:
        for source_dir in sorted(p for p in archive.incoming.iterdir() if p.is_dir()):
            source = source_dir.name
            # Hidden files are copies still in progress.
            for path in sorted(p for p in source_dir.iterdir() if p.is_file() and not p.name.startswith(".")):
                dest = archive.done
                try:
                    added, seen = import_file(archive, path, source, log)
                    log.flush()
                    print(f"{source}/{path.name}: {seen} messages, {added} new")
                except (OSError, ValueError, zipfile.BadZipFile, mailbox.Error) as e:
                    print(f"{source}/{path.name}: failed ({type(e).__name__}: {e})")
                    dest, ok = archive.failed, False
                (dest / source).mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), dest / source / path.name)
    subprocess.run(["notmuch", "new", "--quiet"], check=True)
    missing = add_missing_records(archive)
    if missing:
        print(f"{missing} stored messages had no record; added with source 'unknown'")
        ok = False
    if archive.sources.exists():
        retag(archive)
    return ok


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: mail-archive-import <archive root>")
    sys.exit(0 if run(Archive(argv[0])) else 1)
