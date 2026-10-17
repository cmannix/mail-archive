"""A per-sender summary of the archive, built from message headers only.

One row per sender address, with how much they send, when, signs of bulk or
automated mail, the kinds of subject they use, whether the archive's owner has
ever written to them, and a suggested place for their mail. Subjects and bodies
are not copied into the report: it is meant to be read, by a person or a tool,
without reading the mail itself.
"""

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from mail_archive.importer import read_headers

KINDS = {
    "receipt": r"receipt|order|invoice|payment|paid|statement|bill\b|refund|booking|confirmation|purchase|transaction|application|mortgage|renewal|\btax\b",
    "security": r"\bcode\b|verif|password|sign.?in|log.?in|security|new device|two.factor|2fa",
    "delivery": r"deliver|dispatch|shipped|shipping|tracking|out for|parcel|collect",
    "account": r"terms|privacy|policy|update to your|changes to|your account",
    "monitoring": r"\bdown\b|fail|error|outage|incident|unreachable",
}
def spam_score(msg):
    """The spam filter's score, where the message carries one: X-Spam-Score as a
    number, or X-Spamd-Bar, whose "+" and "-" marks each count one point."""
    value = msg.get("X-Spam-Score")
    if value:
        try:
            return float(value.strip().split()[0])
        except ValueError:
            pass
    bar = msg.get("X-Spamd-Bar")
    if bar is not None:
        return float(bar.count("+") - bar.count("-"))
    return None


NOREPLY = re.compile(r"no.?reply|do.?not.?reply|notifications?@|mailer|alerts?@|automated", re.I)

COLUMNS = [
    "sender", "name", "messages", "last_12_months", "previous_12_months", "first", "last",
    "bulk_share", "spam_score", "noreply", "attachment_share", "you_wrote_to",
    "original_to", "forwarded_for", "delivered_to",
    "kind", "kinds", "category", "short_lived", "drop_candidate", "reason",
]


def text(value):
    """A header value as plain text, decoding any RFC 2047 encoded words."""
    try:
        return str(make_header(decode_header(value or "")))
    except (ValueError, LookupError):
        return value or ""


def date_of(msg):
    try:
        d = parsedate_to_datetime(msg.get("Date", ""))
    except (TypeError, ValueError, IndexError):
        return None
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def is_bulk(msg):
    precedence = (msg.get("Precedence") or "").lower()
    auto = (msg.get("Auto-Submitted") or "no").lower()
    return bool(msg.get("List-Unsubscribe") or msg.get("List-Id") or precedence in ("bulk", "list", "junk") or auto != "no")


def kinds_of(subject):
    return [k for k, pattern in KINDS.items() if re.search(pattern, subject, re.I)]


def suggest(row):
    """A suggested category for a sender's mail, whether it is only useful for a
    short time, whether it is a candidate to stop receiving, and the reason.

    Categories: personal, alerts (codes, sign-in, security and monitoring),
    records (receipts, orders, statements, deliveries), notices (account and
    policy changes, other automated mail), newsletters (bulk mail), inactive
    (nothing in the last 12 months) and review (no clear signal).
    """
    engaged = row["you_wrote_to"] > 0
    if engaged and row["bulk_share"] < 0.5:
        return "personal", False, False, "the owner has written to them"
    kind = row["kind"]
    if kind == "security":
        return "alerts", True, False, "codes and sign-in alerts"
    if kind == "monitoring":
        return "alerts", False, False, "failure and outage alerts"
    if kind == "receipt":
        return "records", False, False, "receipts, orders and statements"
    if kind == "delivery":
        return "records", True, False, "delivery notices"
    if row["last_12_months"] == 0:
        return "inactive", False, False, "nothing in the last 12 months"
    if row["bulk_share"] >= 0.5:
        if engaged:
            return "newsletters", False, False, "bulk mail from a sender the owner has written to"
        frequent = row["last_12_months"] >= 12
        return "newsletters", False, frequent, "frequent bulk mail, never written to" if frequent else "bulk mail"
    if kind == "account":
        return "notices", False, False, "account and policy notices"
    if row["noreply"]:
        return "notices", False, False, "automated mail"
    return "review", False, False, "no clear signal"


