from __future__ import annotations

from sqlalchemy import select

from forgesmith.config import settings
from forgesmith.db.models import Contact, EnrichmentStatusDB, Event, Lead, LeadStatusDB
from forgesmith.db.repo import EnrichmentRepo, LeadRepo
from forgesmith.db.session import session_scope
from forgesmith.domain import LeadCandidate, LeadSource
from forgesmith.enrichment.models import PageSnapshot
from forgesmith.enrichment.runner import enrich_pending


class FakeFetcher:
    def __init__(self, pages: dict[str, PageSnapshot]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, **kwargs: object) -> PageSnapshot:
        self.calls.append(url)
        return self.pages.get(
            url,
            PageSnapshot(
                url=url,
                final_url=url,
                status=404,
                html="",
                engine="http",
                bytes=0,
                elapsed_ms=0,
                error="http_404",
            ),
        )


def snap(
    url: str,
    html: str,
    *,
    status: int | None = 200,
    error: str | None = None,
    engine: str = "http",
) -> PageSnapshot:
    return PageSnapshot(
        url=url,
        final_url=url,
        status=status,
        html=html,
        engine=engine,
        bytes=len(html),
        elapsed_ms=1,
        error=error,
    )


async def make_lead(
    source_id: str,
    *,
    domain: str | None,
    source: LeadSource = LeadSource.SHOPIFY,
    raw: dict | None = None,
    url: str | None = None,
) -> int:
    candidate = LeadCandidate(
        source=source,
        source_id=source_id,
        name="Sklep Testowy",
        url=url or f"https://{domain}/" if domain else None,
        domain=domain,
        raw=raw or {},
    )
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([candidate])
        lead = await LeadRepo(session).by_source_id(source.value, source_id)
        assert lead is not None
        return lead.id


HOMEPAGE = (
    "<html><body><h1>Pracownia mebli</h1>"
    "<p>Produkujemy meble dębowe z litego drewna od wielu lat.</p>"
    '<a href="/kontakt">Kontakt</a>'
    "<p>Nasza pracownia znajduje się w Warszawie i realizuje zamówienia w całej Polsce.</p>"
    "</body></html>"
)
CONTACT = '<html><body><h1>Kontakt</h1><a href="mailto:kontakt@a.pl">napisz</a></body></html>'


def happy_fetcher() -> FakeFetcher:
    return FakeFetcher(
        {
            "https://a.pl/": snap("https://a.pl/", HOMEPAGE),
            "https://a.pl/kontakt": snap("https://a.pl/kontakt", CONTACT),
        }
    )


async def test_happy_path_enriches_stores_contact_context_and_event(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 2)
    lead_id = await make_lead("a.pl", domain="a.pl")
    fetcher = happy_fetcher()

    stats = await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)

    assert stats.enriched == 1
    assert stats.emails == 1
    assert stats.failed == 0
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
        contacts = (await session.execute(select(Contact))).scalars().all()
        events = (await session.execute(select(Event))).scalars().all()

    assert lead is not None and lead.status == LeadStatusDB.ENRICHED
    assert enrichment is not None
    assert enrichment.status == EnrichmentStatusDB.OK
    assert enrichment.storefront_platform is None
    assert [contact.email for contact in contacts] == ["kontakt@a.pl"]
    assert "Produkujemy meble" in enrichment.context["text_excerpt"]
    assert "macie" not in enrichment.context["text_excerpt"]
    assert len(events) == 1 and events[0].action == "enriched"


async def test_no_contact_marks_skipped(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 1)
    lead_id = await make_lead("a.pl", domain="a.pl")
    fetcher = FakeFetcher({"https://a.pl/": snap("https://a.pl/", HOMEPAGE)})

    stats = await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)

    assert stats.no_contact == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
    assert lead is not None and lead.status == LeadStatusDB.SKIPPED
    assert enrichment is not None and enrichment.status == EnrichmentStatusDB.NO_CONTACT


async def test_blocked_site_is_terminal(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 1)
    lead_id = await make_lead("blocked.pl", domain="blocked.pl")
    fetcher = FakeFetcher({"https://blocked.pl/": snap("https://blocked.pl/", "nope", status=403)})

    stats = await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)

    assert stats.blocked == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
    assert lead is not None and lead.status == LeadStatusDB.SKIPPED
    assert enrichment is not None and enrichment.status == EnrichmentStatusDB.BLOCKED


async def test_transient_error_retries_until_max_attempts(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 1)
    lead_id = await make_lead("slow.pl", domain="slow.pl")
    fetcher = FakeFetcher(
        {"https://slow.pl/": snap("https://slow.pl/", "", status=None, error="timeout")}
    )

    first = await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
    assert first.failed == 1
    assert lead is not None and lead.status == LeadStatusDB.NEW

    await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)
    third = await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
    assert third.failed == 1
    assert lead is not None and lead.status == LeadStatusDB.SKIPPED
    assert enrichment is not None and enrichment.attempts == 3


async def test_rerun_performs_zero_fetches(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 2)
    lead_id = await make_lead("a.pl", domain="a.pl")
    fetcher = happy_fetcher()
    await enrich_pending(lead_id=lead_id, fetcher=fetcher, settings=settings)
    calls = list(fetcher.calls)

    stats = await enrich_pending(fetcher=fetcher, settings=settings)

    assert stats.scanned == 0
    assert fetcher.calls == calls


