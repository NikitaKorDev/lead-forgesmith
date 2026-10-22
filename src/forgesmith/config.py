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
    shopify_seeds_file: Path = PROJECT_ROOT / "data" / "shopify_seeds.txt"
    shopify_products_limit: int = 250

    # --- sources: olx ----------------------------------------------------
    olx_base_url: str = "https://www.olx.pl"
    olx_max_pages: int = 3

    # --- llm (phase 3) ---------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout: float = 120.0

    # --- mailer (phase 4) ------------------------------------------------
    mail_dry_run: bool = True
    mail_daily_cap: int = 20
    mail_delay_mean: float = 540.0  # seconds between sends (Gaussian mean)
    mail_delay_sigma: float = 180.0
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    mail_from_name: str = ""
    mail_from_address: str = ""

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


settings = Settings()
