"""Message building, reply threading, `received_for` and `allowed_from`.

All pure functions — no sockets.
"""

from __future__ import annotations

import email
from email.message import Message
from email.utils import parsedate_to_datetime

import pytest

from mailboxd.message import (
    FromNotAllowedError,
    MessageError,
    build_message,
    check_from_allowed,
    normalize_msgids,
    received_for,
    reply_references,
    reply_subject,
    strip_bcc,
    to_bytes,
)

DOMAIN = "mydomain.example"
LOGIN = "inbox@mydomain.example"

# Longer than the 78-column fold limit, like Amazon SES / Exchange Message-IDs.
LONG_ID = "<0100018f3a2b4c5d-6e7f8a9b-0c1d-4e2f-8a3b-4c5d6e7f8a9b-000000@us-east-1.amazonses.com>"
LONG_ID_2 = "<CAF=abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ@mail.gmail.com>"


def _roundtrip(msg: Message) -> Message:
    """What a receiving client sees: serialize, then parse the bytes back."""
    return email.message_from_bytes(to_bytes(msg))


def _inbound(**headers: str | list[str]) -> Message:
    msg = Message()
    for name, value in headers.items():
        for v in value if isinstance(value, list) else [value]:
            msg[name.replace("_", "-")] = v
    return msg


# --------------------------------------------------------------------------
# build_message
# --------------------------------------------------------------------------


def test_build_sets_every_requested_header() -> None:
    msg = _roundtrip(
        build_message(
            from_address="Sales <sales@mydomain.example>",
            to=["a@example.com", "Bee <b@example.com>"],
            subject="hello",
            body_text="plain",
            cc=["c@example.com"],
            bcc=["hidden@example.com"],
            reply_to="replies@mydomain.example",
            in_reply_to="<parent@example.com>",
            references="<root@example.com> <parent@example.com>",
        )
    )
    assert msg["From"] == "Sales <sales@mydomain.example>"
    assert msg["To"] == "a@example.com, Bee <b@example.com>"
    assert msg["Cc"] == "c@example.com"
    assert msg["Bcc"] == "hidden@example.com"
    assert msg["Subject"] == "hello"
    assert msg["Reply-To"] == "replies@mydomain.example"
    assert msg["In-Reply-To"] == "<parent@example.com>"
    assert msg["References"] == "<root@example.com> <parent@example.com>"
    assert msg.get_payload(decode=True).strip() == b"plain"


def test_build_generates_message_id_on_sender_domain_and_a_date() -> None:
    first = build_message("Sales <sales@mydomain.example>", ["a@example.com"], "s", "b")
    second = build_message("Sales <sales@mydomain.example>", ["a@example.com"], "s", "b")
    assert first["Message-ID"].startswith("<")
    assert first["Message-ID"].endswith("@mydomain.example>")
    assert first["Message-ID"] != second["Message-ID"]
    # Raises if the Date header is missing or not RFC 5322.
    assert parsedate_to_datetime(first["Date"]) is not None


def test_build_text_and_html_is_multipart_alternative() -> None:
    msg = _roundtrip(build_message("a@x.com", ["b@y.com"], "s", "plain", "<p>rich</p>"))
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.get_payload()] == ["text/plain", "text/html"]


def test_build_allows_an_empty_draft() -> None:
    msg = _roundtrip(build_message("a@x.com"))
    assert msg["To"] is None
    assert msg["Subject"] == ""
    assert msg["Message-ID"] and msg["Date"]


def test_long_message_ids_are_not_rfc2047_encoded() -> None:
    # The stdlib folds an over-long In-Reply-To / References into `=?utf-8?q?`
    # encoded-words, which breaks threading in every client.
    raw = to_bytes(
        build_message(
            "a@x.com",
            ["b@y.com"],
            "s",
            "b",
            in_reply_to=LONG_ID,
            references=[LONG_ID_2, LONG_ID, "<short@x.com>"],
        )
    )
    assert b"=?utf-8?" not in raw
    msg = email.message_from_bytes(raw)
    assert msg["In-Reply-To"] == LONG_ID
    assert msg["References"].split() == [LONG_ID_2, LONG_ID, "<short@x.com>"]


