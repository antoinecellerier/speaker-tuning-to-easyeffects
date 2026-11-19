"""How this run was started: as an entry script, or as an installed command.

A clone runs ``dolby_to_easyeffects.py`` and its two siblings. A package
installed with pip, pipx or Nix runs commands of the same names without
``.py`` (pyproject.toml ``[project.scripts]``), and no ``.py`` exists to run.
So a message naming a script names it the way this run's user can run it,
and ``sys.argv[0]`` plus `lib.paths.is_checkout` say which. Stdlib-only, like the rest of what
``ee_to_pipewire.py`` imports at startup: the conf writer names a script too.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lib import paths

__all__ = ["ENTRY_SCRIPTS", "typed", "installed", "script"]

# The three entry points, for spelling a sibling the way this one was run.
ENTRY_SCRIPTS = frozenset({"dolby_to_easyeffects.py", "ee_to_pipewire.py",
                           "dolby_to_pipewire.py"})
# The same three installed as commands.
_INSTALLED = frozenset(Path(s).stem for s in ENTRY_SCRIPTS)


def typed() -> Path:
    """This run's ``sys.argv[0]``, or an empty path when there is none."""
    return Path(sys.argv[0]) if sys.argv and sys.argv[0] else Path()


def installed() -> bool:
    """Whether this run is an installed command rather than an entry
    script.

    The name alone can't say: a checkout's script reached through a
    ``~/bin/dolby_to_easyeffects`` symlink has an installed command's name,
    and its user still has the checkout's remedies, ``.py`` names and
    requirements.txt. Only an install runs from outside a checkout."""
    return typed().name in _INSTALLED and not paths.is_checkout()


def script(name: str) -> str:
    """An entry script's name as this run's user knows it: *name*, or its
    command name without ``.py`` when this run is an installed command."""
    return Path(name).stem if installed() else name
