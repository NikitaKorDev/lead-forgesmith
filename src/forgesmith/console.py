"""Console setup: make Windows terminals speak UTF-8.

Polish lead names (``Łukasz``) and category names crash a cp1252 console with
``UnicodeEncodeError``. The Windows console itself handles UTF-8 once the
streams are reconfigured; ``errors="replace"`` keeps output flowing when a
stream cannot be changed (pipes, capture harnesses, closed handles).
"""

from __future__ import annotations

import sys


def force_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8, ignoring streams that cannot change."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
