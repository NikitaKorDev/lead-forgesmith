"""Value objects for Phase 3 draft generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


@dataclass(slots=True)
class ComposeOutcome:
    """What happened to one lead in a compose run."""

    lead_id: int
    outcome: str  # drafted | skipped | failed
    reason: str | None = None


@dataclass(slots=True)
class ComposeStats:
    """Counters for one compose run; printed as the final CLI summary."""

    scanned: int = 0
    skipped: int = 0
    drafted: int = 0
    failed: int = 0
    duration_s: float = 0.0
    outcomes: list[ComposeOutcome] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FactPayload:
    """The cleaned, prompt-safe subset of a lead and its enrichment."""

    domain: str
    source: str
    company_name: str | None
    storefront_platform: str | None
    description: str
    text_excerpt: str
    headings: list[str]
    assortment_titles: list[str]
    contact_kind: str

    def trusted_facts(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "source": self.source,
            "company_name": self.company_name,
            "storefront_platform": self.storefront_platform,
            "contact_kind": self.contact_kind,
        }

    def untrusted_context(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "text_excerpt": self.text_excerpt,
            "headings": self.headings,
            "assortment_titles": self.assortment_titles,
        }

    def canonical_dict(self) -> dict[str, Any]:
        return {**self.trusted_facts(), **self.untrusted_context()}

    def grounding_text(self) -> str:
        """Text against which evidence and invented numbers are checked."""
        parts = [
            self.domain,
            self.company_name or "",
            self.storefront_platform or "",
            self.description,
            self.text_excerpt,
            *self.headings,
            *self.assortment_titles,
        ]
        return "\n".join(part for part in parts if part)


class DraftPayload(BaseModel):
    """The exact JSON object the model is asked to return."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class ValidationResult:
    """Hard errors, advisory warnings and measurements for one draft."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    word_count: int = 0
    subject_length: int = 0
    grounded_evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_hooks(self) -> dict[str, Any]:
        return {
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "word_count": self.word_count,
            "subject_length": self.subject_length,
        }


class ComposeError(Exception):
    """Base class for expected compose failures."""


class ComposeConfigError(ComposeError):
    """The sender profile is incomplete or invalid."""


class LLMError(ComposeError):
    """The provider could not produce a response."""


class DraftParseError(ComposeError):
    """The model response was not valid JSON or did not match the schema."""


class DraftValidationError(ComposeError):
    """The draft failed one or more hard gates."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        super().__init__(", ".join(result.errors) or "validation failed")
