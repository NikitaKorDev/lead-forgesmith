"""Overture Maps places: the businesses no marketplace lists.

Every marketplace payload carries a URL, so those sources hand the pipeline a
domain for free. A shop that only exists as a Google Maps pin never reaches us,
and that is most of the local B2B market. Overture's open places theme fixes the
input side of that gap: it is a free, bulk-downloadable GeoParquet file with
names, categories, addresses and — the column this pipeline needs — ``websites``
for tens of millions of businesses.

A download, not a crawl: the operator fetches one release once (see
``ops/README.md``) and this source reads it locally with duckdb, so there is no
rate limit, no request budget and no network at run time.

The places schema moves between releases. ``categories`` was replaced by
``basic_category`` and ``taxonomy`` in the September 2026 release, and fields
come and go inside nested structs. The projection is therefore built from
``DESCRIBE`` rather than hard-coded: a renamed column costs that one field, not
the whole stage.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import structlog

from forgesmith.blocklist import usable_host
from forgesmith.domain import LeadCandidate, LeadSource, is_freemail, parse_domain
from forgesmith.outreach import policy_for
from forgesmith.sources.base import BaseSource

log = structlog.get_logger(__name__)

#: Rows per hand-off from the worker thread. Large enough that the hop costs
#: nothing, small enough that a country-sized extract never sits in memory.
_CHUNK_ROWS = 500
_PERMANENTLY_CLOSED = "permanently_closed"

#: alias, SQL expression, path that must exist in the parquet schema. A path is
#: ``(column,)`` for a scalar and ``(column, field, ...)`` for a struct field.
_COLUMNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("place_id", '"id"', ("id",)),
    ("name", '"names"."primary"', ("names", "primary")),
    ("taxonomy_primary", '"taxonomy"."primary"', ("taxonomy", "primary")),
    ("hierarchy", '"taxonomy"."hierarchy"', ("taxonomy", "hierarchy")),
    ("alternates", '"taxonomy"."alternates"', ("taxonomy", "alternates")),
    ("categories_primary", '"categories"."primary"', ("categories", "primary")),
    (
        "alternate_categories",
        '"categories"."alternate_categories"',
        ("categories", "alternate_categories"),
    ),
    ("basic_category", '"basic_category"', ("basic_category",)),
    ("websites", '"websites"', ("websites",)),
    ("phones", '"phones"', ("phones",)),
    ("emails", '"emails"', ("emails",)),
    ("socials", '"socials"', ("socials",)),
    ("confidence", '"confidence"', ("confidence",)),
    ("operating_status", '"operating_status"', ("operating_status",)),
    ("brand_name", '"brand"."names"."primary"', ("brand", "names", "primary")),
    ("addresses", '"addresses"', ("addresses",)),
)


class OvertureSource(BaseSource):
    """Local-extract source: it never asks ``self.client`` for a page."""

    name = LeadSource.OVERTURE

    def __init__(
        self,
        *args: object,
        extract: Path,
        country: str,
        categories: Sequence[str] = (),
        exclude_brands: Sequence[str] = (),
        min_confidence: float = 0.5,
        require_website: bool = True,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.extract = Path(extract).expanduser()
        # Bad input is an operator mistake, not a stage failure to retry: raise
        # before the CLI opens a run-ledger row for the stage.
        if not self.extract.is_file():
            raise FileNotFoundError(f"Overture extract not found: {self.extract}")
        self.country = _country_code(country)
        self.categories = [category.strip() for category in categories if category.strip()]
        self.exclude_brands = [brand.strip().casefold() for brand in exclude_brands if brand.strip()]
        self.min_confidence = _confidence(min_confidence)
        self.require_website = bool(require_website)
        self.drops: Counter[str] = Counter()
        if policy_for(self.country) is None:
            log.warning(
                "overture.unknown_country",
                country=self.country,
                detail="no outreach policy; the mailer will refuse these leads",
            )

    async def discover(self) -> AsyncIterator[LeadCandidate]:
        """Stream candidates from the extract without blocking the event loop.

        duckdb is synchronous and a connection is not safe to use from several
        threads, so every call goes to one dedicated worker thread and the
        event loop only converts rows and yields them. Chunked reads keep a
        country-sized extract out of memory.
        """
        self._emitted = 0
        self.drops.clear()
        seen: set[str] = set()
        considered = 0
        log.info(
            "overture.start",
            extract=str(self.extract),
            country=self.country,
            categories=self.categories,
            exclude_brands=self.exclude_brands,
            min_confidence=self.min_confidence,
            require_website=self.require_website,
            limit=self.limit,
        )
        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="overture") as pool:
                cursor = await loop.run_in_executor(pool, self._open_cursor)
                try:
                    while True:
                        rows = await loop.run_in_executor(pool, _fetch_chunk, cursor)
                        if not rows:
                            break
                        for row in rows:
                            considered += 1
                            candidate = self._to_candidate(row)
                            if candidate is None:
                                continue
                            if candidate.domain is None:
                                self.drops["no_website"] += 1
                                continue
                            if candidate.domain in seen:
                                self.drops["duplicate_domain"] += 1
                                continue
                            seen.add(candidate.domain)
                            self._emitted += 1
                            yield candidate
                            if not self._budget_left():
                                return
                finally:
                    await loop.run_in_executor(pool, cursor.close)
        finally:
            log.info(
                "overture.done",
                considered=considered,
                emitted=self._emitted,
                dropped=dict(self.drops),
            )

    # ------------------------------------------------------------- internals
    def _open_cursor(self) -> Any:
        """Inspect the schema, then run the filtered projection (worker thread)."""
        try:
            import duckdb
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise RuntimeError(
                "duckdb is required for the overture source: uv sync --extra sources"
            ) from exc

        connection = duckdb.connect()
        try:
            types = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [self.extract.as_posix()]
                ).fetchall()
            }
            sql, params = self._query(types)
            connection.execute(sql, [self.extract.as_posix(), *params])
        except BaseException:
            connection.close()
            raise
        return connection

    def _query(self, types: dict[str, str]) -> tuple[str, list[object]]:
        projection = [
            f"{expression} AS {alias}"
            for alias, expression, path in _COLUMNS
            if _has(types, path)
        ]
        conditions, params = self._filters(types)
        sql = f"SELECT {', '.join(projection)} FROM read_parquet(?)"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        return sql, params

    def _filters(self, types: dict[str, str]) -> tuple[list[str], list[object]]:
        conditions: list[str] = []
        params: list[object] = []
        if not _has(types, ("addresses", "country")):
            # Filtering is the only way to know a row's country, and country
            # drives the mail gate: guessing would mislabel the whole extract.
            raise ValueError(
                "Overture extract has no addresses.country field; cannot filter by country"
            )
        conditions.append('"addresses"[1]."country" = ?')
        params.append(self.country)

        if self.require_website:
            if _has(types, ("websites",)):
                conditions.append('"websites" IS NOT NULL AND len("websites") > 0')
            else:
                log.warning("overture.no_websites_column", extract=str(self.extract))

        if _has(types, ("confidence",)) and self.min_confidence > 0:
            # Unknown confidence is treated as 0, so a missing score never
            # passes a threshold the record cannot prove it meets.
            conditions.append('coalesce("confidence", 0) >= ?')
            params.append(self.min_confidence)

        if self.categories:
            category_conditions, category_params = self._category_conditions(types)
            if category_conditions:
                conditions.append("(" + " OR ".join(category_conditions) + ")")
                params.extend(category_params)
            else:
                log.warning("overture.no_category_columns", categories=self.categories)
        return conditions, params

    def _category_conditions(self, types: dict[str, str]) -> tuple[list[str], list[object]]:
        """Match every requested label against every category column this release has."""
        predicates: list[str] = []
        params: list[object] = []
        for category in self.categories:
            for path, expression in (
                (("taxonomy", "primary"), '"taxonomy"."primary" = ?'),
                (("taxonomy", "hierarchy"), 'list_contains("taxonomy"."hierarchy", ?)'),
                (("categories", "primary"), '"categories"."primary" = ?'),
                (
                    ("categories", "alternate_categories"),
                    'list_contains("categories"."alternate_categories", ?)',
                ),
                (("basic_category",), '"basic_category" = ?'),
            ):
                if _has(types, path):
                    predicates.append(expression)
                    params.append(category)
        return predicates, params

    def _to_candidate(self, row: dict[str, Any]) -> LeadCandidate | None:
        """Convert one row, recording why it was dropped when it cannot be a lead."""
        if _text(row.get("operating_status")) == _PERMANENTLY_CLOSED:
            self.drops["permanently_closed"] += 1
            return None

        name = _text(row.get("name"))
        if name is None:
            self.drops["no_name"] += 1
            return None

        brand = _text(row.get("brand_name"))
        if brand and self._brand_excluded(brand):
            # A chain contributes one row per branch; drafting each of them
            # would mail the same company a dozen near-identical pitches.
            self.drops["brand"] += 1
            return None

        website = self._website(row.get("websites"))
        if website is None:
            self.drops["no_website"] += 1
            return None

        address = _first_address(row.get("addresses"))
        category = (
            _text(row.get("taxonomy_primary"))
            or _text(row.get("categories_primary"))
            or _text(row.get("basic_category"))
        )
        phones = _texts(row.get("phones"))
        return LeadCandidate.from_site(
            source=self.name,
            source_id=f"overture:{self._place_id(row, name, website)}",
            name=name,
            site_url=website,
            city=_text(address.get("locality")),
            country=self.country,
            category=category,
            phone=phones[0] if phones else None,
            platform="overture",
            raw={
                "place": {
                    "address": address,
                    "categories": self._category_list(row, category),
                    "emails": _texts(row.get("emails")),
                    "socials": _texts(row.get("socials")),
                    "confidence": _number(row.get("confidence")),
                    "brand": brand,
                }
            },
        )

    def _brand_excluded(self, brand: str) -> bool:
        value = brand.casefold()
        return any(excluded in value for excluded in self.exclude_brands)

    def _website(self, value: object) -> str | None:
        """First website that could be a company storefront, not a profile page."""
        for raw in _texts(value):
            domain = parse_domain(raw)
            if domain and not is_freemail(domain) and usable_host(domain):
                return raw
        return None

    def _category_list(self, row: dict[str, Any], primary: str | None) -> list[str]:
        labels: list[str] = []
        for group in (
            row.get("hierarchy"),
            row.get("alternates"),
            row.get("alternate_categories"),
        ):
            labels.extend(_texts(group))
        if primary:
            labels.append(primary)
        unique: dict[str, None] = {}
        for label in labels:
            unique.setdefault(label, None)
        return list(unique)

    @staticmethod
    def _place_id(row: dict[str, Any], name: str, website: str) -> str:
        place_id = _text(row.get("place_id"))
        if place_id:
            return place_id
        # A release without ``id`` still has one place per name+site; a stable
        # fingerprint keeps the run idempotent instead of failing the stage.
        return hashlib.sha256(f"{name}|{website}".encode()).hexdigest()[:24]


def _country_code(value: str) -> str:
    code = value.strip().upper()
    if len(code) != 2 or not code.isascii() or not code.isalpha():
        raise ValueError(f"country must be a two-letter ISO 3166-1 code, got {value!r}")
    return code


def _confidence(value: float) -> float:
    score = float(value)
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"min_confidence must be between 0 and 1, got {value!r}")
    return score


def _fields(type_text: str) -> set[str]:
    """Field names inside a DuckDB type string, e.g. ``STRUCT("primary" VARCHAR)``."""
    return set(re.findall(r"\w+", type_text))


def _has(types: dict[str, str], path: tuple[str, ...]) -> bool:
    """True when the parquet schema carries ``column`` (and its struct ``field``)."""
    column, *nested = path
    type_text = types.get(column)
    if type_text is None:
        return False
    fields = _fields(type_text)
    return all(field in fields for field in nested)


def _fetch_chunk(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchmany(_CHUNK_ROWS)
    if not rows:
        return []
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in rows]


def _text(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _texts(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [text for text in (_text(item) for item in value) if text]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _first_address(value: object) -> dict[str, Any]:
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict) and any(item.values()):
                return dict(item)
    return {}
