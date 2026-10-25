from __future__ import annotations

import base64
import quopri

from forgesmith.inbox.models import InboundKind
from forgesmith.inbox.parse import (
    canonical_message_id,
    find_optout_phrase,
    fold_text,
    headers_want_body,
    parse_message,
    split_references,
    strip_quoted,
)

OPTOUT = "Prosz\u0119 nie kontaktowa\u0107 si\u0119 wi\u0119cej."


def _dsn(
    *,
    recipients: list[tuple[str, str, str]] | None = None,
    message_id: str | None = "<orig@example.com>",
    dsn_message_id: str | None = "<dsn-1@example.net>",
    diagnostic: str = "smtp; 550 5.1.1 user unknown",
    include_original: bool = True,
) -> bytes:
    recipients = recipients if recipients is not None else [("biuro@a.pl", "failed", "5.1.1")]
    blocks = [
        "Reporting-MTA: dns; mx.example.net\r\n",
    ]
    for recipient, action, status in recipients:
        blocks.append(
            f"Final-Recipient: rfc822; {recipient}\r\n"
            f"Action: {action}\r\n"
            f"Status: {status}\r\n"
            f"Diagnostic-Code: {diagnostic}\r\n"
        )
    delivery = "\r\n".join(blocks)
    original = ""
    if include_original and message_id:
        original = (
            "--b1\r\n"
            "Content-Type: message/rfc822\r\n"
            "\r\n"
            "From: jan@example.com\r\n"
            f"Message-ID: {message_id}\r\n"
            "Subject: Hello\r\n"
            "\r\n"
            "original body\r\n"
        )
    return (
        "From: Mail Delivery System <MAILER-DAEMON@example.net>\r\n"
        "To: jan@example.com\r\n"
        "Subject: Undelivered Mail Returned to Sender\r\n"
        "Date: Thu, 22 Oct 2026 10:00:00 +0000\r\n"
        + (f"Message-ID: {dsn_message_id}\r\n" if dsn_message_id else "")
        + "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"\r\n'
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "This is the mail system at host mx.example.net.\r\n"
        "--b1\r\n"
        "Content-Type: message/delivery-status\r\n"
        "\r\n"
        f"{delivery}"
        f"{original}"
        "--b1--\r\n"
    ).encode("utf-8")