def test_build_rejects_header_injection() -> None:
    with pytest.raises(MessageError):
        build_message("a@x.com", ["b@y.com"], "hi\r\nBcc: evil@example.com", "b")
    with pytest.raises(MessageError):
        build_message("a@x.com\r\nBcc: evil@example.com", ["b@y.com"], "s", "b")


def test_normalize_msgids_accepts_bare_bracketed_and_lists() -> None:
    assert normalize_msgids("a@x b@y") == ["<a@x>", "<b@y>"]
    assert normalize_msgids("<a@x><b@y>") == ["<a@x>", "<b@y>"]
    assert normalize_msgids(["<a@x>", "b@y"]) == ["<a@x>", "<b@y>"]
    assert normalize_msgids(None) == []
    with pytest.raises(MessageError):
        normalize_msgids("<a b>")


def test_strip_bcc_only_touches_the_header_block() -> None:
    raw = b"From: a@x.com\r\nBcc: h1@x.com,\r\n h2@x.com\r\nTo: b@y.com\r\n\r\nBcc: body line\r\n"
    assert strip_bcc(raw) == b"From: a@x.com\r\nTo: b@y.com\r\n\r\nBcc: body line\r\n"


# --------------------------------------------------------------------------
# reply threading
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("hello", "Re: hello"),
        ("Re: hello", "Re: hello"),
        ("RE: re:  Re: hello", "Re: hello"),
        ("Regarding lunch", "Re: Regarding lunch"),
        ("", "Re:"),
        (None, "Re:"),
    ],
)
def test_reply_subject_has_a_single_re_prefix(subject: str | None, expected: str) -> None:
    assert reply_subject(subject) == expected


def test_reply_references_appends_message_id_to_existing_chain() -> None:
    refs = reply_references("<root@x> <mid@x>", "<mid@x>", "<orig@x>")
    assert refs == ["<root@x>", "<mid@x>", "<orig@x>"]


def test_reply_references_falls_back_to_in_reply_to() -> None:
    assert reply_references(None, "<parent@x>", "<orig@x>") == ["<parent@x>", "<orig@x>"]
    assert reply_references("", "", "<orig@x>") == ["<orig@x>"]


# --------------------------------------------------------------------------
# received_for
# --------------------------------------------------------------------------


def test_received_for_prefers_delivered_to() -> None:
    msg = _inbound(
        Delivered_To="sales@mydomain.example",
        X_Original_To="other@mydomain.example",
        To="Hello <hello@mydomain.example>",
    )
    assert received_for(msg, DOMAIN, LOGIN) == "sales@mydomain.example"


def test_received_for_checks_headers_in_order() -> None:
    headers = {
        "X_Original_To": "xo@mydomain.example",
        "Envelope_To": "env@mydomain.example",
        "To": "to@mydomain.example",
        "Cc": "cc@mydomain.example",
    }
    expected = [
        "xo@mydomain.example",
        "env@mydomain.example",
        "to@mydomain.example",
        "cc@mydomain.example",
    ]
    for want in expected:
        assert received_for(_inbound(**headers), DOMAIN, LOGIN) == want
        del headers[next(iter(headers))]
    assert received_for(_inbound(**headers), DOMAIN, LOGIN) is None


def test_received_for_skips_the_catch_all_login_when_something_more_specific_exists() -> None:
    # The server stamps Delivered-To with the catch-all mailbox itself; the
    # address the sender actually used is further down.
    msg = _inbound(Delivered_To=LOGIN, To="Support <support@mydomain.example>")
    assert received_for(msg, DOMAIN, LOGIN) == "support@mydomain.example"


