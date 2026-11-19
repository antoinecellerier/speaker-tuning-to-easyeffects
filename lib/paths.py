"""Where the checkout itself is, for the files that ship alongside the code.

A path relative to the repo rather than to the user's home has to resolve from
``__file__``, and the entry points can afford
``Path(__file__).resolve().parent`` only because they sit at the root — a
module one directory deeper needs ``parent.parent``, two deeper needs three
hops, and each of those is a silent breakage waiting for the next move.

So the walk-up is written once, here, and everything else asks. Stdlib-only,
like the rest of what ``ee_to_pipewire.py`` imports at startup.

Two callers. ``lib/driver_package.py`` keeps its default download cache in the
checkout's ``driver-cache/`` (gitignored) rather than under
``$XDG_CACHE_HOME``, so that a bare run from the project root finds what an
earlier run unpacked, through the same probe that finds a hand-unpacked
download there. ``lib/version.py`` asks ``git describe`` only in a checkout.
The scripts under ``tools/`` each walk up from their own depth instead,
because they also insert the result into ``sys.path`` before any ``lib``
import can happen: docs/code-organisation.md "The rules that keep provenance".

The root is not always a checkout. Installed as a package (pip, pipx, Nix),
``lib/`` sits in ``site-packages/`` and this resolves there: nothing of ours to
write beside it, and a ``git describe`` would describe whatever repository
happens to enclose the environment. `is_checkout` tells the two apart.
"""

from pathlib import Path

__all__ = ["REPO_ROOT", "is_checkout"]

# lib/paths.py -> lib/ -> the checkout root.
REPO_ROOT = Path(__file__).resolve().parent.parent


def is_checkout() -> bool:
    """Whether `REPO_ROOT` is a clone or source archive of this project,
    rather than the ``site-packages/`` an installed package lands in.

    ``pyproject.toml`` is the mark: every clone and every GitHub source
    archive has it beside ``lib/``, and installing never copies it.
    `REPO_ROOT` is read per call, so a test can point it elsewhere.
    """
    return (REPO_ROOT / "pyproject.toml").is_file()
