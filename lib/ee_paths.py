"""Where EasyEffects keeps its presets and impulse responses.

Stdlib-only on purpose, for the same reason as ``version.py``:
``ee_to_pipewire.py`` has to resolve the same paths the generator writes to,
and importing the generator to ask would pull numpy/scipy into a converter
that never does any DSP.

The two installs don't mirror each other's layout — the Flatpak keeps its
presets under ``config/`` inside the app's sandbox while the native package
follows XDG and puts them under ``share/`` — so the base has to be chosen,
not derived from a common suffix.

The ``DEFAULT_*`` constants below are the single definition of where a run
writes: the generator's ``--output-dir`` / ``--irs-dir`` / ``--autoload-dir``
defaults and the converter's ``--irs-dir`` default are all this module's
attributes. ``lib/pipewire/install.py`` held a second derivation of the IRS
directory until this module absorbed it. That copy had itself started out
hardcoded to the native path, which sent Flatpak users looking for an impulse
response in a directory they never had — the reason to keep one definition
rather than two that merely agree today.
"""

from pathlib import Path

__all__ = ["FLATPAK_APP_ID", "FLATPAK_BASE", "NATIVE_BASE",
           "prefer_flatpak", "easyeffects_base",
           "USE_FLATPAK", "EASYEFFECTS_BASE",
           "DEFAULT_OUTPUT_DIR", "DEFAULT_IRS_DIR", "DEFAULT_AUTOLOAD_DIR",
           "DEFAULT_EASYEFFECTS_RC"]

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


# Probed once, at import, because every default below has to name the *same*
# install: re-deciding per constant would let a directory appearing mid-run
# split them across the two trees.
USE_FLATPAK = prefer_flatpak()
EASYEFFECTS_BASE = easyeffects_base()

DEFAULT_OUTPUT_DIR = EASYEFFECTS_BASE / "output"
DEFAULT_IRS_DIR = EASYEFFECTS_BASE / "irs"
DEFAULT_AUTOLOAD_DIR = EASYEFFECTS_BASE / "autoload" / "output"

# EasyEffects 8.x KConfig file. Separate from EASYEFFECTS_BASE (which is
# under XDG_DATA_HOME for presets/IRs); this one is under XDG_CONFIG_HOME.
_FLATPAK_RC = Path.home() / ".var" / "app" / FLATPAK_APP_ID / "config" / "easyeffects" / "db" / "easyeffectsrc"
_NATIVE_RC = Path.home() / ".config" / "easyeffects" / "db" / "easyeffectsrc"
DEFAULT_EASYEFFECTS_RC = _FLATPAK_RC if USE_FLATPAK else _NATIVE_RC
