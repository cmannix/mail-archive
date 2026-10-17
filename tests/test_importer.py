import re
import subprocess
import zipfile

import pytest

from mail_archive.importer import Archive, run


def message(msgid, subject, labels=None):
    text = f"From: a@example.com\nTo: me@example.com\nSubject: {subject}\n"
    if msgid is not None:
        text += f"Message-ID: {msgid}\n"
    if labels:
        text += f"X-Gmail-Labels: {labels}\n"
    return text + f"\nBody of {subject}\nFrom the start of a line\n>From a quoted one\n"


def write_mbox(path, messages):
    """Writes mboxrd: each body line matching >*From gains one more ">"."""
    with open(path, "w") as f:
        for m in messages:
            f.write("From sender@example.com Thu Jan  1 00:00:00 2026\n")
            f.write(re.sub(r"^(>*From )", r">\1", m, flags=re.MULTILINE))
            f.write("\n")


@pytest.fixture
def archive(tmp_path, monkeypatch):
    root = tmp_path / "root"
    for source in ("personal", "work"):
        (root / "incoming" / source).mkdir(parents=True)
    config = tmp_path / "notmuch-config"
    config.write_text(
        f"[database]\npath={root / 'maildir'}\n[new]\ntags=\nignore=\n[maildir]\nsynchronize_flags=false\n"
    )
    monkeypatch.setenv("NOTMUCH_CONFIG", str(config))
    return Archive(root)


def notmuch(*args):
    return subprocess.run(["notmuch", *args], check=True, capture_output=True, text=True).stdout.split("\n")[:-1]


def tags(query):
    return sorted(notmuch("search", "--output=tags", query))


def zipped(archive, name, messages):
    mbox = archive.root / "export.mbox"
    write_mbox(mbox, messages)
    with zipfile.ZipFile(archive.incoming / "personal" / name, "w") as z:
        z.write(mbox, "export/Mail/all.mbox")
    mbox.unlink()


def test_imports_once_across_sources_and_tags_both(archive):
    zipped(archive, "export.zip", [message("<one@x>", "one", "Inbox,Category Updates")])
    write_mbox(archive.incoming / "work" / "work.mbox", [message("<one@x>", "one again"), message("<two@x>", "two")])

    assert run(archive)

    assert notmuch("count", "*") == ["2"]
    assert tags("id:one@x") == ["gmail/category-updates", "gmail/inbox", "source/personal", "source/work"]
    assert tags("id:two@x") == ["source/work"]
    assert (archive.done / "work" / "work.mbox").exists()
    assert (archive.done / "personal" / "export.zip").exists()


def test_stores_messages_unchanged(archive):
    original = message("<one@x>", "one")
    write_mbox(archive.incoming / "work" / "work.mbox", [original])

    run(archive)

    (stored,) = (archive.maildir / "cur").iterdir()
    assert stored.read_bytes() == original.encode()


def test_tags_messages_without_or_with_odd_message_ids(archive):
    zipped(archive, "export.zip", [message(None, "no id", "Opened"), message('<we"ird %20 id@x>', "weird", "Important")])

    run(archive)

    assert tags('subject:"no id"') == ["gmail/opened", "source/personal"]
    assert tags("subject:weird") == ["gmail/important", "source/personal"]


def test_a_later_export_adds_only_new_mail(archive):
    write_mbox(archive.incoming / "work" / "a.mbox", [message("<one@x>", "one")])
    run(archive)
    write_mbox(archive.incoming / "work" / "b.mbox", [message("<one@x>", "one"), message("<two@x>", "two")])

    run(archive)

    assert notmuch("count", "*") == ["2"]


def test_unreadable_and_hidden_files(archive):
    (archive.incoming / "work" / "notes.txt").write_text("x")
    (archive.incoming / "work" / "bad.zip").write_text("not a zip")
    (archive.incoming / "work" / ".upload").write_text("still copying")

    assert not run(archive)

    assert (archive.failed / "work" / "notes.txt").exists()
    assert (archive.failed / "work" / "bad.zip").exists()
    assert (archive.incoming / "work" / ".upload").exists()


def test_tags_are_rebuilt_from_sources_after_losing_the_index(archive):
    zipped(archive, "export.zip", [message("<one@x>", "one", "Inbox")])
    run(archive)
    subprocess.run(["rm", "-rf", str(archive.maildir / ".notmuch")], check=True)

    run(archive)

    assert tags("id:one@x") == ["gmail/inbox", "source/personal"]
