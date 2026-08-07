"""The `lib/` package's stdlib-only contract, enforced rather than asserted.

`lib/version.py`, `lib/ee_paths.py` and `lib/doctor.py` each say in their own
docstring that they import nothing but the stdlib, because `ee_to_pipewire.py`
imports all three at startup and must not pull numpy/scipy into a converter
that does no DSP. Until now that was a promise in prose. As more of
`dolby_to_easyeffects.py` moves into `lib/`, the tempting shortcut is to reach
for numpy in a module one of these already imports — this fails the moment
that happens, at the cheap end of the pipeline rather than in a user's startup
time.

Each check runs in a subprocess: `sys.modules` is process-wide, and under
`-n auto` some other test in the same worker has almost certainly imported
numpy already.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The DSP stack, plus the optional presentation deps. rich is absent on a
# plain install and every script is required to work without it, so a lib
# module that hard-imports it would break that contract too.
FORBIDDEN = ("numpy", "scipy", "rich", "rich_argparse")

STDLIB_ONLY = ("lib.version", "lib.ee_paths", "lib.doctor")


@pytest.mark.parametrize("module", STDLIB_ONLY)
def test_module_pulls_in_no_heavy_dependency(module):
    probe = (
        "import sys\n"
        f"import {module}\n"
        f"got = [n for n in {FORBIDDEN!r} if n in sys.modules]\n"
        "print(','.join(got))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    pulled = result.stdout.strip()
    assert not pulled, (
        f"{module} is documented as stdlib-only but pulls in {pulled}. "
        "Move whatever needs it into a module ee_to_pipewire.py doesn't "
        "import at startup."
    )


def test_lib_package_import_is_free():
    """`import lib` must stay side-effect-free — no submodule re-exports.

    A convenience re-export in `lib/__init__.py` would drag the whole package
    in behind any single import and hand every future module a ready-made
    import cycle.
    """
    probe = (
        "import sys\n"
        "import lib\n"
        "print(','.join(n for n in sys.modules if n.startswith('lib.')))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip(), (
        f"importing lib also imported {result.stdout.strip()}"
    )


def test_root_holds_only_entry_points():
    """Every root .py file is something a user types.

    The layout rule this repo keeps: the root is the command surface (those
    paths appear in the README, the issue template and the argcomplete
    registration line), and everything else lives under lib/.
    """
    entry_points = {"dolby_to_easyeffects.py", "ee_to_pipewire.py",
                    "dolby_to_pipewire.py"}
    found = {p.name for p in ROOT.glob("*.py")}
    assert found == entry_points, (
        f"unexpected root-level Python: {sorted(found - entry_points)} — "
        "a module that is not an entry point belongs in lib/"
    )
