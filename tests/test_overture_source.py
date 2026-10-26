"""Overture places source: extraction, drops and schema drift.

The fixtures are built with duckdb itself, so the tests need neither pyarrow nor
a committed binary and can model a release that dropped a column.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from forgesmith import cli
from forgesmith.db.models import Event, Lead
from forgesmith.db.session import session_scope
from forgesmith.sources.overture import OvertureSource

runner = CliRunner()

_FULL_SCHEMA = """
    id VARCHAR,
    names STRUCT("primary" VARCHAR),
    taxonomy STRUCT("primary" VARCHAR, hierarchy VARCHAR[], alternates VARCHAR[]),
    categories STRUCT("primary" VARCHAR, alternate_categories VARCHAR[]),
    basic_category VARCHAR,
    confidence DOUBLE,
    operating_status VARCHAR,
    websites VARCHAR[],
    phones VARCHAR[],
    emails VARCHAR[],
    socials VARCHAR[],
    brand STRUCT(names STRUCT("primary" VARCHAR), wikidata VARCHAR),
    addresses STRUCT(
        freeform VARCHAR,
        locality VARCHAR,
        postcode VARCHAR,
        region VARCHAR,
        country VARCHAR
    )[]
"""


def _write_parquet(
    path: Path, rows: list[dict[str, Any]], schema: str = _FULL_SCHEMA
) -> Path:
    """Materialize a places extract the way a release download would look."""
    connection = duckdb.connect()
    try:
        connection.execute(f"CREATE TABLE places ({schema})")
        for row in rows:
            columns = ", ".join(row)
            placeholders = ", ".join("?" for _ in row)
            connection.execute(
                f"INSERT INTO places ({columns}) VALUES ({placeholders})", list(row.values())
            )
        connection.execute(f"COPY places TO '{path.as_posix()}' (FORMAT parquet)")
    finally:
        connection.close()
    return path


def _place(
    place_id: str, name: str | None, *, websites: list[str] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": place_id,
        "names": {"primary": name} if name is not None else None,
        "confidence": 0.9,
        "operating_status": "open",
        "websites": websites,
        "addresses": [
            {
                "freeform": "ul. Testowa 1",
                "locality": "Warszawa",
                "postcode": "00-001",
                "region": "mazowieckie",
                "country": "PL",
            }
        ],
        **extra,
    }


@pytest.fixture()
def places(tmp_path: Path) -> Path:
    rows = [
        _place(
            "good-1",
            "Mebis",
            websites=["https://www.mebis.pl/"],
            taxonomy={
                "primary": "furniture_store",
                "hierarchy": ["shopping", "furniture_store"],
                "alternates": ["store"],
            },
            categories={
                "primary": "furniture_store",
                "alternate_categories": ["home_goods_store"],
            },
            basic_category="furniture_store",
            confidence=0.92,
            phones=["+48 22 123 45 67"],
            emails=["biuro@mebis.pl"],
            socials=["https://www.facebook.com/mebis"],
        ),
        _place(
            "closed-1",
            "Dawny Sklep",
            websites=["https://dawnysklep.pl/"],
            operating_status="permanently_closed",
        ),
        _place(
            "chain-1",
            "Salon Meble Warszawa",
            websites=["https://salonmeble.pl/"],
            brand={"names": {"primary": "Salon Meble"}, "wikidata": None},
        ),
        _place("fb-1", "Facebook Shop", websites=["https://www.facebook.com/mebis"]),
        _place("mail-1", "Gmail Biznes", websites=["https://gmail.com"]),
        _place("noweb-1", "Bez Strony"),
        _place("low-1", "Slabo Potwierdzony", websites=["https://slabo.pl/"], confidence=0.2),
        _place(
            "de-1",
            "Berlin Laden",
            websites=["https://berlinladen.de/"],
            addresses=[{"freeform": None, "locality": "Berlin", "country": "DE"}],
        ),
        _place("noname-1", None, websites=["https://noname.pl/"]),
    ]
    return _write_parquet(tmp_path / "places.parquet", rows)


def _source(path: Path, **kwargs: Any) -> OvertureSource:
    return OvertureSource(cast(Any, None), extract=path, country="PL", **kwargs)


async def _collect(source: OvertureSource) -> list[Any]:
    return [candidate async for candidate in source.discover()]


async def test_discover_emits_a_usable_place(places: Path) -> None:
    source = _source(places, exclude_brands=["Salon Meble"])
    candidates = await _collect(source)

    assert [candidate.source_id for candidate in candidates] == ["overture:good-1"]
    lead = candidates[0]
    assert lead.source == "overture"
    assert lead.name == "Mebis"
    assert lead.domain == "mebis.pl"
    assert lead.url == "https://mebis.pl/"
    assert lead.city == "Warszawa"
    assert lead.country == "PL"
    assert lead.category == "furniture_store"
    assert lead.phone == "+48 22 123 45 67"
    assert lead.platform == "overture"
    assert lead.raw["place"]["address"]["locality"] == "Warszawa"
    assert lead.raw["place"]["categories"] == [
        "shopping",
        "furniture_store",
        "store",
        "home_goods_store",
    ]
    assert lead.raw["place"]["emails"] == ["biuro@mebis.pl"]
    assert lead.raw["place"]["socials"] == ["https://www.facebook.com/mebis"]
    assert lead.raw["place"]["confidence"] == pytest.approx(0.92)
    assert lead.raw["place"]["brand"] is None
    # Closed, chain-branded and nameless rows survive the SQL filters, so their
    # reasons are counted where the operator can see them. The Facebook page and
    # the freemail address pass the "has a website" filter but cannot be mailed,
    # so they are counted under the same reason as a place without a site.
    assert source.drops == {
        "brand": 1,
        "no_name": 1,
        "no_website": 2,
        "permanently_closed": 1,
    }


async def test_websites_that_are_not_storefronts_are_counted(places: Path) -> None:
    source = _source(places, exclude_brands=["Salon Meble"], require_website=False)
    candidates = await _collect(source)

    assert [candidate.source_id for candidate in candidates] == ["overture:good-1"]
    # A Facebook page, a freemail address and no website at all: none of them
    # can become an outbound target, and all three share one reason.
    assert source.drops["no_website"] == 3


async def test_limit_is_honoured(tmp_path: Path) -> None:
    path = _write_parquet(
        tmp_path / "many.parquet",
        [
            _place("a", "Alfa", websites=["https://alfa.pl/"]),
            _place("b", "Beta", websites=["https://beta.pl/"]),
            _place("c", "Gamma", websites=["https://gamma.pl/"]),
        ],
    )

    candidates = await _collect(_source(path, limit=2))

    assert len(candidates) == 2
    assert len({candidate.domain for candidate in candidates}) == 2


async def test_one_lead_per_domain(tmp_path: Path) -> None:
    path = _write_parquet(
        tmp_path / "branches.parquet",
        [
            _place("branch-1", "Mebis Warszawa", websites=["https://mebis.pl/"]),
            _place("branch-2", "Mebis Krakow", websites=["https://mebis.pl/"]),
        ],
    )
    source = _source(path)

    candidates = await _collect(source)

    assert len(candidates) == 1
    assert source.drops == {"duplicate_domain": 1}


_LEGACY_SCHEMA = """
    id VARCHAR,
    names STRUCT("primary" VARCHAR),
    categories STRUCT("primary" VARCHAR, alternate_categories VARCHAR[]),
    confidence DOUBLE,
    websites VARCHAR[],
    addresses STRUCT(freeform VARCHAR, locality VARCHAR, country VARCHAR)[]
