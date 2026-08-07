"""Where the checkout itself is, for the files that ship alongside the code.

A handful of paths are relative to the repo rather than to the user's home:
``tools/measure_pw/validate_conf.py``, which ``ee_to_pipewire.py`` shells out
to for its schema self-check, is the first. Those resolve from ``__file__``,
and the entry points can afford ``Path(__file__).resolve().parent`` only
because they sit at the root — a module one directory deeper needs
``parent.parent``, two deeper needs three hops, and each of those is a silent
breakage waiting for the next move.

So the walk-up is written once, here, and everything else asks. Stdlib-only,
like the rest of what ``ee_to_pipewire.py`` imports at startup.

This is a *source* path, not a data path: it points at the checkout a user
cloned or unzipped, which is the only place these files exist. Nothing is
installed, so there is no prefix to search and no fallback to make.
"""

from pathlib import Path

__all__ = ["REPO_ROOT"]

# lib/paths.py -> lib/ -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parent.parent
