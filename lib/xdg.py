"""The two XDG base directories, resolved the way the apps we write for do.

Stdlib-only on purpose, like ``ee_paths.py`` and ``ee_socket.py``: both
converters import this at startup to build argparse defaults, and neither may
pull DSP in to name a directory.

**The rule is "absolute, or the default" — not "set, or the default".** The
XDG basedir spec calls a relative value invalid and says to ignore it, and Qt
implements exactly that, so a relative or empty ``XDG_DATA_HOME`` has to fall
back here too or this module and EasyEffects disagree about where the presets
are. Verified against ``qtpaths6`` 6.10.2, the Qt the installed EasyEffects
links: an absolute value is honoured, a relative one and an empty one both
resolve to ``~/.local/share``.

Reading the environment is the whole point, and it is also the whole risk:
these are process-wide inputs, so a caller that resolves a path *after*
something has changed the environment gets a different answer. The callers
that matter resolve theirs once, at import.

Both roots, rather than one function, because ``ee_paths.py`` needs them
apart: EasyEffects asks Qt for each separately, taking presets, impulse
responses, rnnoise models and autoload profiles from ``AppDataLocation``
(upstream ``src/presets_directory_manager.cpp:39``) and its settings database
from ``ConfigLocation`` plus a literal ``/easyeffects/db``
(``src/db_manager.cpp:86``). EasyEffects never reads either variable itself;
Qt does. EasyEffects sets no ``organizationName``, so Qt appends no
organization component and the leaf really is ``easyeffects`` under both
roots.

``lib/pipewire/checks.py`` wants only the config root: PipeWire and
WirePlumber read ``XDG_CONFIG_HOME`` for their drop-in directories too
(``man pipewire``; ``man wireplumber``, "``~/.config/wireplumber/`` unless
``$XDG_CONFIG_HOME``"), so a conf written under the default there is one the
daemon never scans.

Nothing here is for the *Flatpak* trees. ``flatpak run`` overrides all four
XDG variables inside the sandbox to point at ``~/.var/app/<app id>/``
(``man flatpak-run``), so a sandboxed app never sees the host's values and the
host-side spelling of its tree is rooted at ``$HOME``, not at these. The one
Flatpak path that *is* an XDG path is the per-user *install* root,
``$XDG_DATA_HOME/flatpak`` (``man flatpak``, ``FLATPAK_USER_DIR``).
"""

import os
from pathlib import Path

__all__ = ["data_home", "config_home"]


def _home(variable: str, fallback: str) -> Path:
    """One XDG base directory: the variable if it is absolute, else the
    spec's default under ``$HOME``.

    A function rather than a constant so the environment is read per call —
    which is what lets a test set the variable with ``monkeypatch.setenv``
    instead of reimporting whatever asked.
    """
    value = os.environ.get(variable, "")
    return Path(value) if value.startswith("/") else Path.home() / fallback


def data_home() -> Path:
    """``$XDG_DATA_HOME``, or ``~/.local/share``."""
    return _home("XDG_DATA_HOME", ".local/share")


def config_home() -> Path:
    """``$XDG_CONFIG_HOME``, or ``~/.config``."""
    return _home("XDG_CONFIG_HOME", ".config")