"""


async def test_a_release_without_phones_taxonomy_or_brand_still_emits(tmp_path: Path) -> None:
    path = _write_parquet(
        tmp_path / "legacy.parquet",
        [
            {
                "id": "legacy-1",
                "names": {"primary": "Stary Schemat"},
                "categories": {
                    "primary": "furniture_store",
                    "alternate_categories": ["store"],
                },
                "confidence": 0.9,
                "websites": ["https://staryschemat.pl/"],
                "addresses": [{"freeform": "ul. Stara 1", "locality": "Krakow", "country": "PL"}],
            }
        ],
        schema=_LEGACY_SCHEMA,
    )

    candidates = await _collect(_source(path, categories=["furniture_store"]))

    assert [candidate.source_id for candidate in candidates] == ["overture:legacy-1"]
    lead = candidates[0]
    assert lead.phone is None
    assert lead.city == "Krakow"
    assert lead.category == "furniture_store"
    assert lead.raw["place"]["categories"] == ["store", "furniture_store"]
    assert lead.raw["place"]["emails"] == []
    assert lead.raw["place"]["socials"] == []
    assert lead.raw["place"]["brand"] is None


async def test_a_renamed_website_column_degrades_instead_of_crashing(tmp_path: Path) -> None:
    """The realistic drift: every row loses its website, the stage still finishes."""
    schema = """
        id VARCHAR,
        names STRUCT("primary" VARCHAR),
        confidence DOUBLE,
        website VARCHAR,
        addresses STRUCT(freeform VARCHAR, locality VARCHAR, country VARCHAR)[]
    """
    path = _write_parquet(
        tmp_path / "renamed.parquet",
        [
            {
                "id": "renamed-1",
                "names": {"primary": "Renamed"},
                "confidence": 0.9,
                "website": "https://renamed.pl/",
                "addresses": [{"locality": "Poznan", "country": "PL"}],
            }
        ],
        schema=schema,
    )
    source = _source(path)

    assert await _collect(source) == []
    assert source.drops == {"no_website": 1}


async def test_an_extract_without_a_country_field_is_refused(tmp_path: Path) -> None:
    """Country drives the mail gate, so it is never guessed."""
    schema = """
        id VARCHAR,
        names STRUCT("primary" VARCHAR),
        websites VARCHAR[],
        addresses STRUCT(freeform VARCHAR, locality VARCHAR)[]
    """
    path = _write_parquet(
        tmp_path / "no-country.parquet",
        [{"id": "x-1", "names": {"primary": "X"}, "websites": ["https://x.pl/"]}],
        schema=schema,
    )

    with pytest.raises(ValueError, match=r"addresses\.country"):
        await _collect(_source(path))


def _stub(tmp_path: Path) -> Path:
    """An existing file that is never queried: eager validation only stats it."""
    path = tmp_path / "places.parquet"
    path.write_bytes(b"PAR1")
    return path


def test_a_missing_extract_fails_eagerly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        _source(tmp_path / "missing.parquet")


@pytest.mark.parametrize("code", ["", "POL", "P1", "p l"])
def test_a_bad_country_code_fails_eagerly(tmp_path: Path, code: str) -> None:
    with pytest.raises(ValueError, match="two-letter"):
        OvertureSource(cast(Any, None), extract=_stub(tmp_path), country=code)


@pytest.mark.parametrize("score", [-0.1, 1.5])
def test_an_impossible_confidence_threshold_fails_eagerly(tmp_path: Path, score: float) -> None:
    with pytest.raises(ValueError, match="min_confidence"):
        _source(_stub(tmp_path), min_confidence=score)


# ------------------------------------------------------------------------ CLI
def test_cli_source_overture_stores_leads_and_records_the_run(db, tmp_path: Path) -> None:
    path = _write_parquet(
        tmp_path / "places.parquet",
        [_place("good-1", "Mebis", websites=["https://www.mebis.pl/"])],
    )

    result = runner.invoke(
        cli.app, ["source", "overture", "--extract", str(path), "--country", "PL", "--limit", "5"]
    )

    assert result.exit_code == 0, result.output
    assert "overture: 1 new" in result.output

    async def stored() -> tuple[list[Lead], list[Event]]:
        async with session_scope() as session:
            leads = list((await session.execute(select(Lead))).scalars())
            runs = list(
                (await session.execute(select(Event).where(Event.entity == "run"))).scalars()
            )
        return leads, runs

    leads, runs = asyncio.run(stored())
    assert [(lead.source, lead.domain, lead.country) for lead in leads] == [
        ("overture", "mebis.pl", "PL")
    ]
    assert [event.detail["stage"] for event in runs] == ["overture"]


def test_cli_source_overture_refuses_a_missing_extract_before_the_ledger(
    db, tmp_path: Path
) -> None:
    result = runner.invoke(
        cli.app, ["source", "overture", "--extract", str(tmp_path / "nope.parquet")]
    )

    assert result.exit_code == 1
    assert "not found" in result.output

    async def runs() -> list[Event]:
        async with session_scope() as session:
            return list((await session.execute(select(Event))).scalars())

    assert asyncio.run(runs()) == []
