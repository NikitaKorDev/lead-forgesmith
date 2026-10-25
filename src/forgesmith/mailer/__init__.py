"""Phase 4: approval, dry-run ``.eml`` and capped SMTP delivery.

Exports are lazy so ``forgesmith.db.repo`` can import the value objects without
triggering the runner (which imports the repo back).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from forgesmith.mailer.models import (
        ApprovalRefused,
        MailerError,
        SendConfigError,
        SendMode,
        SendRefused,
        SendStats,
        SendStatus,
    )
    from forgesmith.mailer.runner import SendRunner, send_queued

__all__ = [
    "ApprovalRefused",
    "MailerError",
    "SendConfigError",
    "SendMode",
    "SendRefused",
    "SendRunner",
    "SendStats",
    "SendStatus",
    "send_queued",
]

_LAZY = {
    "ApprovalRefused": "forgesmith.mailer.models",
    "MailerError": "forgesmith.mailer.models",
    "SendConfigError": "forgesmith.mailer.models",
    "SendMode": "forgesmith.mailer.models",
    "SendRefused": "forgesmith.mailer.models",
    "SendStats": "forgesmith.mailer.models",
    "SendStatus": "forgesmith.mailer.models",
    "SendRunner": "forgesmith.mailer.runner",
    "send_queued": "forgesmith.mailer.runner",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(name)
    from importlib import import_module

    module = import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
