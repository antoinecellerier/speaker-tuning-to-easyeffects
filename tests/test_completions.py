"""Shell tab-completion traps (argcomplete integration).

Completions are derived from the live argparse parsers rather than generated
into a checked-in file, so there is no third mirror of the flag list to drift
(cf. .claude/rules/cli-help.md). What *can* break silently is the plumbing: a
newly added option nobody classified completing nothing, the wrapper's two
composed completer tables drifting apart, or the generator's deferred DSP
import climbing back out of main() to module scope. Those are what these traps
lock down.

Everything left here needs argcomplete *installed*: the tests below the gate
drive its own protocol, and `_attach_completers` imports
`argcomplete.completers`. The gate itself is at module scope, so it aborts the
import of this whole file rather than skipping a section of it — the
DSP-deferral trap above it is collateral, and a trap for argcomplete being
*absent* could not run here at all. Those live in
`tests/test_optional_deps.py`, which has no such gate.
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
from tests.conftest import write_synthetic_tuning_xml

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = (
    REPO / "dolby_to_easyeffects.py",
    REPO / "ee_to_pipewire.py",
    REPO / "dolby_to_pipewire.py",
)


def test_the_dsp_import_is_deferred_past_every_early_return(tmp_path):
    """NumPy/SciPy are ~0.35 s of the generator's ~0.5 s startup, and the
    generator imports them inside main(), just above the emit loop. So a path
    that returns before that loop — --version here, with --list, --doctor,
    --speaker-info and an argparse error alongside it — must cost nothing,
    while a real conversion still gets them.

    Both halves are load-bearing: an import hoisted back to module scope fails
    the first, one deferred past its own use fails the second. A regression on
    the first half is invisible except as a sluggish `--version`, hence a trap
    on sys.modules rather than on wall-clock.

    The conversion passes both output directories — --output-dir without
    --irs-dir writes the .irs into the live EasyEffects tree.
    """
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import dolby_to_easyeffects as d\n"
        "try:\n"
        "    d.main(sys.argv[1:])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('numpy' in sys.modules, 'scipy' in sys.modules)\n" % str(REPO)
    )

    def dsp_loaded(*argv: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", probe, *argv],
            capture_output=True, text=True, timeout=120, cwd=REPO,
        )
        assert result.returncode == 0, result.stderr
        # main() prints the run's own output first; the probe's verdict is the
        # last line.
        return result.stdout.strip().splitlines()[-1]

    assert dsp_loaded("--version") == "False False", (
        "the DSP stack reached a path that returns before the emit loop — "
        "something imports numpy at module scope again"
    )

    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert dsp_loaded(str(xml),
                      "--output-dir", str(tmp_path / "presets"),
                      "--irs-dir", str(tmp_path / "irs")) == "True True"
    assert list((tmp_path / "irs").glob("*.irs")), "no conversion happened"


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


def _complete(script: Path, comp_line: str,
              env_extra: dict[str, str] | None = None) -> list[str]:
    """Ask a script for its completions the way a shell does.

    argcomplete signals a completion request through the environment and
    answers on file descriptor 8, so the `8>&1` redirection below is exactly
    what the shell hook performs. That exercises the real path — parser build,
    _attach_completers, autocomplete — with no pty and no flakiness.

    ``env_extra`` is layered last, so a caller can bend the child's
    environment (PYTHONPATH, say) without rebuilding the protocol variables.
    """
    env = {
        **os.environ,
        "_ARGCOMPLETE": "1",
        "_ARGCOMPLETE_IFS": "\n",
        "_ARGCOMPLETE_SUPPRESS_SPACE": "1",
        "COMP_TYPE": "9",  # TAB
        "COMP_LINE": comp_line,
        "COMP_POINT": str(len(comp_line)),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        ["bash", "-c", 'exec "$1" "$2" 8>&1 1>/dev/null 2>/dev/null',
         "_", sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=REPO, timeout=30,
    )
    return [c for c in proc.stdout.split("\n") if c]


def test_completion_survives_an_unimportable_dsp_stack(tmp_path):
    """A TAB press must not cost the ~0.35 s numpy import — argcomplete re-runs
    the whole script on *every* one of them. Asserted by making numpy and scipy
    raise on import and requiring completion to work anyway, which is stronger
    than a sys.modules probe: a probe passes on a path that would have imported
    them had it gone one line further.

    Below the argcomplete gate on purpose, not by oversight: it drives the
    real protocol, so it could not run without argcomplete wherever in this
    file it sat. That is a fact about this test, not about the gate — the gate
    is at module scope and aborts the import of the whole file, so it skips
    what sits above it too. See `tests/test_optional_deps.py` for the traps
    that had to leave because of it.
    """
    blocker = tmp_path / "no-dsp"
    blocker.mkdir()
    for module in ("numpy", "scipy"):
        (blocker / f"{module}.py").write_text(
            f'raise ImportError("{module} blocked by '
            'tests/test_completions.py")\n'
        )
    got = _complete(SCRIPTS[0], "dolby_to_easyeffects.py --disable ",
                    env_extra={"PYTHONPATH": str(blocker)})
    assert got == list(messages.DISABLEABLE_FILTERS), (
        "completion broke with the DSP stack unimportable, so something on "
        f"the completion path now imports numpy or scipy. Got: {got}"
    )


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
