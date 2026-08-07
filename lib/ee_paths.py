"""Where EasyEffects keeps its presets and impulse responses.

Stdlib-only on purpose, for the same reason as ``version.py``:
``ee_to_pipewire.py`` has to resolve the same paths the generator writes to,
and importing the generator to ask would pull numpy/scipy into a converter
that never does any DSP.

The two installs don't mirror each other's layout — the Flatpak keeps its
presets under ``config/`` inside the app's sandbox while the native package
follows XDG and puts them under ``share/`` — so the base has to be chosen,
not derived from a common suffix.
"""

from pathlib import Path

__all__ = ["FLATPAK_APP_ID", "FLATPAK_BASE", "NATIVE_BASE",
           "prefer_flatpak", "easyeffects_base"]

FLATPAK_APP_ID = "com.github.wwmm.easyeffects"
FLATPAK_BASE = (Path.home() / ".var" / "app" / FLATPAK_APP_ID
                / "config" / "easyeffects")
NATIVE_BASE = Path.home() / ".local" / "share" / "easyeffects"


def prefer_flatpak() -> bool:
    """Choose between Flatpak and native EasyEffects install locations.

    Prefers whichever install has a data directory (i.e. has been run
    at least once). If neither has been run, probes Flatpak app install
    roots so a freshly-installed-but-unopened Flatpak still picks the
    Flatpak paths. On systems with both installed and both launched,
    preserves the prior default (Flatpak wins).
    """
    if FLATPAK_BASE.exists():
        return True
    if NATIVE_BASE.exists():
        return False
    for root in (
        Path("/var/lib/flatpak/app"),
        Path.home() / ".local" / "share" / "flatpak" / "app",
    ):
        if (root / FLATPAK_APP_ID).exists():
            return True
    return False


def easyeffects_base() -> Path:
    """The install root both scripts derive their defaults from."""
    return FLATPAK_BASE if prefer_flatpak() else NATIVE_BASE
