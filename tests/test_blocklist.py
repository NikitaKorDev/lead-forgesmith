"""The shared host blocklist must reject profile pages, not just bare domains."""

from __future__ import annotations

import pytest

from forgesmith.blocklist import BLOCKED_DOMAINS, BLOCKED_SUFFIXES, usable_host


@pytest.mark.parametrize(
    "host",
    [
        "mebis.pl",
        "www.mebis.pl",
        "sklep.mebis.com.pl",
        "kowalski.co.uk",
        "example.com",
    ],
)
def test_company_storefronts_are_usable(host: str) -> None:
    assert usable_host(host)


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "mebis",
        "localhost",
        "192.168.0.10",
        "8.8.8.8",
        "::1",
    ],
)
def test_hosts_without_a_registrable_name_are_not_usable(host: str | None) -> None:
    assert not usable_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "facebook.com",
        "www.facebook.com",
        "m.facebook.com",
        "instagram.com",
        "linkedin.com",
        "olx.pl",
        "www.olx.pl",
        "sklep.olx.pl",
        "d123.olxcdn.com",
        "static.olxstatic.com",
        "allegro.pl",
        "bit.ly",
        "goo.gl",
        "linktr.ee",
        "www.google.com",
    ],
)
def test_marketplaces_socials_and_shorteners_are_refused(host: str) -> None:
    assert not usable_host(host)


def test_the_list_is_exported_for_a_second_caller() -> None:
    assert "facebook.com" in BLOCKED_DOMAINS
    assert ".cloudfront.net" in BLOCKED_SUFFIXES
