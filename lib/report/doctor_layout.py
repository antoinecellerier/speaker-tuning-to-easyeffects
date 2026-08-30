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

import textwrap
from collections.abc import Sequence

from lib import console, doctor
from lib.doctor import CheckResult
from lib.pipewire import session
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


# Every row's value starts at this column, and wrapped continuations indent
# to it, so a value that folds still reads as one column. One constant for
# both doctors' blocks: sized to the widest label either report prints —
# `Selected preset:` — plus a space. A label that outgrows it widens the
# block rather than breaking the alignment silently, which is what `row` and
# the two gutter traps (tests/test_preset.py, tests/test_pw_doctor.py,
# through tests/conftest.py `assert_rows_line_up`) enforce.
GUTTER = 19

# The one hint for "the tool ran but the daemon didn't answer", shared by
# every row that can meet that state, so a reader sees one sentence for one
# condition wherever it strikes.
DAEMON_HINT = "is the PipeWire daemon running?"


def row(label: str, value: str, gutter: int) -> str:
    """One `label: value` row, the value starting at column *gutter*."""
    return f"  {label + ':':<{gutter - 2}}{value}"


def wrapped_row(label: str, value: str, gutter: int) -> list[str]:
    """A row whose value may run past the line: folded to the gutter, like a
    plugin chain, so a narrow terminal never truncates the qualifier."""
    return textwrap.wrap(value, width=console._wrap_width(),
                         break_on_hyphens=False,
                         initial_indent=row(label, "", gutter),
                         subsequent_indent=" " * gutter)


def continuation(text: str, gutter: int) -> list[str]:
    """A second line of a row, hung on the gutter: the standing state above,
    what the check itself observed below, so the two never read as one."""
    return textwrap.wrap(text, width=console._wrap_width(),
                         break_on_hyphens=False,
                         initial_indent=" " * gutter,
                         subsequent_indent=" " * gutter)


def version_rows(pipewire: session.Version, wireplumber: session.Version,
                 gutter: int) -> list[str]:
    """`Versions:` — which audio server the rest of the section describes.

    First row of `=== PipeWire ===` in both reports. PipeWire's is the
    *running daemon*'s (`pw-cli info 0`); WirePlumber has no equivalent
    query, so its number is the installed binary's (`wireplumber
    --version`) — the two can differ after an upgrade nobody restarted,
    and the (running)/(installed) tags say which claim each number makes.
    """
    def half(name: str, v: session.Version, tag: str) -> str:
        if not v.ok:
            reason = (f"{v.reason} — {DAEMON_HINT}"
                      if v.reason == "no answer from pw-cli" else v.reason)
            return f"{name} not read ({reason})"
        return f"{name} {v.text} ({tag})"
    return wrapped_row("Versions",
                       f"{half('PipeWire', pipewire, 'running')}, "
                       f"{half('WirePlumber', wireplumber, 'installed')}",
                       gutter)


def output_sink_rows(label: str, node: str, suffix: str, gutter: int
                     ) -> list[str]:
    """The `Output sink:` row both doctors print: *label* (the sink's
    description, or "" when the probe settled nothing) leading, *node* (its
    node.name, already redacted) trailing, *suffix* (" (from saved config)"
    or "") after it.

    The description leads because it answers the reader's question — what is
    my sound coming out of — and the node name trails because it answers the
    tool's: it is what --autoload-sink takes and what a bug report is triaged
    on. Node names run past 70 columns, so the two share a line only when
    they fit; the name is never wrapped — a name broken across lines stops
    being greppable and stops being copy-pasteable — and just overflows."""
    if not label:
        return [row("Output sink", node + suffix, gutter)]
    width = console._wrap_width()
    one_line = row("Output sink", f"{label} — {node}{suffix}", gutter)
    if len(one_line) <= width:
        return [one_line]
    return textwrap.wrap(label, width=width, break_on_hyphens=False,
                         initial_indent=row("Output sink", "", gutter),
                         subsequent_indent=" " * gutter) \
        + [" " * gutter + node + suffix]


