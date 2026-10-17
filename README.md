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
  labels, where the export carries them in a header (see `LABEL_HEADERS`).
- Tags are rebuilt from `sources.jsonl` after each import: `source/<source>` for
  every source a message was seen in, `label/<label>` for its labels, and
  `delivered/<address>` for each address in its `Delivered-To` headers (a forwarded
  message carries one per hop, so this shows where it first arrived).
- Imported files move to `<root>/imported/<source>/`; files that could not be read
  move to `<root>/failed/<source>/` and make the command exit 1. Hidden files are
  treated as copies in progress and left alone.

notmuch must be on `PATH` (the Nix package wraps it in), and `NOTMUCH_CONFIG` must
name a configuration whose database path is `<root>/maildir`, with `synchronize_flags`
off and no tags applied by `notmuch new`.

## Sender report

`mail-archive-report <root> --me <address> [--me <address> ...] [--output file.csv]`
writes one CSV row per sender, built from message headers only: no subjects or
bodies are copied into it, so it can be read without reading the mail.

| Column | Meaning |
|---|---|
| `sender`, `name` | From address, and its most common display name |
| `messages`, `last_12_months`, `previous_12_months` | How much they send, and the trend |
| `first`, `last` | Dates of the oldest and newest message |
| `bulk_share` | Share sent as bulk or list mail (`List-Unsubscribe`, `List-Id`, `Precedence`, `Auto-Submitted`) |
| `noreply` | The address looks automated (no-reply, notifications, alerts) |
| `attachment_share` | Share sent as `multipart/mixed`, which usually means attachments |
| `you_wrote_to` | Messages from a `--me` address to them: the clearest sign of a real contact |
| `delivered_to` | Addresses in their messages' `Delivered-To` headers |
| `kind`, `kinds` | Subject patterns: `receipt`, `security`, `delivery`, `account`, counted; `kind` is the main one when it covers a third of their messages |
| `category` | A suggested category: `personal`, `alerts` (codes, sign-in and security notices), `records` (receipts, orders, statements, deliveries), `notices` (account and policy changes, other automated mail), `newsletters` (bulk), `inactive` (nothing in 12 months) or `review` (no clear signal) |
| `short_lived` | Only useful for a short time (codes, alerts, delivery notices) |
| `drop_candidate` | Frequent bulk mail the owner has never written to |
| `reason` | Why the category was suggested |

Messages from a `--me` address are counted as written by the owner, not as a
sender. Rows are ordered by mail in the last 12 months.

## Development

`nix develop` gives Python, pytest and notmuch; run `pytest`. `nix flake check`
builds the package, which runs the tests.
