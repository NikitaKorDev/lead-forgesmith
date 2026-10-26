from __future__ import annotations

from pathlib import Path

from forgesmith.config import Settings, settings


def test_data_dir_relative_defaults(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORGESMITH_DATA_DIR", str(tmp_path))
    config = Settings(_env_file=None)

    assert config.data_dir == tmp_path
    assert config.seeds_file == tmp_path / "shopify_seeds.txt"
    assert config.overrides_file == tmp_path / "domain_overrides.csv"
    assert config.backups_dir == tmp_path / "backups"
    assert config.outbox_dir == tmp_path / "outbox"


def test_explicit_seed_and_override_paths_still_win(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORGESMITH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FORGESMITH_SHOPIFY_SEEDS_FILE", str(tmp_path / "custom.txt"))
    monkeypatch.setenv("FORGESMITH_ENRICH_DOMAIN_OVERRIDES", str(tmp_path / "custom.csv"))
    config = Settings(_env_file=None)

    assert config.seeds_file == tmp_path / "custom.txt"
    assert config.overrides_file == tmp_path / "custom.csv"


def test_overture_extract_defaults_under_the_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORGESMITH_DATA_DIR", str(tmp_path))
    config = Settings(_env_file=None)

    assert config.overture_extract is None
    assert config.overture_extract_file == tmp_path / "extracts" / "overture-places.parquet"
    assert config.overture_country == "PL"


def test_explicit_overture_extract_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FORGESMITH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FORGESMITH_OVERTURE_EXTRACT", str(tmp_path / "elsewhere.parquet"))
    monkeypatch.setenv("FORGESMITH_OVERTURE_COUNTRY", "DE")
    config = Settings(_env_file=None)

    assert config.overture_extract == tmp_path / "elsewhere.parquet"
    assert config.overture_extract_file == tmp_path / "elsewhere.parquet"
    assert config.overture_country == "DE"


def test_ollama_url_is_explicit_for_containers(monkeypatch) -> None:
    monkeypatch.setenv("FORGESMITH_OLLAMA_BASE_URL", "http://host.docker.internal:11434")
    config = Settings(_env_file=None)

    assert config.ollama_base_url == "http://host.docker.internal:11434"
    assert settings is not config
