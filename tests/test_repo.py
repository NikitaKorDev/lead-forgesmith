from __future__ import annotations

from sqlalchemy import select

from forgesmith.db.models import Contact, Lead, LeadStatusDB
from forgesmith.db.repo import LeadRepo
from forgesmith.db.session import session_scope
from forgesmith.domain import LeadCandidate, LeadSource


def shopify_candidate(domain: str, name: str = "Sklep") -> LeadCandidate:
    return LeadCandidate.from_site(
        source=LeadSource.SHOPIFY,
        source_id=domain,
        name=name,
        site_url=f"https://{domain}/",
        category="Meble",
    )


async def test_upsert_inserts_and_counts(db) -> None:
    async with session_scope() as session:
        repo = LeadRepo(session)
        stats = await repo.upsert_many([shopify_candidate("a.pl"), shopify_candidate("b.pl")])
        assert (stats.inserted, stats.updated, stats.duplicates) == (2, 0, 0)
        assert await repo.count() == 2
        assert await repo.status_counts() == {"new": 2}
        assert await repo.source_counts() == {"shopify": 2}


async def test_upsert_is_idempotent_and_refreshes_new_leads(db) -> None:
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([shopify_candidate("a.pl", "Stara nazwa")])

    async with session_scope() as session:
        stats = await LeadRepo(session).upsert_many([shopify_candidate("a.pl", "Nowa nazwa")])

    assert (stats.inserted, stats.updated) == (0, 1)

    async with session_scope() as session:
        lead = await LeadRepo(session).by_domain("a.pl")
        assert lead is not None
        assert lead.name == "Nowa nazwa"


async def test_second_marketplace_lead_for_same_domain_is_duplicate(db) -> None:
    async with session_scope() as session:
        await LeadRepo(session).upsert_many([shopify_candidate("a.pl")])

    other = LeadCandidate(
        source=LeadSource.OLX,
        source_id="olx:1",
        name="Inny sprzedawca",
        url="https://www.olx.pl/d/oferta/x-ID1.html",
        domain="a.pl",
    )
    async with session_scope() as session:
        stats = await LeadRepo(session).upsert_many([other])

    assert (stats.inserted, stats.duplicates) == (0, 1)


async def test_duplicate_inside_single_batch(db) -> None:
    first = shopify_candidate("a.pl", "Pierwszy")
    second = LeadCandidate(source=LeadSource.OLX, source_id="olx:9", name="Drugi", domain="a.pl")
    async with session_scope() as session:
        stats = await LeadRepo(session).upsert_many([first, second])
    assert (stats.inserted, stats.duplicates) == (1, 1)


async def test_leads_without_domain_are_all_kept(db) -> None:
    leads = [
        LeadCandidate(source=LeadSource.OLX, source_id=f"olx:{i}", name=f"Sprzedawca {i}")
        for i in range(3)
    ]
    async with session_scope() as session:
        stats = await LeadRepo(session).upsert_many(leads)
    assert stats.inserted == 3


async def test_sent_leads_are_not_overwritten(db) -> None:
    async with session_scope() as session:
        repo = LeadRepo(session)
        await repo.upsert_many([shopify_candidate("a.pl", "Stara nazwa")])
        lead = await repo.by_domain("a.pl")
        assert lead is not None
        lead.status = LeadStatusDB.SENT

    async with session_scope() as session:
        stats = await LeadRepo(session).upsert_many([shopify_candidate("a.pl", "Nowa nazwa")])
        assert stats.updated == 0
        lead = await LeadRepo(session).by_domain("a.pl")
        assert lead is not None
        assert lead.name == "Stara nazwa"


async def test_relations_and_filters(db) -> None:
    async with session_scope() as session:
        repo = LeadRepo(session)
        await repo.upsert_many([shopify_candidate("a.pl"), shopify_candidate("b.pl")])
        lead = await repo.by_domain("a.pl")
        assert lead is not None
        session.add(Contact(lead_id=lead.id, email="kontakt@a.pl", kind="company"))
        session.add(Contact(lead_id=lead.id, email="biuro@a.pl"))

    async with session_scope() as session:
        rows = await LeadRepo(session).list(limit=10)
        assert len(rows) == 2
        assert isinstance(rows[0], Lead)
        contacts = (await session.execute(select(Contact))).scalars().all()
        assert len(contacts) == 2
        assert {c.email for c in contacts} == {"kontakt@a.pl", "biuro@a.pl"}
        filtered = await LeadRepo(session).list(limit=10, status="enriched")
        assert filtered == []
