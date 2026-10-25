"""Build the prompt-safe fact payload and its canonical hash."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from forgesmith.compose.clean import (
    clean_description,
    clean_excerpt,
    clean_headings,
    clean_products,
    is_context_insufficient,
    legal_company_name,
    normalize_text,
    scrub_untrusted,
)
from forgesmith.compose.models import FactPayload
from forgesmith.db.models import Lead


def build_fact_payload(
    lead: Lead,
    contact_kind: str,
    *,
    max_excerpt_chars: int = 3000,
    max_description_chars: int = 800,
    max_headings: int = 12,
    max_products: int = 12,
    min_context_chars: int = 120,
) -> FactPayload:
    """Select only fields that are safe to show the model."""
    enrichment = lead.enrichment
    if enrichment is None:
        raise ValueError(f"lead {lead.id} has no enrichment")

    context = enrichment.context or {}
    description = clean_description(
        enrichment.description or context.get("description") or "",
        max_chars=max_description_chars,
    )
    excerpt = clean_excerpt(
        context.get("text_excerpt") or "",
        max_chars=max_excerpt_chars,
    )
    headings = clean_headings(context.get("headings") or [], limit=max_headings)
    products = clean_products(context.get("products") or [], limit=max_products)

    company = legal_company_name(enrichment.legal_name)
    if company is None and lead.source == "shopify":
        company = _shopify_name(lead.name)

    if is_context_insufficient(
        text_excerpt=excerpt,
        description=description,
        assortment_titles=products,
        min_chars=min_context_chars,
    ):
        # The caller checks this flag separately; keep the payload empty of
        # pseudo-facts so a bug cannot accidentally prompt with nothing.
        excerpt = ""

    return FactPayload(
        domain=lead.domain or "",
        source=lead.source,
        company_name=company,
        storefront_platform=enrichment.storefront_platform,
        description=description,
        text_excerpt=excerpt,
        headings=headings,
        assortment_titles=products,
        contact_kind=contact_kind,
    )


def payload_is_insufficient(payload: FactPayload, *, min_context_chars: int = 120) -> bool:
    return is_context_insufficient(
        text_excerpt=payload.text_excerpt,
        description=payload.description,
        assortment_titles=payload.assortment_titles,
        min_chars=min_context_chars,
    )


def payload_dict(payload: FactPayload) -> dict[str, Any]:
    return payload.canonical_dict()


def canonical_json(payload: FactPayload) -> str:
    return json.dumps(
        _normalize_value(payload.canonical_dict()),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def context_hash(payload: FactPayload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _shopify_name(raw: str | None) -> str | None:
    name = normalize_text(scrub_untrusted(raw or ""))
    if not 2 <= len(name) <= 80:
        return None
    if any(char.isdigit() for char in name) or "://" in name or "www." in name:
        return None
    if name.casefold() in {"sklep", "shop", "home", "strona główna"}:
        return None
    return name