def test_received_for_returns_the_login_address_when_it_is_the_only_match() -> None:
    msg = _inbound(Delivered_To=LOGIN, To="Email <INBOX@mydomain.example>", Cc="x@example.com")
    assert received_for(msg, DOMAIN, LOGIN) == "INBOX@mydomain.example"
    # Bcc'd to the catch-all: only the server's own stamp mentions us.
    assert received_for(_inbound(Delivered_To=LOGIN, To="x@example.com"), DOMAIN, LOGIN) == LOGIN


def test_received_for_ignores_other_domains() -> None:
    msg = _inbound(
        Delivered_To="someone@gmail.com",
        To=["a@example.com, Sales <sales@mydomain.example>"],
        Cc="b@sub.mydomain.example",
    )
    assert received_for(msg, DOMAIN, LOGIN) == "sales@mydomain.example"
    assert received_for(_inbound(To="a@example.com"), DOMAIN, LOGIN) is None
    assert received_for(_inbound(To="a@notmydomain.example"), DOMAIN, LOGIN) is None


def test_received_for_does_not_answer_as_a_colleague() -> None:
    # An ordinary (non-catch-all) mailbox: the mail was sent to me, the boss
    # is merely copied. "Skip the login address" must not turn that into the
    # boss's address.
    me = "me@corp.example"
    msg = _inbound(To=f"Me <{me}>", Cc="Boss <boss@corp.example>")
    assert received_for(msg, "corp.example", me) == me
    msg = _inbound(To="boss@corp.example, team@corp.example", Cc=me)
    assert received_for(msg, "corp.example", me) == me
    # Same when the server also stamps the delivery.
    msg = _inbound(Delivered_To=me, To=me, Cc="boss@corp.example")
    assert received_for(msg, "corp.example", me) == me


def test_received_for_only_trusts_the_topmost_delivery_header() -> None:
    # The receiving server prepends its own Delivered-To; one further down is
    # an earlier hop — or something the sender typed into the message.
    msg = _inbound(Delivered_To=[LOGIN, "ceo@mydomain.example"], To=LOGIN)
    assert received_for(msg, DOMAIN, LOGIN) == LOGIN
    msg = _inbound(Delivered_To=["billing@MyDomain.example", "old-hop@gmail.com"])
    assert received_for(msg, DOMAIN, LOGIN) == "billing@MyDomain.example"


def test_received_for_without_a_mailbox_domain_is_none() -> None:
    assert received_for(_inbound(To="sales@mydomain.example"), None, None) is None


# --------------------------------------------------------------------------
# allowed_from
# --------------------------------------------------------------------------


def test_allowed_from_empty_means_unrestricted() -> None:
    check_from_allowed("anyone@anywhere.example", [])
    check_from_allowed("anyone@anywhere.example", None)


@pytest.mark.parametrize(
    "sender",
    [
        "sales@mydomain.example",
        "Sales Team <sales@mydomain.example>",
        "SALES@MYDOMAIN.EXAMPLE",
        "exact@other.example",
    ],
)
def test_allowed_from_accepts_matches(sender: str) -> None:
    check_from_allowed(sender, ["*@mydomain.example", "exact@other.example"])


@pytest.mark.parametrize(
    "sender",
    [
        "ceo@example.com",
        "x@sub.mydomain.example",
        "x@mydomain.example.evil.example",
        # The display name is not the address.
        '"sales@mydomain.example" <ceo@example.com>',
        # A second address must not ride along behind an allowed one...
        "sales@mydomain.example, ceo@example.com",
        # ...including one the stdlib address parser silently drops.
        "ceo@example.com[], sales@mydomain.example",
        "sales@mydomain.example <ceo@example.com>",
        "sales@mydomain.example (ceo@example.com)",
        "sales@mydomain.example\n",
        "sales@mydomain.example\u2028",
        "not-an-address",
        "",
    ],
)
def test_allowed_from_rejects_everything_else(sender: str) -> None:
    with pytest.raises(FromNotAllowedError):
        check_from_allowed(sender, ["*@mydomain.example"])
