import base64
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


def test_bulk_senders_written_to_once_monitoring_and_more_records(maildir):
    n = 0
    for i in range(20):
        n += 1
        add(maildir, n, "Venue <members@venue.example>", f"What's on {i}", extra="List-Unsubscribe: <mailto:u@venue.example>\n")
    n += 1
    add(maildir, n, "Me <me@example.com>", "Membership question", to="members@venue.example")
    for i in range(12):
        n += 1
        add(maildir, n, "Monitor <alerts@monitor.example>", f"[DOWN] job {i}", extra="List-Unsubscribe: <mailto:u@monitor.example>\n")
    n += 1
    add(maildir, n, "Market <transaction@market.example>", "Transaction complete")
    n += 1
    add(maildir, n, "Lender <updates@lender.example>", "Mortgage application received")

    rows = by_sender(build(maildir, ["me@example.com"], now=NOW))

    venue = rows["members@venue.example"]
    assert (venue["category"], venue["drop_candidate"], venue["you_wrote_to"]) == ("newsletters", False, 1)
    assert (rows["alerts@monitor.example"]["category"], rows["alerts@monitor.example"]["short_lived"]) == ("alerts", False)
    assert rows["transaction@market.example"]["category"] == "records"
    assert rows["updates@lender.example"]["category"] == "records"


def test_routing_headers_and_spam_scores(maildir):
    add(maildir, 1, "a@x.example", "one", extra="X-Original-To: old@example.com\nX-Forwarded-For: old@example.com new@example.com\nX-Spamd-Bar: +++\n")
    add(maildir, 2, "a@x.example", "two", extra="X-Original-To: new@example.com\nX-Spamd-Bar: -\n")
    add(maildir, 3, "b@y.example", "Your purchase from a shop", extra="X-Spam-Score: 4.5 (yes)\n")

    rows = by_sender(build(maildir, [], now=NOW))

    a = rows["a@x.example"]
    assert set(a["original_to"].split()) == {"old@example.com", "new@example.com"}
    assert a["forwarded_for"] == "old@example.com"
    assert a["spam_score"] == 1.0
    assert (rows["b@y.example"]["spam_score"], rows["b@y.example"]["category"]) == (4.5, "records")


def test_names_with_commas_stay_one_sender(maildir):
    encoded = "=?UTF-8?B?" + base64.b64encode("Smith, John".encode()).decode() + "?="
    add(maildir, 1, f"{encoded} <john@bank.example>", "Statement")
    add(maildir, 2, "Me <me@example.com>", "Hello", to=f"{encoded} <john@bank.example>, other@example.org")

    rows = by_sender(build(maildir, ["me@example.com"], now=NOW))

    assert set(rows) == {"john@bank.example"}
    assert rows["john@bank.example"]["name"] == "Smith, John"
    assert rows["john@bank.example"]["you_wrote_to"] == 1