async def test_force_never_downgrades_enriched_lead(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 2)
    lead_id = await make_lead("a.pl", domain="a.pl")
    await enrich_pending(lead_id=lead_id, fetcher=happy_fetcher(), settings=settings)

    failing = FakeFetcher(
        {"https://a.pl/": snap("https://a.pl/", "", status=None, error="timeout")}
    )
    await enrich_pending(lead_id=lead_id, force=True, fetcher=failing, settings=settings)

    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
        contacts = (await session.execute(select(Contact))).scalars().all()
    assert lead is not None and lead.status == LeadStatusDB.ENRICHED
    assert enrichment is not None and enrichment.status == EnrichmentStatusDB.OK
    assert [contact.email for contact in contacts] == ["kontakt@a.pl"]


async def test_unresolved_domain_is_no_site_once(db, monkeypatch) -> None:
    lead_id = await make_lead(
        "olx:404",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {}},
        url="https://www.olx.pl/d/oferta/404-ID404.html",
    )

    stats = await enrich_pending(lead_id=lead_id, fetcher=FakeFetcher({}), settings=settings)

    assert stats.no_site == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
    assert lead is not None and lead.status == LeadStatusDB.SKIPPED
    assert enrichment is not None and enrichment.status == EnrichmentStatusDB.NO_SITE


async def test_resolve_only_records_domain_without_touching_lead(db, monkeypatch) -> None:
    lead_id = await make_lead(
        "olx:38",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama"}},
        url="https://www.olx.pl/d/oferta/x-ID1.html",
    )
    fetcher = FakeFetcher(
        {
            "https://regama.olx.pl": snap(
                "https://regama.olx.pl", '<a href="https://regama.pl">Strona www</a>'
            )
        }
    )

    stats = await enrich_pending(
        source="olx", resolve_only=True, fetcher=fetcher, settings=settings
    )

    assert stats.resolved == 1
    async with session_scope() as session:
        lead = await session.get(Lead, lead_id)
        enrichment = await EnrichmentRepo(session).get(lead_id)
    assert lead is not None
    assert lead.domain is None
    assert lead.status == LeadStatusDB.NEW
    assert enrichment is not None
    assert enrichment.domain == "regama.pl"
    assert enrichment.domain_source == "shop_page"
    assert enrichment.status is None


async def test_resolve_only_reports_overrides_and_duplicates(db, monkeypatch, tmp_path) -> None:
    first = await make_lead(
        "olx:38",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama"}},
        url="https://www.olx.pl/d/oferta/1-ID1.html",
    )
    second = await make_lead(
        "olx:39",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama2"}},
        url="https://www.olx.pl/d/oferta/2-ID2.html",
    )
    third = await make_lead(
        "olx:40",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama3"}},
        url="https://www.olx.pl/d/oferta/3-ID3.html",
    )
    overrides = tmp_path / "overrides.csv"
    overrides.write_text(f"lead_id,domain,note\n{first},override.pl,test\n", encoding="utf-8")
    monkeypatch.setattr(settings, "enrich_domain_overrides", overrides)
    fetcher = FakeFetcher(
        {
            "https://regama.olx.pl": snap(
                "https://regama.olx.pl", '<a href="https://same.pl">Strona www</a>'
            ),
            "https://regama2.olx.pl": snap(
                "https://regama2.olx.pl", '<a href="https://same.pl">Strona www</a>'
            ),
            "https://regama3.olx.pl": snap(
                "https://regama3.olx.pl", '<a href="https://same.pl">Strona www</a>'
            ),
        }
    )

    stats = await enrich_pending(
        source="olx", resolve_only=True, fetcher=fetcher, settings=settings
    )

    assert stats.resolved == 3
    assert stats.overridden == 1
    assert stats.duplicates == 1
    async with session_scope() as session:
        first_enrichment = await EnrichmentRepo(session).get(first)
        second_enrichment = await EnrichmentRepo(session).get(second)
        third_enrichment = await EnrichmentRepo(session).get(third)
    assert first_enrichment is not None and first_enrichment.domain == "override.pl"
    assert second_enrichment is not None and second_enrichment.domain == "same.pl"
    assert third_enrichment is not None and third_enrichment.domain == "same.pl"


async def test_duplicate_resolved_domain_is_not_crawled_twice(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "enrich_max_pages", 1)
    first = await make_lead(
        "olx:38",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama"}},
        url="https://www.olx.pl/d/oferta/1-ID1.html",
    )
    second = await make_lead(
        "olx:39",
        domain=None,
        source=LeadSource.OLX,
        raw={"ad": {"shop_subdomain": "regama2"}},
        url="https://www.olx.pl/d/oferta/2-ID2.html",
    )
    fetcher = FakeFetcher(
        {
            "https://regama.olx.pl": snap(
                "https://regama.olx.pl", '<a href="https://same.pl">Strona www</a>'
            ),
            "https://regama2.olx.pl": snap(
                "https://regama2.olx.pl", '<a href="https://same.pl">Strona www</a>'
            ),
            "https://same.pl/": snap(
                "https://same.pl/",
                HOMEPAGE.replace("</body>", '<a href="mailto:kontakt@same.pl">napisz</a></body>'),
            ),
        }
    )

    stats = await enrich_pending(source="olx", fetcher=fetcher, settings=settings)

    assert stats.duplicates == 1
    assert stats.enriched == 1
    assert stats.no_site == 1
    assert fetcher.calls.count("https://same.pl/") == 1
    async with session_scope() as session:
        first_lead = await session.get(Lead, first)
        second_lead = await session.get(Lead, second)
    assert first_lead is not None and first_lead.domain == "same.pl"
    assert second_lead is not None and second_lead.domain is None
    assert second_lead.status == LeadStatusDB.SKIPPED
