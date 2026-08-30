"""The frame both ``--doctor`` reports print into.

`dolby_to_easyeffects.py --doctor` checks the environment an EasyEffects preset
lands in; `ee_to_pipewire.py --doctor` checks the PipeWire chain. They probe
different things, but a reader meets the same report either way, so the order
and the text around the findings live here rather than in each of them.

**Inventory leads, diagnosis trails.** Someone runs `--doctor` because
something is already wrong, and the report is longer than a terminal: printed
inventory-last, the checks and the fix command scrolled off a 26-line window
and a PCI listing was the last thing on screen. So the widest context goes
first (hardware, the same block `--speaker-info` prints), then the audio
server (`=== PipeWire ===`: where the sound goes, the clock, dropped buffers),
then the tool's own state (`=== EasyEffects setup ===`, or the PipeWire path's
`=== PipeWire filter-chain setup ===`), then what is wrong with it, then what
to do about it. The setup block sits directly above the checks because the
check details name those confs, sinks and presets — the facts a reader
cross-references stay on the same screen. `.claude/rules/user-messages.md`
states the contract; `tests/test_pw_doctor.py` and `tests/test_preset.py`
each trap the order.

The hardware block is *not* printed from here. Both reports show it in the same
slot, but the PipeWire side probes at print time and the EasyEffects side at
gather time, so each calls `lib/report/speaker.py` itself and this module stays
below it in the import graph.

Why here and not `lib/doctor.py`, which owns the shared vocabulary: this prints,
so it reaches `lib/console.py`, and `console` imports `lib/doctor.py` for
`tilde`. An `import console` there would close a cycle — which is why that
module's printers take a `cprint` instead. Everything below already lives above
`console`, so it can just call it.
"""

from __future__ import annotations

from collections.abc import Sequence

from lib import console, doctor
from lib.doctor import CheckResult
from lib.report import findings as report_findings


def print_report_header(running_version: str) -> None:
    """The project name and version, alone at the top.

    The project's name, not the running script's: `dolby_to_pipewire.py
    --doctor` and `ee_to_pipewire.py --doctor` print the same report, and a
    header naming the other script reads as a mis-invocation.
    """
    console.cprint("head", f"speaker-tuning-to-easyeffects {running_version}")
    print()


def print_environment(lines: Sequence[str], title: str) -> None:
    """An inventory block — raw probed facts, always shown, under *title*.

    Shown whatever the checks concluded: a verdict can be wrong or UNKNOWN and
    the report still has to be diagnosable by someone reading it remotely.
    Sections are named by what they list (`=== PipeWire ===`, `=== EasyEffects
    setup ===`), like every other section of the report — not "Environment",
    which named nothing a reader could find.

    ``lines`` are rendered strings, printed verbatim through bare ``print``.
    Unstyled is deliberate — this is a paste block, and each report pads its
    labels to a gutter so the values line up. An empty string is a group
    break within the block.
    """
    console.cprint("head", title)
    for line in lines:
        print(line)
    print()


def print_check_block(title: str, shown: Sequence[CheckResult],
                      counted: Sequence[CheckResult] | None = None) -> None:
    """The diagnosis: the header, the checks, the counted summary, the verdict.

    The header sits here, with the checks it names, rather than at the top of
    the report — up there it labelled a hardware dump it has nothing to do
    with, and left the check block as the only section without a heading.

    ``counted`` defaults to ``shown`` and differs only where a report collapses
    a run of identical passes into one line: the summary has to stay honest
    about how many checks actually ran, so a machine with dozens of presets
    still reports every one of them in the count.
    """
    console.cprint("head", title)
    # Once per report, not once per check: this asks the OS for the terminal
    # size, and it cannot change mid-report.
    width = console._wrap_width()
    for check in shown:
        doctor.emit_check(check, console.cprint, width)
    print()
    doctor.print_summary(shown if counted is None else counted, console.cprint)
    print()
    doctor.print_verdict(shown if counted is None else counted, console.cprint)


def print_closing(advice: Sequence[tuple[str, str]] = ()) -> None:
    """What to do, then the link — the last thing on screen.

    ``advice`` is the report's own remedy as ``(cprint style, text)`` pairs,
    the shape a `CheckResult`'s ``steps`` already uses: it is the one step a
    reader cannot derive from a diagnosis.

    One link, and it is last (`.claude/rules/user-messages.md`). The report is
    written to be pasted, and with the inventory no longer trailing there is
    nothing after it left to say where it should go.
    """
    if advice:
        print()
        for style, text in advice:
            console.cprint(style, text)
    print()
    console.cprint("cta", "Still stuck? Paste everything above into an issue:")
    console.cprint("cta", f"  {report_findings._REPORT_FORM_URL}")