def _reply(
    body: str,
    *,
    from_addr: str = "anna@a.pl",
    in_reply_to: str | None = "<orig@example.com>",
    references: str | None = None,
    extra_headers: str = "",
    content_type: str = "text/plain; charset=utf-8",
    transfer_encoding: str | None = None,
    raw_body: bytes | None = None,
) -> bytes:
    headers = [
        f"From: Anna <{from_addr}>",
        "To: jan@example.com",
        "Subject: Re: Hello",
        "Date: Thu, 22 Oct 2026 11:00:00 +0000",
        "Message-ID: <reply-1@a.pl>",
        f"Content-Type: {content_type}",
    ]
    if transfer_encoding:
        headers.append(f"Content-Transfer-Encoding: {transfer_encoding}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if references:
        headers.append(f"References: {references}")
    if extra_headers:
        headers.append(extra_headers)
    payload = raw_body if raw_body is not None else body.encode("utf-8")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload


def _headers(raw: bytes) -> bytes:
    return raw.split(b"\r\n\r\n", 1)[0]


# ------------------------------------------------------------------ DSNs
def test_hard_dsn_is_parsed_and_correlated() -> None:
    parsed = parse_message(_dsn())
    assert parsed.kind is InboundKind.DSN
    assert parsed.is_hard_bounce is True
    assert parsed.is_soft_bounce is False
    assert parsed.final_recipient == "biuro@a.pl"
    assert parsed.enhanced_status == "5.1.1"
    assert parsed.smtp_code == 550
    assert parsed.action == "failed"
    assert canonical_message_id(parsed.reported_message_id) == "orig@example.com"
    assert "original body" not in str(parsed.detail)


def test_soft_dsn_is_not_a_hard_bounce() -> None:
    parsed = parse_message(_dsn(recipients=[("biuro@a.pl", "failed", "4.2.2")]))
    assert parsed.kind is InboundKind.DSN
    assert parsed.is_soft_bounce is True
    assert parsed.is_hard_bounce is False


def test_delayed_action_is_not_a_bounce() -> None:
    parsed = parse_message(_dsn(recipients=[("biuro@a.pl", "delayed", "4.4.1")]))
    assert parsed.action == "delayed"
    assert parsed.is_hard_bounce is False
    assert parsed.is_soft_bounce is True


def test_multi_recipient_dsn_picks_the_failed_block() -> None:
    parsed = parse_message(
        _dsn(
            recipients=[
                ("ok@a.pl", "delivered", "2.0.0"),
                ("biuro@b.pl", "failed", "5.1.1"),
            ]
        )
    )
    assert parsed.final_recipient == "biuro@b.pl"
    assert parsed.enhanced_status == "5.1.1"


def test_dsn_without_final_recipient_falls_back_to_failed_recipients() -> None:
    raw = _dsn(recipients=[], include_original=False, dsn_message_id=None).replace(
        b"Subject: Undelivered Mail Returned to Sender\r\n",
        b"Subject: Undelivered Mail Returned to Sender\r\nX-Failed-Recipients: biuro@c.pl\r\n",
    )
    parsed = parse_message(raw)
    assert parsed.final_recipient == "biuro@c.pl"
    assert parsed.reported_message_id is None
    assert parsed.detail["failed_recipients"] == "biuro@c.pl"


def test_arf_and_mdn_are_classified_separately() -> None:
    arf = _reply(
        "This is spam.",
        content_type='multipart/report; report-type=feedback-report; boundary="f"',
        raw_body=(
            b"--f\r\nContent-Type: text/plain\r\n\r\nspam\r\n"
            b"--f\r\nContent-Type: message/feedback-report\r\n\r\n"
            b"Feedback-Type: abuse\r\n\r\n--f--\r\n"
        ),
    )
    assert parse_message(arf).kind is InboundKind.ARF

    mdn = _reply(
        "Read.",
        content_type='multipart/report; report-type=disposition-notification; boundary="m"',
        raw_body=(
            b"--m\r\nContent-Type: text/plain\r\n\r\nread\r\n"
            b"--m\r\nContent-Type: message/disposition-notification\r\n\r\n"
            b"Disposition: manual-action/MDN-sent-manually; displayed\r\n\r\n--m--\r\n"
        ),
    )
    assert parse_message(mdn).kind is InboundKind.MDN


# ---------------------------------------------------------------- replies
def test_thread_reply_is_recognized() -> None:
    parsed = parse_message(_reply("Dzi\u0119kuj\u0119, odezw\u0119 si\u0119."))
    assert parsed.kind is InboundKind.REPLY
    assert parsed.referenced_ids == ("orig@example.com",)
    assert parsed.from_addr == "anna@a.pl"


def test_references_are_ordered_and_deduplicated() -> None:
    parsed = parse_message(
        _reply(
            "ok",
            in_reply_to="<second@example.com>",
            references="<first@example.com> <second@example.com> <third@example.com>",
        )
    )
    assert parsed.referenced_ids == (
        "second@example.com",
        "first@example.com",
        "third@example.com",
    )


def test_optout_phrase_is_found_after_diacritic_folding() -> None:
    parsed = parse_message(_reply(OPTOUT))
    assert parsed.kind is InboundKind.OPTOUT
    assert parsed.optout_phrase == "prosze nie kontaktowac"


def test_quoted_history_is_not_scanned() -> None:
    body = (
        "Dzi\u0119kuj\u0119 za wiadomo\u015b\u0107.\r\n"
        "\r\n"
        "W dniu 22.10.2026 o 10:00 jan@example.com napisa\u0142:\r\n"
        "> unsubscribe me please\r\n"
    )
    parsed = parse_message(_reply(body))
    assert parsed.kind is InboundKind.REPLY
    assert parsed.optout_phrase is None


def test_our_own_footer_does_not_trip_the_optout_scan() -> None:
    body = (
        "Dzi\u0119kuj\u0119, ale mamy ju\u017c swoje rozwi\u0105zanie.\r\n"
        "\r\n"
        "> Je\u015bli ta wiadomo\u015b\u0107 nie jest dla Pa\u0144stwa, "
        "prosz\u0119 o kr\u00f3tk\u0105 odpowied\u017a \u2013 usun\u0119 kontakt.\r\n"
    )
    parsed = parse_message(_reply(body))
    assert parsed.kind is InboundKind.REPLY


def test_quoted_printable_polish_body() -> None:
    encoded = quopri.encodestring(OPTOUT.encode("utf-8"))
    parsed = parse_message(
        _reply(
            "",
            transfer_encoding="quoted-printable",
            raw_body=encoded,
        )
    )
    assert parsed.kind is InboundKind.OPTOUT
    assert parsed.optout_phrase == "prosze nie kontaktowac"


def test_base64_polish_body() -> None:
    encoded = base64.b64encode(OPTOUT.encode("utf-8"))
    parsed = parse_message(_reply("", transfer_encoding="base64", raw_body=encoded))
    assert parsed.kind is InboundKind.OPTOUT


def test_html_only_body_is_scanned() -> None:
    parsed = parse_message(
        _reply(
            "",
            content_type="text/html; charset=utf-8",
            raw_body=(
                "<html><body><p>Prosz\u0119 nie kontaktowa\u0107 si\u0119</p></body></html>"
            ).encode("utf-8"),
        )
    )
    assert parsed.kind is InboundKind.OPTOUT


def test_auto_submitted_is_not_a_reply_or_optout() -> None:
    parsed = parse_message(
        _reply(
            "I am on holiday. unsubscribe is in my signature.",
            extra_headers="Auto-Submitted: auto-replied",
        )
    )
    assert parsed.kind is InboundKind.AUTO
    assert parsed.optout_phrase is None


def test_precedence_bulk_and_list_id_are_auto() -> None:
    assert parse_message(_reply("x", extra_headers="Precedence: bulk")).kind is InboundKind.AUTO
    assert parse_message(_reply("x", extra_headers="List-Id: <x.example>")).kind is InboundKind.AUTO


# ----------------------------------------------------------- body fetching
def test_headers_want_body_for_reports_and_threads_only() -> None:
    assert headers_want_body(_headers(_dsn())) is True
    assert headers_want_body(_headers(_reply("hello"))) is True
    plain = b"From: a@b.pl\r\nTo: c@d.pl\r\nSubject: hi\r\n\r\nbody"
    assert headers_want_body(_headers(plain)) is False


# --------------------------------------------------------------- utilities
def test_strip_quoted_keeps_only_the_top_of_the_message() -> None:
    text = "Yes.\n\nOn 22 Oct 2026, jan@example.com wrote:\n> unsubscribe"
    assert strip_quoted(text) == "Yes."
    assert strip_quoted("> quoted only") == ""
    assert strip_quoted("Line one\n-- \nsig") == "Line one"


def test_fold_text_handles_polish_l() -> None:
    assert fold_text("Prosz\u0119 nie kontaktowa\u0107, \u0142askawie!") == (
        "prosze nie kontaktowac laskawie"
    )
    assert find_optout_phrase("Prosz\u0119 nie pisa\u0107.") == "prosze nie pisac"


def test_canonical_message_id_and_references() -> None:
    assert canonical_message_id("<A@B.PL>") == "a@b.pl"
    assert canonical_message_id("  <a@b.pl>  ") == "a@b.pl"
    assert canonical_message_id("") is None
    assert split_references("<a@b> <c@d>") == ("a@b", "c@d")
    assert split_references("garbage") == ("garbage",)
    assert split_references(None) == ()


def test_dedupe_hash_prefers_message_id_and_is_stable() -> None:
    first = parse_message(_dsn())
    second = parse_message(_dsn())
    assert first.dedupe_hash == second.dedupe_hash
    assert len(first.dedupe_hash) == 64

    no_id_a = parse_message(_dsn(include_original=False, dsn_message_id=None))
    no_id_b = parse_message(_dsn(include_original=False, dsn_message_id=None))
    assert no_id_a.dedupe_hash == no_id_b.dedupe_hash
    assert no_id_a.dedupe_hash != first.dedupe_hash
