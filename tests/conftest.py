from __future__ import annotations

import pytest
import pytest_asyncio

from forgesmith.config import settings
from forgesmith.db import init_db, reset_state


@pytest.fixture(autouse=True)
def _fast_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """No politeness delays in tests."""
    monkeypatch.setattr(settings, "request_delay", 0.0)
    monkeypatch.setattr(settings, "request_timeout", 5.0)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        settings, "database_url", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    )
    reset_state()
    await init_db()
    yield
    reset_state()
