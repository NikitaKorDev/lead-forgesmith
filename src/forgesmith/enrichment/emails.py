"""Observed-email extraction and selection.

Design rules that matter more than the regex:

* scan visible text only (the parser removes scripts/hidden honeypots);
* never synthesize an address from a name pattern;
* eligibility decides whether an address exists, ranking only orders the
  survivors (a weighted threshold silently drops ``kontakt@gmail.com``);
* a free provider is a scoring signal, not a rejection reason.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from forgesmith.domain import registrable_domain, same_site
from forgesmith.enrichment.models import EmailHit
from forgesmith.enrichment.site import ParsedPage
from forgesmith.utils import walk_dicts

_EMAIL = re.compile(
    r"(?<![\w.%+\-])"
    r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+)"
    r"(?![\w.\-])"
)
_AT_BRACKET = re.compile(r"\s*[\[\(\{]\s*(?:at|małpa|malpa)\s*[\]\)\}]\s*", re.IGNORECASE)
_DOT_BRACKET = re.compile(r"\s*[\[\(\{]\s*(?:dot|kropka)\s*[\]\)\}]\s*", re.IGNORECASE)
_UUID = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)

_VENDOR_DOMAINS = {
    "sentry.io",
    "sentry-next.wixpress.com",
    "wixpress.com",
    "wix.com",
    "myshopify.com",
    "shopify.com",
    "shoper.pl",
    "shoper.com",
    "cloudflare.com",
    "example.com",
    "example.org",
    "domain.com",
    "email.com",
    "yourdomain.com",
    "sentry.wixpress.com",
}
_VENDOR_SUFFIXES = (".sentry.io", ".wixpress.com", ".myshopify.com")
_FORBIDDEN_LOCAL = {
    "no-reply",
    "noreply",
    "no_reply",
    "postmaster",
    "abuse",
    "webmaster",
    "hostmaster",
    "mailer-daemon",
}
_FILE_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "svg",
    "ico",
    "css",
    "js",
    "json",
    "xml",
    "pdf",
    "zip",
    "mp4",
}
_FREE_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "onet.pl",
    "wp.pl",
    "interia.pl",
    "o2.pl",
    "gazeta.pl",
    "poczta.onet.pl",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
}
_ROLE_PRIMARY = {
    "sales",
    "sprzedaz",
    "sprzedaż",
    "kontakt",
    "contact",
    "info",
    "biuro",
    "sklep",
    "shop",
    "zamowienia",
    "zamówienia",
    "orders",
    "office",
    "hello",
    "sekretariat",
    "recepcja",
}
_ROLE_SECONDARY = {
    "bok",
    "obsluga",
    "obsługa",
    "support",
    "pomoc",
    "reklamacje",
    "complaints",
    "ksiegowosc",
    "księgowość",
    "accounting",
    "press",
    "marketing",
    "newsletter",
    "wspolpraca",
    "współpraca",
}
_LEGAL_TLDS = re.compile(r"^[A-Za-z]{2,24}$|^xn--[A-Za-z0-9\-]+$")


def deobfuscate(text: str) -> str:
    """Turn ``name [at] domain [dot] pl`` and percent-encoding into an address."""
    text = text.replace("%40", "@").replace("%2E", ".").replace("%2e", ".")
    text = _AT_BRACKET.sub("@", text)
    return _DOT_BRACKET.sub(".", text)


def _valid_structure(email: str) -> bool:
    if email.count("@") != 1:
        return False
    local, domain = email.rsplit("@", 1)
    if not local or not domain or len(email) > 320 or len(local) > 64:
        return False
    if ".." in domain or domain.startswith(".") or domain.endswith("."):
        return False
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    tld = labels[-1].lower()
    if not _LEGAL_TLDS.match(tld):
        return False
    return tld not in _FILE_EXTENSIONS


def _from_mailto(href: str) -> list[str]:
    payload = unquote(href[len("mailto:") :])
    payload = payload.split("?", 1)[0]
    addresses: list[str] = []
    for part in re.split(r"[,;]", payload):
        part = part.strip().strip("<>").strip()
        if part:
            addresses.append(part)
    return addresses


def _jsonld_emails(page: ParsedPage) -> list[str]:
    found: list[str] = []
    for payload in page.jsonld:
        for node in walk_dicts(payload):
            value = node.get("email")
            if isinstance(value, str) and "@" in value:
                found.append(value.removeprefix("mailto:").strip())
    return found


def find_candidates(page: ParsedPage) -> list[str]:
    """Every address observed on a page, normalized and deduplicated."""
    text = deobfuscate(page.text)
    raw = _EMAIL.findall(text)
    for href in page.mailtos:
        raw.extend(_from_mailto(href))
    raw.extend(_jsonld_emails(page))
    seen: set[str] = set()
    emails: list[str] = []
    for value in raw:
        email = value.strip().strip(".,;:").lower()
        if "@" not in email or email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails


def classify(email: str, site_domain: str | None) -> str:
    local, domain = email.rsplit("@", 1)
    if local in _ROLE_PRIMARY or local in _ROLE_SECONDARY or local.startswith(("kontakt", "info")):
        return "role"
    if site_domain and same_site(domain, site_domain):
        return "personal"
    return "other"


def eligibility_reason(email: str, site_domain: str | None) -> str | None:
    """Return a rejection reason, or ``None`` when the address is usable."""
    if not _valid_structure(email):
        return "invalid_structure"
    local, domain = email.rsplit("@", 1)
    if local in _FORBIDDEN_LOCAL or local.startswith(("no-reply", "noreply")):
        return "forbidden_role"
    if domain in _VENDOR_DOMAINS or domain.endswith(_VENDOR_SUFFIXES):
        return "vendor_domain"
    if domain.split(".")[-1].lower() in _FILE_EXTENSIONS:
        return "file_address"
    if _UUID.match(local.replace(".", "").replace("-", "")):
        return "machine_generated"
    if site_domain and same_site(domain, site_domain):
        return None
    if local in _ROLE_PRIMARY or local in _ROLE_SECONDARY:
        return None
    return "not_site_or_role"


def _role_tier(local: str) -> int:
    if local in _ROLE_PRIMARY:
        return 0
    if local in _ROLE_SECONDARY:
        return 1
    return 2


def _confidence(email: str, site_domain: str | None, kind: str) -> float:
    if kind == "role":
        return 1.0 if site_domain and same_site(email.rsplit("@", 1)[1], site_domain) else 0.8
    return 0.5


def select_contacts(
    pages: list[tuple[str, ParsedPage]],
    site_domain: str | None,
    *,
    limit: int = 3,
) -> tuple[list[EmailHit], list[dict[str, str]]]:
    """Eligibility gate first, then role-first ordering; return hits and rejects."""
    hits: dict[str, EmailHit] = {}
    rejected: list[dict[str, str]] = []
    rejected_seen: set[str] = set()
    for page_url, page in pages:
        for email in find_candidates(page):
            if email in hits:
                continue
            reason = eligibility_reason(email, site_domain)
            if reason is not None:
                if email not in rejected_seen:
                    rejected_seen.add(email)
                    rejected.append({"email": email, "reason": reason})
                continue
            local, domain = email.rsplit("@", 1)
            kind = classify(email, site_domain)
            hits[email] = EmailHit(
                email=email,
                kind=kind,
                confidence=_confidence(email, site_domain, kind),
                source_url=page_url,
                local=local,
                domain=domain,
            )

    ordered = sorted(
        hits.values(),
        key=lambda hit: (
            _role_tier(hit.local),
            0 if site_domain and same_site(hit.domain, site_domain) else 1,
            0 if hit.domain in _FREE_DOMAINS else 1,
            hit.email,
        ),
    )
    primary = [hit for hit in ordered if _role_tier(hit.local) == 0]
    if primary:
        selected = primary[:limit]
    else:
        secondary = [hit for hit in ordered if _role_tier(hit.local) == 1]
        selected = secondary[:1] if secondary else ordered[:1]
    return selected, rejected


def is_free_provider(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in _FREE_DOMAINS


def registrable(email_or_domain: str) -> str | None:
    value = email_or_domain.rsplit("@", 1)[-1]
    return registrable_domain(value)