def build(maildir, me, now=None):
    now = now or datetime.now(timezone.utc)
    year_ago, two_years_ago = now - timedelta(days=365), now - timedelta(days=730)
    me = {a.lower() for a in me}
    senders = defaultdict(lambda: {"names": Counter(), "dates": [], "bulk": 0, "attachments": 0, "kinds": Counter(), "delivered": Counter(), "original": Counter(), "forwarded": Counter(), "spam": []})
    wrote_to = Counter()

    for path in sorted(Path(maildir, "cur").iterdir()):
        msg = read_headers(path)
        from_ = getaddresses([text(msg.get("From", ""))])
        if not from_ or not from_[0][1]:
            continue
        name, address = from_[0][0], from_[0][1].lower()
        if address in me:
            for _, to in getaddresses([text(msg.get(h, "")) for h in ("To", "Cc")]):
                if to and to.lower() not in me:
                    wrote_to[to.lower()] += 1
            continue
        s = senders[address]
        if name:
            s["names"][name] += 1
        date = date_of(msg)
        if date:
            s["dates"].append(date)
        s["bulk"] += is_bulk(msg)
        s["attachments"] += (msg.get_content_type() == "multipart/mixed")
        s["kinds"].update(kinds_of(text(msg.get("Subject", ""))))
        s["delivered"].update(a.lower() for _, a in getaddresses(msg.get_all("Delivered-To", [])) if a)
        s["original"].update(a.lower() for _, a in getaddresses(msg.get_all("X-Original-To", [])) if a)
        # X-Forwarded-For lists the forwarding account first, then the target.
        s["forwarded"].update(a.split()[0].lower() for a in msg.get_all("X-Forwarded-For", []) if a.split())
        score = spam_score(msg)
        if score is not None:
            s["spam"].append(score)
        s["count"] = s.get("count", 0) + 1

    rows = []
    for address, s in senders.items():
        dates = sorted(s["dates"])
        kind, hits = (s["kinds"].most_common(1) or [("", 0)])[0]
        row = {
            "sender": address,
            "name": s["names"].most_common(1)[0][0] if s["names"] else "",
            "messages": s["count"],
            "last_12_months": sum(d >= year_ago for d in dates),
            "previous_12_months": sum(two_years_ago <= d < year_ago for d in dates),
            "first": dates[0].date().isoformat() if dates else "",
            "last": dates[-1].date().isoformat() if dates else "",
            "bulk_share": round(s["bulk"] / s["count"], 2),
            "noreply": bool(NOREPLY.search(address)),
            "attachment_share": round(s["attachments"] / s["count"], 2),
            "you_wrote_to": wrote_to[address],
            "spam_score": round(sum(s["spam"]) / len(s["spam"]), 1) if s["spam"] else "",
            "original_to": " ".join(a for a, _ in s["original"].most_common()),
            "forwarded_for": " ".join(a for a, _ in s["forwarded"].most_common()),
            "delivered_to": " ".join(a for a, _ in s["delivered"].most_common()),
            # A kind counts when at least a third of the sender's subjects show it.
            "kind": kind if hits * 3 >= s["count"] else "",
            "kinds": " ".join(f"{k}:{n}" for k, n in s["kinds"].most_common()),
        }
        row["category"], row["short_lived"], row["drop_candidate"], row["reason"] = suggest(row)
        rows.append(row)
    rows.sort(key=lambda r: (-r["last_12_months"], -r["messages"], r["sender"]))
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", help="the archive root (holding maildir/)")
    parser.add_argument("--me", action="append", default=[], help="an address of the archive's owner; repeat for each")
    parser.add_argument("--output", help="write the CSV here instead of standard output")
    args = parser.parse_args(argv)
    rows = build(Path(args.root, "maildir"), args.me)
    out = open(args.output, "w", newline="") if args.output else sys.stdout
    writer = csv.DictWriter(out, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    if args.output:
        out.close()
