"""The colour dependencies stay optional, end to end.

`rich` and `rich-argparse` are optional — the README promises that "without
them everything still works in plain monochrome" — and two guarded imports in
`lib/console.py` carry that promise: the themed `Console`, and the
`RichHelpFormatter` each `build_parser` hands to argparse. Nothing else in the
suite notices if either is "tidied" into a plain import: the machine that
makes that edit has both installed, so the run stays green and only a user on
a plain install ever sees the traceback.

`tests/test_completions.py::test_runs_with_argcomplete_absent` is the sibling
trap, in the same shape, for the third optional dependency.

**Why this one is not in that file.** Its module-scope
`pytest.importorskip("argcomplete")` aborts the import of the whole module, so
on a machine without argcomplete the file collapses to a single skip and
*every* test in it goes uncollected — including the ones defined above the
gate, the argcomplete-absence trap among them. Measured by hiding argcomplete
behind a meta-path finder: 17 passed becomes 1 skipped. Hosting the rich trap
there would gate rich coverage on an unrelated optional dependency, which is
the failure mode these traps exist to prevent. This file has no module-scope
gate, and nothing added here may grow one.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = (
    REPO / "dolby_to_easyeffects.py",
    REPO / "ee_to_pipewire.py",
    REPO / "dolby_to_pipewire.py",
)

# argparse wraps the epilog at the terminal width, so the child's width is
# pinned rather than inherited: the tip below has to land on one line, and the
# two runs being compared have to wrap identically.
HELP_COLUMNS = "80"

# The epilog every parser appends when a colour dep is missing, truncated
# before the wrap point so the assertion doesn't depend on where it falls.
TIP = "Tip: install rich and rich-argparse for colored output"


def _help(script: Path, env_extra: dict[str, str] | None = None) -> str:
    """`<script> --no-color --help`, captured.

    Exit 0 and a usage synopsis are the floor: an optional import that lost
    its guard fails here, before any assertion on the text gets a chance.
    """
    result = subprocess.run(
        [sys.executable, str(script), "--no-color", "--help"],
        capture_output=True, text=True, timeout=60, cwd=REPO,
        env={**os.environ, "COLUMNS": HELP_COLUMNS, **(env_extra or {})},
    )
    assert result.returncode == 0, (
        f"{script.name} --help exited {result.returncode}:\n{result.stderr}"
    )
    assert "usage:" in result.stdout, (
        f"{script.name} --no-color --help printed no lowercase usage "
        "synopsis, so the plain formatter is not in force: argparse writes "
        "'usage:' where rich-argparse title-cases it to 'Usage:'. Check that "
        "build_parser still pre-scans argv for --no-color.\n"
        + result.stdout[:400]
    )
    return result.stdout


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_runs_with_rich_absent(script, tmp_path):
    """With rich and rich-argparse unimportable, `--no-color --help` renders
    byte for byte what it renders with them installed, plus the install tip.

    Both are blocked, not just rich: `rich_argparse` imports rich itself, so
    blocking rich alone turns an absent optional dependency into a failure
    *inside* a present one — a different path from the one a plain install
    takes, and a confusing one to debug.

    Byte-identity is the strong claim here, and it holds only because
    `--no-color` pins `argparse.HelpFormatter` on both sides (each
    `build_parser` pre-scans argv for the flag). Without `--no-color` it does
    not hold and is not meant to: rich-argparse title-cases the section
    headings ("Usage:", "Options:", "Positional Arguments:"), so the coloured
    rendering differs by design and nothing here covers it.

    The one intended difference is the epilog — with either dep missing every
    parser appends the install tip — which is also what proves both shadows
    took effect, since the tip names each dep the child could not import. On a
    machine that has neither dep installed the reference run is itself the
    fallback, so the comparison is a tautology there and only the tip
    assertion bites.
    """
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    for module in ("rich", "rich_argparse"):
        (blocker / f"{module}.py").write_text(
            f'raise ImportError("{module} blocked by '
            'tests/test_optional_deps.py")\n'
        )
    # Prepended, not replacing: whatever the caller's PYTHONPATH contributes
    # has to reach both runs or the comparison is not like for like.
    pythonpath = os.pathsep.join(
        p for p in (str(blocker), os.environ.get("PYTHONPATH")) if p)

    without = _help(script, {"PYTHONPATH": pythonpath})
    installed = _help(script)

    assert without.startswith(installed), (
        f"{script.name}: --no-color --help must render the same whether or "
        "not rich is installed, and does not:\n"
        + "\n".join(difflib.unified_diff(
            installed.splitlines(), without.splitlines(),
            "rich installed", "rich blocked", lineterm=""))
        + "\n\n--no-color pins argparse.HelpFormatter on both sides, so a "
        "difference above the install tip means a rich-only path leaked into "
        "the plain rendering."
    )
    assert TIP in without, (
        f"{script.name}: with both colour deps unimportable the parser must "
        "append the install tip naming them. Its absence means either the "
        "epilog no longer names both, or the shadow modules did not take "
        f"effect at all:\n{without}"
    )
