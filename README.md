# mail-archive

A provider-independent archive of email: every message kept exactly as received,
one file per message in Maildir, indexed and tagged with
[notmuch](https://notmuchmail.org). The archive is the record; tags are derived
from it and can be rebuilt at any time.

## Importing

`mail-archive-import <root>` imports every export waiting in
`<root>/incoming/<source>/`, where `<source>` names the account it came from:

- `.mbox` files, and `.zip` files containing them.
- Each message is stored in `<root>/maildir/cur/`, named after a hash of its
  Message-ID (or of its contents, if it has none). A message already in the
  archive, from any source, is not stored again, so a newer export of an account
  can be imported over an older one.
- mbox escaping is undone (mboxrd: one `>` is removed from body lines matching
  `>*From `), so the stored message is the one that was sent. An export written
  as plain mbox (mboxo) loses one `>` from body lines that began `>From `.
- Every message seen is appended to `<root>/sources.jsonl`, with its source and
  Gmail labels, from the `X-Gmail-Labels` header where an export includes it.
- Tags are rebuilt from `sources.jsonl` after each import: `source/<source>` for
  every source a message was seen in, and `gmail/<label>` for its labels.
- Imported files move to `<root>/imported/<source>/`; files that could not be read
  move to `<root>/failed/<source>/` and make the command exit 1. Hidden files are
  treated as copies in progress and left alone.

notmuch must be on `PATH` (the Nix package wraps it in), and `NOTMUCH_CONFIG` must
name a configuration whose database path is `<root>/maildir`, with `synchronize_flags`
off and no tags applied by `notmuch new`.

## Development

`nix develop` gives Python, pytest and notmuch; run `pytest`. `nix flake check`
builds the package, which runs the tests.