def clock_rows(settings: session.ClockSettings, d: session.Dropouts | None,
               gutter: int) -> list[str]:
    """`Clock:` — the session's clock settings, then on a second line the
    cycle the output actually ran at during the check, or why unknown.

    Shared by both doctors: the `=== PipeWire ===` section is the audio
    server's, whichever chain runs on it. "session defaults" and
    "session-wide" are load-bearing: `clock.quantum` is what a client gets
    when it asks for nothing (`man pipewire.conf`), a driver runs at the
    lowest quantum any follower asks for (`man pw-top`), and a per-node
    `node.force-quantum` rule pins a graph without touching these keys. The
    running cycle comes from the driver row in `pw-top`. "quantum" is
    PipeWire's word for the samples processed per graph cycle — a buffer
    size, not a clock — and the row says so once; the cycle length in ms is
    what a reader can weigh against a crackle.
    """
    if not settings.ok:
        hint = {"pw-metadata not found": "clock settings not read",
                "no answer from pw-metadata": DAEMON_HINT}
        return wrapped_row("Clock", f"{settings.reason} — "
                           f"{hint.get(settings.reason, 'clock settings not read')}",
                           gutter)
    forced = []
    if settings.force_quantum not in ("", "0"):
        forced.append(f"quantum {settings.force_quantum}")
    if settings.force_rate not in ("", "0"):
        forced.append(f"rate {settings.force_rate}")
    bounds = (f", min {settings.min_quantum}, max {settings.max_quantum}"
              if settings.min_quantum and settings.max_quantum else "")
    tail = (f"; forced session-wide: {', '.join(forced)}" if forced
            else ", no session-wide override")
    rows = wrapped_row("Clock", f"{settings.rate} Hz, quantum {settings.quantum} "
                       f"samples per cycle (session defaults{bounds}){tail}", gutter)
    if d is not None and d.ok and d.sink is not None:
        if d.running_quantum and d.running_rate:
            ms = 1000.0 * d.running_quantum / d.running_rate
            rows += continuation(
                f"during the check: {d.running_rate} Hz, "
                f"{d.running_quantum}-sample cycles ({ms:.1f} ms)", gutter)
        else:
            rows += continuation("during the check: the output was idle", gutter)
    return rows


def dropouts_rows(d: session.Dropouts, pw_age: float | None, app_age: float | None,
                  gutter: int, *, app: str = "EasyEffects",
                  nodes: str = "EasyEffects' nodes",
                  busiest: str = "the busiest EasyEffects node",
                  into: str = "into EasyEffects") -> list[str]:
    """`Dropouts:` — pw-top's xrun counters on the output sink and on the
    chain's own nodes, then on a second line what happened during the check.

    The counters are cumulative from node creation — nothing rebases them;
    pw-top's `c` key clears only its own display — so a total means nothing
    without an age. The PipeWire process uptime (and *app*'s, when the chain
    lives in one) is the bound a paste can carry: a node is at most that
    old, and EasyEffects recreates its filter nodes on every pipeline
    restart, including the preset reload the generator performs, so its
    counter can be much younger. The growth during the doctor's own
    five-second window is the only "is it happening now"; a zero there with
    no stream running says nothing, and the row says which it was —
    "running", not "audible": any application's active playback stream keeps
    the graph running, silent or not. On a driver node, which the output
    sink usually is, ERR counts every cycle the whole graph missed, and the
    row says so. A count that could not be read says so rather than
    printing nothing: in a pasted report an absent row and a zero look
    alike, and the reassuring reading wins. "the output sink" is the
    `Output sink:` row above.
    """
    if not d.ok:
        return wrapped_row("Dropouts", f"not read ({d.reason})", gutter)
    # The plain word leads and the unit follows it: "0 xruns" alone read as
    # alarming to a reviewer until they worked out that 0 is the good number;
    # "xruns" stays because it is pw-top's column, what a maintainer greps a
    # paste for.
    has_sink, has_chain = d.sink is not None, d.chain is not None
    if not any(c for c in (d.sink, d.chain) if c):
        where = {(True, True): f"the output sink or any of {nodes}",
                 (True, False): "the output sink",
                 (False, True): f"any of {nodes}"}[(has_sink, has_chain)]
        lead = f"none (0 xruns) on {where}"
    else:
        parts = []
        if has_sink:
            parts.append(f"{d.sink} xruns on the output sink"
                         + (" (it drives the clock, so any node's dropout "
                            "counts there)" if d.sink_is_driver else ""))
        if has_chain:
            unit = "" if parts else " xruns"
            parts.append(f"none{unit} on {nodes}" if d.chain == 0 else
                         f"{d.chain}{unit} on {busiest} ({d.chain_node})")
        lead = ", ".join(parts)
    ages = []
    if pw_age is not None:
        ages.append(f"PipeWire's {session.format_age(pw_age)}")
    if app_age is not None:
        ages.append(f"{app}' {session.format_age(app_age)}"
                    if app.endswith("s") else f"{app}'s {session.format_age(app_age)}")
    since = " — since each node was created" + (
        f", at most {' / '.join(ages)} uptime" if ages else "")
    rows = wrapped_row("Dropouts", lead + since, gutter)
    if not any(c for c in (d.sink_recent, d.chain_recent) if c):
        now = f"none in {d.window_s:.0f} s"
    else:
        got = []
        if d.sink_recent is not None:
            got.append(f"{d.sink_recent} on the sink")
        if d.chain_recent is not None:
            got.append(f"{d.chain_recent} on {nodes}")
        now = f"{', '.join(got)} in {d.window_s:.0f} s"
    heard = ("a playback stream was running" if d.playing
             else f"nothing was playing {into}")
    return rows + continuation(f"during the check: {now}, {heard}", gutter)


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
