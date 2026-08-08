"""Shell tab-completion traps (argcomplete integration).

Completions are derived from the live argparse parsers rather than generated
into a checked-in file, so there is no third mirror of the flag list to drift
(cf. .claude/rules/cli-help.md). What *can* break silently is the plumbing: a
newly added option nobody classified completing nothing, the wrapper's two
composed completer tables drifting apart, or numpy reaching the completion
path, which argcomplete re-runs the whole script for on every TAB press.
Those are what these traps lock down.

Everything left here needs argcomplete *installed*, and that is the entry
condition rather than an observation: the tests below the gate drive its own
protocol, and the completer-table tests above it call `_attach_completers`,
which imports `argcomplete.completers`. The gate is at module scope, so it
aborts the import of this whole file rather than skipping a section of it —
sitting above it buys a test nothing. So a trap that needs no argcomplete does
not belong here however well it reads beside these, and none is left: the
generator's DSP-deferral trap is in `tests/test_layout.py` with the converter's
half of the same contract, and the sink-completer pair in
`tests/test_optional_deps.py` with the traps for argcomplete being *absent*,
which could never have run here at all. Neither file has such a gate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dolby_to_easyeffects
import dolby_to_pipewire
import ee_to_pipewire
from lib.report import messages

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = (
    REPO / "dolby_to_easyeffects.py",
    REPO / "ee_to_pipewire.py",
    REPO / "dolby_to_pipewire.py",
)


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
    what sits above it too. See `tests/test_layout.py` and
    `tests/test_optional_deps.py` for the traps that had to leave because of
    it.
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
