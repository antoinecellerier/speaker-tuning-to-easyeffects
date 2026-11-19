"""The installed commands' entry points: run a script as a clone would.

pyproject.toml installs everything under one package,
``speaker_tuning_to_easyeffects/``, holding the three scripts and ``lib/``,
so no generic top-level ``lib`` lands in ``site-packages/`` where another
distribution could claim the name. The code still imports itself as ``lib``
and the scripts by their own names, so each entry point first puts that
package's folder at the front of ``sys.path``, which is what a clone's
script directory gives a ``./dolby_to_easyeffects.py`` run, then calls the
script's ``run_cli``.

Only the installed commands come here. A clone runs the scripts directly
and never imports this module. Stdlib-only, so the imports this triggers
are the script's own.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

__all__ = ["dolby_to_easyeffects", "dolby_to_pipewire", "ee_to_pipewire"]


def _run(script: str) -> int:
    """*script*'s ``run_cli()``, imported from this package's folder."""
    # lib/launch.py -> lib/ -> the folder the scripts sit in.
    root = str(Path(__file__).resolve().parent.parent)
    # Moved to the front even when already present further back: an
    # earlier entry holding its own lib/ would otherwise win the import.
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return importlib.import_module(script).run_cli()


def dolby_to_easyeffects() -> int:
    return _run("dolby_to_easyeffects")


def dolby_to_pipewire() -> int:
    return _run("dolby_to_pipewire")


def ee_to_pipewire() -> int:
    return _run("ee_to_pipewire")
