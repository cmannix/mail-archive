"""Imports mail exports dropped in <root>/incoming/<source>/ into the archive.

Accepts .mbox files and .zip files containing them.
Each message is stored unchanged in maildir/cur/, named after a hash of its
Message-ID, so a message already in the archive from any source is skipped.
Every message seen is recorded in sources.jsonl with its source and Gmail
labels; tags are rebuilt from that file, never stored only in the index.

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


def gmail_labels(msg):
    value = msg.get("X-Gmail-Labels", "")
    return [label.strip() for label in value.split(",") if label.strip()]


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
        log.write(json.dumps({"key": name, "msgid": msgid, "source": source, "labels": gmail_labels(msg)}) + "\n")
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
    return "".join(c if c.isalnum() or c in "-_/." else "-" for c in text.lower())


def notmuch_query(archive, record):
    if record["msgid"]:
        # notmuch drops whitespace from Message-IDs; a quote is doubled to
        # escape it inside the quoted term.
        msgid = "".join(record["msgid"].split()).replace('"', '""')
        return f'id:"{msgid}"'
    # notmuch's own id for a message without a Message-ID.
    raw = archive.message_path(record["key"]).read_bytes()
    return f"id:notmuch-sha1-{hashlib.sha1(raw).hexdigest()}"


def retag(archive):
    """Adds a tag for each source and Gmail label recorded in sources.jsonl."""
    tags = {}
    with archive.sources.open() as log:
        for line in log:
            record = json.loads(line)
            entry = tags.setdefault(notmuch_query(archive, record), set())
            entry.add(f"source/{tag_name(record['source'])}")
            entry.update(f"gmail/{tag_name(label)}" for label in record["labels"])
    batch = "".join(f"{' '.join('+' + t for t in sorted(ts))} -- {q}\n" for q, ts in tags.items())
    subprocess.run(["notmuch", "tag", "--batch"], input=batch, text=True, check=True)


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
    if archive.sources.exists():
        retag(archive)
    return ok


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.exit("usage: mail-archive-import <archive root>")
    sys.exit(0 if run(Archive(argv[0])) else 1)
