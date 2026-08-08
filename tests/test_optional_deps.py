"""The optional dependencies stay optional, end to end.

`rich` and `rich-argparse` are optional — the README promises that "without
them everything still works in plain monochrome" — and two guarded imports in
`lib/console.py` carry that promise: the themed `Console`, and the
`RichHelpFormatter` each `build_parser` hands to argparse. `argcomplete` is
the third, guarded the same way at the top of all three scripts. Nothing else
in the suite notices if one of them is "tidied" into a plain import: the
machine that makes that edit has them all installed, so the run stays green
and only a user on a plain install ever sees the traceback.

**Why the argcomplete traps are here and not in `tests/test_completions.py`.**
That file's module-scope `pytest.importorskip("argcomplete")` aborts the
import of the whole module, so on a machine without argcomplete it collapses
to a single skip and *every* test in it goes uncollected — including the ones
defined above the gate. An absence trap that only runs where the dependency is
present asserts nothing, which is the failure mode these traps exist to
prevent, so the two absence traps below, which need no argcomplete of their
own, live here. The sink-completer pair beside them is here for the second
half of that reason alone: a completer is a plain function nobody needs
argcomplete installed to call, so over there those two were skipped as
collateral rather than by design. This file has no module-scope gate, and
nothing added here may grow one.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dolby_to_easyeffects
from lib.hardware import sinks

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


# --- argcomplete ----------------------------------------------------------

@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_runs_with_argcomplete_absent(script, tmp_path):
    """argcomplete is optional, exactly like rich/rich-argparse: with it
    unimportable the scripts must behave identically. Guards against someone
    "tidying" the guarded import into a plain one."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "argcomplete.py").write_text(
        'raise ImportError("blocked by tests/test_optional_deps.py")\n'
    )
    env = {**os.environ, "PYTHONPATH": str(blocker)}
    result = subprocess.run(
        [sys.executable, str(script), "--no-color", "--help"],
        capture_output=True, text=True, timeout=30, env=env, cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_sink_completer_degrades_when_pipewire_is_absent(monkeypatch):
    """A wedged or missing PipeWire must yield no suggestions, never an
    exception — an exception inside a completer breaks the user's TAB key."""
    def boom():
        raise RuntimeError("pw-dump exploded")

    monkeypatch.setattr(sinks, "_enumerate_audio_sinks", boom)
    assert dolby_to_easyeffects._complete_sink_names("") == []


@pytest.mark.skipif(shutil.which("pactl") is None and
                    shutil.which("pw-dump") is None,
                    reason="no PipeWire tooling present")
def test_sink_completer_filters_by_prefix(monkeypatch):
    monkeypatch.setattr(
        sinks, "_enumerate_audio_sinks",
        lambda: [{"name": "alsa_output.speaker"}, {"name": "bluez_output.x"}],
    )
    assert dolby_to_easyeffects._complete_sink_names("alsa") == [
        "alsa_output.speaker"
    ]
    assert len(dolby_to_easyeffects._complete_sink_names("")) == 2


# --- numpy / scipy, which are NOT optional ---------------------------------

def _absent(tmp_path, module: str) -> str:
    """A PYTHONPATH entry under which `module` is genuinely absent.

    Not a shadow `<module>.py` that raises, the way the traps above hide rich
    and argcomplete: those two are guarded by `except ImportError`, so a module
    that exists and raises is close enough. Here it is not. The generator
    catches `ModuleNotFoundError` specifically — a numpy that is installed but
    fails to load has to keep its own traceback — and reads `exc.name` to say
    which module to install, and a hand-raised `ImportError` carries neither.
    Absence is a `find_spec` that raises, which is what the import system does
    when nothing on the path provides the name; `sitecustomize` installs the
    finder before the script under test gets a chance to import anything.
    """
    blocker = tmp_path / f"no-{module}"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\n"
        "class _Absent:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        f"        if name == {module!r} or name.startswith({module + '.'!r}):\n"
        "            raise ModuleNotFoundError(\n"
        "                f\"No module named {name!r}\", name=name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Absent())\n"
    )
    # Prepended: the suite may be running from a venv whose PYTHONPATH the
    # child still needs to find the repo's own imports.
    return os.pathsep.join(
        p for p in (str(blocker), os.environ.get("PYTHONPATH")) if p)


@pytest.mark.parametrize("module", ["numpy", "scipy"])
def test_a_missing_dsp_dependency_names_itself(module, tmp_path):
    """Clone the repo, skip the install step, run it on an XML: the only thing
    on the screen must be one line saying what to install.

    Both modules are covered because the message names `exc.name` rather than
    the word "numpy", and only scipy proves that: it is imported not here but
    by `lib/preset/emit.py`, so a message hardcoding "numpy" would send a user
    whose numpy is fine off to reinstall it.

    Nothing has been printed by the time this fires — the import sits above the
    emit loop, and the default single-profile path prints no banner of its own —
    so a traceback here is the entire output the user gets. Hence the assertion
    that there is none, and that the exit status still says the run failed.
    """
    from tests.conftest import write_synthetic_tuning_xml

    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out_dir = tmp_path / "presets"
    irs_dir = tmp_path / "irs"
    result = subprocess.run(
        [sys.executable, str(REPO / "dolby_to_easyeffects.py"), "--no-color",
         str(xml), "--output-dir", str(out_dir), "--irs-dir", str(irs_dir)],
        capture_output=True, text=True, timeout=120, cwd=REPO,
        env={**os.environ, "PYTHONPATH": _absent(tmp_path, module)},
    )
    output = result.stdout + result.stderr

    assert "Traceback" not in output, (
        f"with {module} absent the generator printed a traceback instead of "
        f"an install hint:\n{output}"
    )
    assert result.returncode == 1, (
        f"a run that could not start must exit 1, got {result.returncode}:\n"
        f"{output}"
    )
    assert f"{module} is not installed" in output, (
        f"the error must name the module that is actually missing ({module}), "
        f"since it is the one the user has to install:\n{output}"
    )
    assert "requirements.txt" in output, (
        f"the error must point at the install step:\n{output}"
    )
    # And nothing after it. The line the guard adds by default sends a reader
    # who has just been told what to install off to read a flag list, and no
    # flag installs numpy — this is the site console.no_next_step exists for.
    assert "--help" not in output, (
        "the install instruction was followed by the generic --help pointer, "
        f"which cannot fix a missing module:\n{output}"
    )
    # The other half of the same bug (previous commit): a run that produces
    # nothing leaves nothing behind either, and this is the earliest failure
    # that can reach the mkdir.
    assert not out_dir.exists() and not irs_dir.exists(), (
        "a run that never started created its output directories anyway"
    )


# The shell hook reads only the head of the file looking for the marker, so
# placement is load-bearing, not cosmetic.
# https://github.com/kislyuk/argcomplete — "the shell will look for the string
# PYTHON_ARGCOMPLETE_OK in the first 1024 bytes of any executable".
MARKER_WINDOW = 1024


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_marker_within_the_scanned_window(script):
    """Every script advertises itself to the shell hook, early enough to be
    seen. Editing the header docstring is what would push it out of range.

    Here rather than with the completion traps because it reads bytes off
    disk: it must hold on a machine that has yet to install argcomplete,
    which is precisely the machine where the marker is about to matter."""
    head = script.read_bytes()[:MARKER_WINDOW]
    assert b"# PYTHON_ARGCOMPLETE_OK" in head, (
        f"{script.name}: the marker must appear in the first {MARKER_WINDOW} "
        "bytes or the shell will not offer completions for it"
    )
