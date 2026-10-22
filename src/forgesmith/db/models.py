"""SQLAlchemy models. Portable across SQLite (dev) and Postgres (prod)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class LeadStatusDB(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    COMPOSED = "composed"
    QUEUED = "queued"
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    SUPPRESSED = "suppressed"
    SKIPPED = "skipped"


class EnrichmentStatusDB(StrEnum):
    """Terminal outcomes of one crawl. ``None`` means "resolved, not crawled"."""

    OK = "ok"
    NO_SITE = "no_site"
    NO_CONTACT = "no_contact"
    BLOCKED = "blocked"
    ERROR = "error"


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_leads_source_source_id"),
        Index("ix_leads_domain", "domain"),
        Index("ix_leads_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    domain: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(64))
    platform: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=LeadStatusDB.NEW, nullable=False)
    raw: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    contacts: Mapped[list[Contact]] = relationship(
        back_populates="lead", cascade="all, delete-orphan"
    )
    enrichment: Mapped[Enrichment | None] = relationship(
        back_populates="lead", uselist=False, cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Lead {self.id} {self.source}:{self.source_id} {self.name!r}>"


class Enrichment(Base):
    """Enrichment state, kept out of ``Lead.raw`` (sources replace that wholesale)."""

    __tablename__ = "enrichments"
    __table_args__ = (UniqueConstraint("lead_id", name="uq_enrichments_lead_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # None only while a --resolve-only row has not been crawled yet.
    status: Mapped[str | None] = mapped_column(String(32))
    domain: Mapped[str | None] = mapped_column(String(255))
    domain_source: Mapped[str] = mapped_column(String(32), default="none", nullable=False)
    site_url: Mapped[str | None] = mapped_column(String(2048))
    storefront_platform: Mapped[str | None] = mapped_column(String(64))
    legal_name: Mapped[str | None] = mapped_column(String(512))
    nip: Mapped[str | None] = mapped_column(String(16))
    description: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    engine: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lead: Mapped[Lead] = relationship(back_populates="enrichment")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Enrichment lead={self.lead_id} status={self.status!r}>"


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("lead_id", "email", name="uq_contacts_lead_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="company")
    source_url: Mapped[str | None] = mapped_column(String(2048))
    confidence: Mapped[float] = mapped_column(default=0.5)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lead: Mapped[Lead] = relationship(back_populates="contacts")


class Message(Base):
    """A generated pitch. Kept even after sending for reply-rate analysis."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    hooks: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Send(Base):
    __tablename__ = "sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), nullable=False)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="dry_run")
    smtp_message_id: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Suppression(Base):
    """Hard opt-out list. Checked before every single send."""

    __tablename__ = "suppressions"
    __table_args__ = (UniqueConstraint("value", name="uq_suppressions_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="email")
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Event(Base):
    """Append-only audit trail: who changed what, when, and why."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
