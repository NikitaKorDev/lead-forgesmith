"""Qt desktop console for Forgesmith.

PyQt5 is an optional extra: the CLI and the pipeline stages never import this
package, so ``uv sync`` stays headless. Install the GUI with::

    uv sync --extra gui
    uv run forgesmith-gui
"""

from __future__ import annotations

INSTALL_HINT = (
    "PyQt5 is not installed.\n\n"
    "Install the desktop extra, then start the console:\n"
    "    uv sync --extra gui\n"
    "    uv run forgesmith-gui\n"
)


def main() -> None:
    """Entry point for the ``forgesmith-gui`` script."""
    try:
        from forgesmith.gui.app import run
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the environment
        if (exc.name or "").split(".")[0] == "PyQt5":
            raise SystemExit(INSTALL_HINT) from exc
        raise
    raise SystemExit(run())


__all__ = ["main"]
