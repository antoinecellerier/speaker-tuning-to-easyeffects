"""Where EasyEffects keeps its presets and impulse responses.

Stdlib-only on purpose, for the same reason as ``version.py``:
``ee_to_pipewire.py`` has to resolve the same paths the generator writes to,
and importing the generator to ask would pull numpy/scipy into a converter
that never does any DSP. Nothing here may shell out either — this module
builds argparse defaults, so a subprocess would put its timeout on ``--help``.

EasyEffects 8 keeps presets, impulse responses and autoload profiles under
``XDG_DATA_HOME`` and only its settings database under ``XDG_CONFIG_HOME``
(upstream ``src/presets_directory_manager.cpp``, ``src/db_manager.cpp``). Both
installs follow that same split; the Flatpak's two XDG roots are just spelled
``~/.var/app/<app id>/{data,config}``. So a run's write base is chosen per
install, while the rc path below is rooted separately rather than derived from
it.

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

__all__ = ["FLATPAK_APP_ID", "FLATPAK_BASE", "FLATPAK_CONFIG_BASE",
           "NATIVE_BASE", "flatpak_app_installed", "flatpak_tree_exists",
           "prefer_flatpak", "easyeffects_base",
           "USE_FLATPAK", "EASYEFFECTS_BASE",
           "DEFAULT_OUTPUT_DIR", "DEFAULT_IRS_DIR", "DEFAULT_AUTOLOAD_DIR",
           "DEFAULT_EASYEFFECTS_RC", "uses_custom_dirs"]

FLATPAK_APP_ID = "com.github.wwmm.easyeffects"
_FLATPAK_APP = Path.home() / ".var" / "app" / FLATPAK_APP_ID
# EasyEffects 8.0.0 moved presets, impulse responses and autoload profiles to
# XDG_DATA_HOME, and migrates any XDG_CONFIG_HOME copies into it on *every*
# start — copying them across and sending the old directory to the trash. So
# the config tree is not a fallback to write to: it is a directory EasyEffects
# empties. These presets need EasyEffects 8 anyway (a 7 is a --doctor FAIL),
# and no version both reads the config tree and can load what we write.
FLATPAK_BASE = _FLATPAK_APP / "data" / "easyeffects"
# The same sandbox's other XDG root: EasyEffects 8 kept its settings database
# here, and a pre-8 install kept its presets here too. Never written to; read
# for the rc below, and probed to recognise a Flatpak that predates the move.
FLATPAK_CONFIG_BASE = _FLATPAK_APP / "config" / "easyeffects"
NATIVE_BASE = Path.home() / ".local" / "share" / "easyeffects"


def flatpak_app_installed() -> bool:
    """Is the EasyEffects Flatpak deployed, whether or not it has ever run?

    Two stats against the system and per-user app roots, never ``flatpak
    info`` — see the no-subprocess rule above. ``Path.home()`` is read inside
    the function rather than frozen into a constant so the tests' fake-``$HOME``
    redirect, which patches this module's own ``Path``, reaches it.
    """
    for root in (Path("/var/lib/flatpak/app"),
                 Path.home() / ".local" / "share" / "flatpak" / "app"):
        if (root / FLATPAK_APP_ID).exists():
            return True
    return False


def flatpak_tree_exists() -> bool:
    """Has a Flatpak EasyEffects left files here, under *either* XDG root?

    Detection only — nothing is ever written to the config tree. A pre-8
    Flatpak that only wrote under ``config/`` would otherwise be
    indistinguishable from no Flatpak at all, now that ``FLATPAK_BASE`` names
    the data tree, and would be silently handed the native paths instead.
    """
    return FLATPAK_BASE.exists() or FLATPAK_CONFIG_BASE.exists()


def prefer_flatpak() -> bool:
    """Choose between Flatpak and native EasyEffects install locations.

    Prefers whichever install has a data directory (i.e. has been run at least
    once). If neither has been run, probes Flatpak app install roots so a
    freshly-installed-but-unopened Flatpak still picks the Flatpak paths. On
    systems with both installed and both launched, preserves the prior default
    (Flatpak wins) — but a Flatpak tree with no Flatpak deployed behind it is
    leftovers from an uninstall, not an install, and loses to a native tree.
    """
    flatpak_used = flatpak_tree_exists()
    native_used = NATIVE_BASE.exists()
    installed = flatpak_app_installed()
    if flatpak_used and (installed or not native_used):
        return True
    if native_used:
        return False
    return installed


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

# EasyEffects 8.x KConfig file. Rooted in the *config* tree deliberately, and
# not under EASYEFFECTS_BASE: 8.0.0 moved presets, impulses and autoload
# profiles to XDG_DATA_HOME but kept the settings database under
# XDG_CONFIG_HOME. The two agreed by accident while the Flatpak base was the
# config tree — which is why the rc writes landed correctly for Flatpak users
# all along and the presets beside them did not. Deriving this from
# FLATPAK_CONFIG_BASE is what keeps them agreeing on purpose.
_FLATPAK_RC = FLATPAK_CONFIG_BASE / "db" / "easyeffectsrc"
_NATIVE_RC = Path.home() / ".config" / "easyeffects" / "db" / "easyeffectsrc"


def uses_custom_dirs(output_dir: Path, irs_dir: Path) -> bool:
    """Did a run write somewhere other than EasyEffects' own tree?

    *Either* dir moved counts, so every check that keys on this agrees about
    what "custom" means: --doctor skips the EE-location and selected-preset
    verdicts on it, and the end-of-run install-mismatch warning fires only on
    its negation. Written once because the two read as De Morgan duals and
    an inverted hand-written copy would be silent.
    """
    return output_dir != DEFAULT_OUTPUT_DIR or irs_dir != DEFAULT_IRS_DIR


DEFAULT_EASYEFFECTS_RC = _FLATPAK_RC if USE_FLATPAK else _NATIVE_RC
