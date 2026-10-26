"""Cross-cutting configuration. Every knob is overridable via ``.env``."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FORGESMITH_",
        extra="ignore",
    )

    # --- storage ---------------------------------------------------------
    database_url: str = (
        f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'data' / 'forgesmith.db').as_posix()}"
    )
    data_dir: Path = PROJECT_ROOT / "data"

    # --- http ------------------------------------------------------------
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    request_timeout: float = 20.0
    request_delay: float = 1.5
    max_concurrency: int = 5
    max_retries: int = 2

    # --- sources: shopify ------------------------------------------------
    # ``None`` means "use data_dir/shopify_seeds.txt", so a container that
    # mounts its data elsewhere does not need to remap every path.
    shopify_seeds_file: Path | None = None
    shopify_products_limit: int = 250

    # --- sources: olx ----------------------------------------------------
    olx_base_url: str = "https://www.olx.pl"
    olx_max_pages: int = 3

    # --- sources: overture -----------------------------------------------
    # A local extract, not a crawl: the operator downloads the parquet once
    # (see ops/README.md). ``None`` means "use
    # data_dir/extracts/overture-places.parquet", so a container that mounts
    # its data elsewhere does not need to remap every path.
    overture_extract: Path | None = None
    overture_country: str = "PL"

    # --- enrichment (phase 2) --------------------------------------------
    enrich_max_pages: int = Field(default=5, ge=1)
    enrich_max_attempts: int = Field(default=3, ge=1)
    enrich_lead_timeout: float = Field(default=60.0, gt=0)
    enrich_page_timeout: float = Field(default=10.0, gt=0)
    enrich_max_bytes: int = Field(default=2_000_000, gt=0)
    enrich_text_chars: int = Field(default=3000, ge=0)
    enrich_email_limit: int = Field(default=3, ge=1)
    enrich_browser_settle_ms: int = Field(default=3000, ge=0)
    enrich_respect_robots: bool = True
    # ``None`` means "use data_dir/domain_overrides.csv" (see above).
    enrich_domain_overrides: Path | None = None

    # --- llm (phase 3) ---------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout: float = 120.0
    compose_temperature: float = Field(default=0.4, ge=0.0, le=2.0)
    compose_num_predict: int = Field(default=600, ge=1)
    compose_max_attempts: int = Field(default=3, ge=1)
    compose_body_min_words: int = Field(default=80, ge=1)
    compose_body_max_words: int = Field(default=140, ge=1)
    compose_min_context_chars: int = Field(default=120, ge=0)

    # Sender identity is shared with Phase 4's SMTP envelope.  The offer is
    # what the model is allowed to pitch; empty values make ``compose`` fail
    # fast instead of generating placeholder copy.
    sender_company: str = ""
    sender_offer: str = ""
    sender_portfolio_url: str = ""

    # --- mailer (phase 4) ------------------------------------------------
    # ``mail_dry_run`` is the unattended kill switch: an explicit ``--live`` run
    # requested while it is true sends nothing. Dry runs still work.
    mail_dry_run: bool = True
    mail_daily_cap: int = Field(default=20, ge=0)
    mail_delay_mean: float = 540.0  # seconds between sends (Gaussian mean)
    mail_delay_sigma: float = 180.0
    mail_delay_min: float = Field(default=120.0, ge=0.0)
    mail_delay_max: float = Field(default=1800.0, ge=0.0)
    mail_backoff_base: float = Field(default=300.0, ge=0.0)
    mail_backoff_cap: float = Field(default=3600.0, ge=0.0)
    mail_window_start: int = Field(default=9, ge=0, le=23)
    mail_window_end: int = Field(default=17, ge=1, le=24)
    mail_window_tz: str = "Europe/Warsaw"
    mail_weekdays_only: bool = True
    mail_max_attempts: int = Field(default=3, ge=1)
    mail_allowlist: str = ""
    # Comma-separated ISO-3166-1 alpha-2 codes the operator is cleared to mail.
    # Unlike ``mail_allowlist`` (where empty means "everyone"), an empty value
    # here refuses every country: "empty" must never be read as the whole world.
    # It is empty by default on purpose: the operator opts in to each country
    # after checking its rules. Poland is refused by the outreach gate even when
    # listed, because Article 398 PKE requires prior consent for B2B email.
    mail_countries: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_ssl: bool = False  # 465 implicit TLS
    smtp_starttls: bool = True  # 587 STARTTLS
    smtp_timeout: float = Field(default=30.0, gt=0)
    mail_from_name: str = ""
    mail_from_address: str = ""

    # --- inbox (phase 4.5) -----------------------------------------------
    # Read-only IMAP ingest. Credentials are usually the same mailbox the SMTP
    # side sends from; an empty host makes ``inbox sync`` fail fast.
    imap_host: str = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_user: str = ""
    imap_password: str = Field(default="", repr=False)
    imap_ssl: bool = True
    imap_starttls: bool = False
    imap_timeout: float = Field(default=30.0, gt=0)
    imap_mailbox: str = "INBOX"
    imap_lookback_days: int = Field(default=30, ge=1)
    imap_fetch_limit: int = Field(default=200, ge=1)
    imap_max_body_bytes: int = Field(default=262_144, gt=0)

    # --- phase 5: containers / unattended operation ----------------------
    # ``mail_dry_run`` doubles as the unattended kill switch: a live run
    # requested while it is true does nothing and exits 0.
    log_json: bool = False
    browser_no_sandbox: bool = False
    approval_channel: str = ""  # "unattended" makes ``approve`` refuse
    send_max_runtime: float = Field(default=0.0, ge=0.0)
    db_busy_timeout_ms: int = Field(default=5000, ge=0)
    stage_stale_after: float = Field(default=21600.0, gt=0)

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def mailer_lock_path(self) -> Path:
        return self.data_dir / "mailer.lock"

    @property
    def inbox_lock_path(self) -> Path:
        return self.data_dir / "inbox.lock"

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def seeds_file(self) -> Path:
        return self.shopify_seeds_file or self.data_dir / "shopify_seeds.txt"

    @property
    def overrides_file(self) -> Path:
        return self.enrich_domain_overrides or self.data_dir / "domain_overrides.csv"

    @property
    def overture_extract_file(self) -> Path:
        return self.overture_extract or self.data_dir / "extracts" / "overture-places.parquet"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
