"""Shell tab-completion traps (argcomplete integration).

Completions are derived from the live argparse parsers rather than generated
into a checked-in file, so there is no third mirror of the flag list to drift
(cf. .claude/rules/cli-help.md). What *can* break silently is the plumbing:
the magic marker slipping past the 1024-byte window the shell hook scans, the
optional import turning into a hard one, or the deferred DSP import creeping
back onto the completion path. Those are what these traps lock down.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dolby_to_easyeffects
import dolby_to_pipewire
import ee_to_pipewire
from lib.hardware import sinks
from lib.report import messages

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = (
    REPO / "dolby_to_easyeffects.py",
    REPO / "ee_to_pipewire.py",
    REPO / "dolby_to_pipewire.py",
)

# The shell hook reads only the head of the file looking for the marker, so
# placement is load-bearing, not cosmetic.
# https://github.com/kislyuk/argcomplete — "the shell will look for the string
# PYTHON_ARGCOMPLETE_OK in the first 1024 bytes of any executable".
MARKER_WINDOW = 1024


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_marker_within_the_scanned_window(script):
    """Every script advertises itself to the shell hook, early enough to be
    seen. Editing the header docstring is what would push it out of range."""
    head = script.read_bytes()[:MARKER_WINDOW]
    assert b"# PYTHON_ARGCOMPLETE_OK" in head, (
        f"{script.name}: the marker must appear in the first {MARKER_WINDOW} "
        "bytes or the shell will not offer completions for it"
    )


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_runs_with_argcomplete_absent(script, tmp_path):
    """argcomplete is optional, exactly like rich/rich-argparse: with it
    unimportable the scripts must behave identically. Guards against someone
    "tidying" the guarded import into a plain one."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    (blocker / "argcomplete.py").write_text(
        'raise ImportError("blocked by tests/test_completions.py")\n'
    )
    env = {**os.environ, "PYTHONPATH": str(blocker)}
    result = subprocess.run(
        [sys.executable, str(script), "--no-color", "--help"],
        capture_output=True, text=True, timeout=30, env=env, cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_completion_path_skips_the_dsp_import():
    """NumPy/SciPy are ~0.4 s of the ~0.5 s startup and argcomplete re-runs the
    script on *every* TAB, so the completion path must not import them. A
    regression here is invisible except as sluggish completion — hence a trap
    on sys.modules rather than on wall-clock."""
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import dolby_to_easyeffects\n"
        "print('numpy' in sys.modules, 'scipy' in sys.modules)\n" % str(REPO)
    )
    env = {**os.environ, "_ARGCOMPLETE": "1"}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=30, env=env, cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False", (
        "the DSP stack leaked onto the completion path: " + result.stdout
    )


def test_dsp_loads_on_a_normal_run():
    """The other half of the deferral: a real run still gets NumPy, whether or
    not _ARGCOMPLETE happens to be set in the environment."""
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import dolby_to_easyeffects as d\n"
        "d.ensure_dsp()\n"
        "print(d.np.array([1.0]).sum())\n" % str(REPO)
    )
    for env_extra in ({}, {"_ARGCOMPLETE": "1"}):
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, **env_extra}, cwd=REPO,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1.0"


# --- completer coverage ---------------------------------------------------

def _value_taking(parser):
    """Options that consume a value, i.e. the ones a shell could usefully
    complete. Flags (nargs == 0) and -h/--version have nothing to offer."""
    return [a for a in parser._actions if a.nargs != 0]


@pytest.mark.parametrize(
    "build, attach",
    [
        (dolby_to_easyeffects.build_parser, dolby_to_easyeffects._attach_completers),
        (ee_to_pipewire.build_parser, ee_to_pipewire._attach_completers),
    ],
    ids=["dolby_to_easyeffects", "ee_to_pipewire"],
)
def test_every_path_option_has_a_completer(build, attach):
    """argparse records `type=Path` for directories and files alike, so the
    kind lives in _attach_completers. A newly added path option that nobody
    classified would silently complete nothing — fail instead."""
    parser = build([])
    attach(parser)
    unclassified = [
        a.option_strings[0] if a.option_strings else a.dest
        for a in _value_taking(parser)
        if a.type is Path and getattr(a, "completer", None) is None
    ]
    assert not unclassified, (
        "path-valued options with no completer: "
        + ", ".join(unclassified)
        + " — add them to _attach_completers()"
    )


def test_wrapper_inherits_both_completer_tables():
    """dolby_to_pipewire composes its parser from both converters' argument
    builders; its completers must compose the same way rather than being a
    third table to keep in sync."""
    parser = dolby_to_pipewire.build_parser([])
    dolby_to_easyeffects._attach_completers(parser)
    ee_to_pipewire._attach_completers(parser)
    by_dest = {a.dest: a for a in parser._actions}
    # xml_file/windows/output_dir come from the generator's table,
    # target_sink from the PipeWire converter's.
    for dest in ("xml_file", "windows", "output_dir", "target_sink"):
        assert getattr(by_dest[dest], "completer", None) is not None, (
            f"--{dest} lost its completer when the two tables were composed"
        )


# --- end-to-end completion, driven through argcomplete's own protocol ------

argcomplete = pytest.importorskip(
    "argcomplete", reason="argcomplete not installed"
)


def _complete(script: Path, comp_line: str) -> list[str]:
    """Ask a script for its completions the way a shell does.

    argcomplete signals a completion request through the environment and
    answers on file descriptor 8, so the `8>&1` redirection below is exactly
    what the shell hook performs. That exercises the real path — parser build,
    _attach_completers, autocomplete — with no pty and no flakiness.
    """
    env = {
        **os.environ,
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\n",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_TYPE": "9",  # TAB
        "COMP_LINE": comp_line,
        "COMP_POINT": str(len(comp_line)),
    }
    proc = subprocess.run(
        ["bash", "-c", 'exec "$1" "$2" 8>&1 1>/dev/null 2>/dev/null',
         "_", sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=30,
    )
    return [c for c in proc.stdout.split("\n") if c]


def test_disable_completes_exactly_the_disableable_filters():
    """The value list a user tabs through is read off the parser, so it cannot
    drift from DISABLEABLE_FILTERS. This asserts that wiring end to end."""
    got = _complete(SCRIPTS[0], "dolby_to_easyeffects.py --disable ")
    assert got == list(messages.DISABLEABLE_FILTERS)


def test_enable_completes_exactly_the_enableable_filters():
    got = _complete(SCRIPTS[0], "dolby_to_easyeffects.py --enable ")
    assert got == list(messages.ENABLEABLE_FILTERS)


def test_variant_completes_the_wrapper_choices():
    got = _complete(SCRIPTS[2], "dolby_to_pipewire.py --variant ")
    assert got == [*dolby_to_pipewire.VARIANT_STEMS, "all"]


def test_prefix_narrows_the_flag_list():
    """A partial flag completes to the matching long options."""
    got = _complete(SCRIPTS[1], "ee_to_pipewire.py --no")
    assert "--no-validate" in got and "--no-color" in got
    assert all(c.startswith("--no") for c in got)


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
