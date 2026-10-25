"""``python -m forgesmith`` — same entry point as the ``forgesmith`` script.

The GUI drives stages as one-shot subprocesses, so it needs a module entry point
that works with the interpreter running the app (``sys.executable``) instead of
relying on a ``Scripts`` shim being on ``PATH``.
"""

from __future__ import annotations

from forgesmith.cli import app

if __name__ == "__main__":  # pragma: no cover - process entry point
    app()
