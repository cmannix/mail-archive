import csv
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from mail_archive.report import build, main

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


@pytest.fixture
def maildir(tmp_path):
    for d in ("cur", "new", "tmp"):
        (tmp_path / "maildir" / d).mkdir(parents=True)
    return tmp_path / "maildir"


def add(maildir, n, sender, subject, days_ago=10, to="me@example.com", extra=""):
    date = format_datetime(NOW - timedelta(days=days_ago))
    path = maildir / "cur" / f"m{n}:2,S"
    path.write_text(f"From: {sender}\nTo: {to}\nDate: {date}\nSubject: {subject}\n{extra}\nbody\n")


def by_sender(rows):
    return {r["sender"]: r for r in rows}


def test_suggestions(maildir):
    n = 0
    for i in range(15):
        n += 1
        add(maildir, n, "News <news@shop.example>", f"Sale {i}", extra="List-Unsubscribe: <mailto:u@shop.example>\n")
    for i in range(3):
        n += 1
        add(maildir, n, "Shop <orders@shop.example>", f"Your order {i} is confirmed")
    n += 1
    add(maildir, n, "Bank <no-reply@bank.example>", "Your one-time code")
    n += 1
    add(maildir, n, "Old <old@gone.example>", "Hello", days_ago=800)
    n += 1
    add(maildir, n, "Friend <friend@example.org>", "Dinner?")
    n += 1
    add(maildir, n, "Me <me@example.com>", "Re: Dinner?", to="Friend <friend@example.org>")

    rows = by_sender(build(maildir, ["me@example.com"], now=NOW))

    assert (rows["news@shop.example"]["category"], rows["news@shop.example"]["drop_candidate"]) == ("newsletters", True)
    assert rows["news@shop.example"]["bulk_share"] == 1.0
    assert rows["orders@shop.example"]["category"] == "records"
    assert (rows["no-reply@bank.example"]["category"], rows["no-reply@bank.example"]["short_lived"]) == ("alerts", True)
    assert rows["no-reply@bank.example"]["noreply"] is True
    assert rows["old@gone.example"]["category"] == "inactive"
    assert rows["old@gone.example"]["previous_12_months"] == 0
    assert rows["friend@example.org"]["you_wrote_to"] == 1
    assert rows["friend@example.org"]["category"] == "personal"
    assert "me@example.com" not in rows


def test_counts_dates_and_delivery(maildir):
    add(maildir, 1, "a@x.example", "one", days_ago=500, extra="Delivered-To: second@example.com\nDelivered-To: first@example.com\n")
    add(maildir, 2, "a@x.example", "two", days_ago=5)

    (row,) = build(maildir, [], now=NOW)

    assert (row["messages"], row["last_12_months"], row["previous_12_months"]) == (2, 1, 1)
    assert row["first"] == (NOW - timedelta(days=500)).date().isoformat()
    assert set(row["delivered_to"].split()) == {"first@example.com", "second@example.com"}


def test_decodes_names_and_leaves_subjects_out(maildir, tmp_path):
    add(maildir, 1, "=?UTF-8?B?w4lsaXNl?= <elise@example.org>", "A private subject line")
    out = tmp_path / "report.csv"

    main([str(maildir.parent), "--output", str(out)])

    (row,) = list(csv.DictReader(out.open()))
    assert row["name"] == "Élise"
    assert "private subject" not in out.read_text()
