"""Your Gmail, by voice: "check my mails" lists the new ones, "read the second one" reads it out. Gmail is read in
Ari's browser on the Workstation (signed in once); a fake browser stands in for Chrome here."""

from __future__ import annotations

import pytest

from argus.worker import think as th
from argus.worker.think import straight_to
from test_workstation import FakeDesk, ws
from test_workstation_browser import FakeBrowser, plug_ctx

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed version="0.3" xmlns="http://purl.org/atom/ns#">
  <title>Gmail - Inbox for sas@example.com</title><fullcount>3</fullcount>
  <entry><title>Dinner on Friday?</title><summary>Are we still on for Friday at 7? I booked the place</summary>
    <link rel="alternate" href="https://mail.google.com/mail?account_id=x&amp;message_id=a1&amp;view=conv"/>
    <issued>2026-10-09T10:00:00Z</issued><author><name>Kaancha Perera</name><email>kaancha@example.com</email></author>
  </entry>
  <entry><title>Your statement is ready</title><summary>Your October statement for account ending 4411</summary>
    <link rel="alternate" href="https://mail.google.com/mail?account_id=x&amp;message_id=b2&amp;view=conv"/>
    <issued>2026-10-09T08:00:00Z</issued><author><name>HNB Alerts</name><email>alerts@hnb.lk</email></author>
  </entry>
  <entry><title>Invoice #2210</title><summary>Please find the invoice attached</summary>
    <link rel="alternate" href="https://mail.google.com/mail?account_id=x&amp;message_id=c3&amp;view=conv"/>
    <issued>2026-10-08T18:00:00Z</issued><author><email>billing@acme.io</email></author>
  </entry>
</feed>"""

TOOLS = {t: {} for t in ("check_email", "read_email", "do_in_browser", "open_app", "move_window_to_me",
                         "take_window", "search_in_browser", "web_search")}


def mail_ctx(inp=None, store=None, **extra):
    d = FakeDesk([(1, "Word", "winword", "you")])
    d.wins[50] = ws.Win(50, "Inbox - Gmail - Google Chrome", "chrome", 77)
    d.on[50] = "ari"
    b = FakeBrowser({"x": {"title": "Gmail", "items": []}}, "x")
    c = plug_ctx(d, b, inp or {}, **extra)
    c.store.data.update(store or {})
    return c, d, b


def test_the_feed_and_which_mail_you_meant():
    mails = ws.parse_feed(FEED)
    assert [m["from"] for m in mails] == ["Kaancha Perera", "HNB Alerts", "billing@acme.io"]
    assert mails[0]["subject"] == "Dinner on Friday?" and "message_id=a1" in mails[0]["link"]
    pick = lambda w: (ws.pick_mail(mails, w) or {}).get("subject")  # noqa: E731
    assert pick("") == pick("the first one") == pick("1") == pick("latest") == "Dinner on Friday?"
    assert pick("second") == pick("2") == pick("from HNB") == "Your statement is ready"
    assert pick("last") == pick("about the invoice") == pick("acme") == "Invoice #2210"
    assert pick("from nobody") is None and pick("9") is None


def test_check_my_mails_lists_the_new_ones():
    c, d, b = mail_ctx(gmail_unread=lambda page: {"mails": ws.parse_feed(FEED)})
    out = ws.mail_new(c)
    assert out["done"] and out["count"] == 3 and out["mails"][1]["from"] == "HNB Alerts"
    assert len(c.store.data["mail"]) == 3  # kept for "read the second one"
    said = th.mail_said("check_email", out)
    assert said.startswith("You've got 3 new emails.") and "1, from Kaancha Perera, about Dinner on Friday?" in said
    assert "billing" in said and said.endswith("Want me to read one? Say which.")
    assert th.mail_follow_up("check_email", out) == {"kind": "tool", "name": "read_email", "args": {"which": "1"}}
    assert th.mail_said("check_email", {"done": True, "count": 0, "mails": []}).startswith("No new emails")


def test_read_the_second_one():
    mails = ws.parse_feed(FEED)
    opened = []

    def gmail_open(page, link):
        opened.append(link)
        return {"ok": True, "subject": "Your statement is ready", "from": "HNB Alerts",
                "text": "Dear customer,\n\nYour statement for October is ready. Log in to see it."}

    c, d, b = mail_ctx({"which": "second"}, store={"mail": mails}, gmail_open=gmail_open,
                       gmail_unread=lambda page: pytest.fail("the list from the last check is used"))
    out = ws.mail_read(c)
    assert out["done"] and out["whole"] and "message_id=b2" in opened[0]
    said = th.mail_said("read_email", out)
    assert said.startswith("From HNB Alerts, Your statement is ready. It says: Dear customer, Your statement")


def test_not_signed_in_hands_you_the_window():
    c, d, b = mail_ctx(gmail_unread=lambda page: {"login": True})
    out = ws.mail_new(c)
    assert out["handed_to_you"] and "sign in once" in out["problem"] and d.on[50] == "you"
    assert b.went == ["https://mail.google.com/"]
    assert "sign in once" in th.mail_said("check_email", out)


@pytest.mark.parametrize("said,tool,args", [
    ("check my mails", "check_email", {}),
    ("Open Chrome and check my emails", "check_email", {}),
    ("Can you open Chrome and check for my emails", "check_email", {}),
    ("any new emails?", "check_email", {}),
    ("tell me what new mails I got", "check_email", {}),
    ("what's in my inbox", "check_email", {}),
    ("read the second one", "read_email", {"which": "second"}),
    ("what does the mail from Kaancha say", "read_email", {"which": "from Kaancha"}),
    ("open the email about the invoice", "read_email", {"which": "about the invoice"}),
    ("read email 3", "read_email", {"which": "3"}),
    ("open chrome and send an email to kaancha", "do_in_browser",
     {"goal": "send an email to kaancha", "url": "https://mail.google.com/"}),  # writing is a browser task
])
def test_said_plainly(said, tool, args):
    assert straight_to(TOOLS, said) == (tool, args)


def test_mail_tools_are_private_and_described():
    from pathlib import Path

    import yaml

    m = yaml.safe_load((Path(ws.__file__).parent / "plugin.yaml").read_text())
    tools = {t["name"]: t for t in m["ari"]["tools"]}
    assert tools["check_email"]["private"] and tools["read_email"]["private"]  # never to Claude
