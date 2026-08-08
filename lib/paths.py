"""Where the checkout itself is, for the files that ship alongside the code.

A path relative to the repo rather than to the user's home has to resolve from
``__file__``, and the entry points can afford
``Path(__file__).resolve().parent`` only because they sit at the root — a
module one directory deeper needs ``parent.parent``, two deeper needs three
hops, and each of those is a silent breakage waiting for the next move.

So the walk-up is written once, here, and everything else asks. Stdlib-only,
like the rest of what ``ee_to_pipewire.py`` imports at startup.

**Nothing asks today.** The one caller was ``lib/pipewire/install.py``, which
built ``tools/measure_pw/validate_conf.py`` from this to shell out to the
converter's schema self-check; that check now runs in process against
``lib.pipewire.validate`` and needs no path at all. What is left is elsewhere
and stays elsewhere: the scripts under ``tools/`` each walk up from their own
depth, because they also insert the result into ``sys.path`` before any
``lib`` import can happen. So this module is a place to put the next such
path, not something the code depends on — read the emptiness as that, rather
than as a caller you have not found yet.

This is a *source* path, not a data path: it points at the checkout a user
cloned or unzipped, which is the only place these files exist. Nothing is
installed, so there is no prefix to search and no fallback to make.
"""

from pathlib import Path

__all__ = ["REPO_ROOT"]

# lib/paths.py -> lib/ -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parent.parent
