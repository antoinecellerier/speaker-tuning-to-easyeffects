"""PipeWire's session as `--doctor` reads it: versions, clock, dropouts.

Read-only wrappers around PipeWire's own tools — `pw-metadata -n settings`
for the clock (rate, quantum, its bounds, anything forced) and `pw-top -b`
for each node's xrun counter — plus the pure parsers that turn their text
into values, so the rows they feed can be tested without a daemon. Issue #84
is why they exist: a crackling report whose pasted `--doctor` output could
not say what quantum the chain was running at or whether the graph was
dropping buffers at all.

Never writes. Forcing a quantum is a whole-session change the user opts into
by hand (`docs/ee-to-pipewire.md`, "Small-quantum systems under load"), and
the one place this project does it — `tools/measure_perf/compare_paths.py` —
is a measurement harness behind the audio handoff.

Stdlib-only, deliberately: both doctors print from it through
`lib/report/doctor_layout.py`, and `tests/test_layout.py` lists it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, NamedTuple

# Names PipeWire gives EasyEffects' playback-path nodes: its virtual
# sink/source pair and the `ee_soe_*` (stream output effects) / `ee_sie_*`
# (input) filters. Read off a live `pw-top` on EasyEffects 8.2.8, which also
# lists an `ee_test_signals` node — its test-tone generator, not on the
# playback path, and deliberately not matched.
EASYEFFECTS_NODE_PREFIXES = ("easyeffects_", "ee_soe_", "ee_sie_")

_TIMEOUT = 5  # seconds — the same ceiling as the doctors' other probes

# `pw-top -b -n K` prints two snapshots at once on startup, then one per
# second (its refresh timer), so K iterations are a window of about K-2 s —
# measured: -n 4 returns in 2.0 s, -n 6 in 4.0 s. Five seconds is long enough
# for a dropout a listener hears as crackle to recur, and short enough not to
# double the doctor's run.
WINDOW_ITERATIONS = 7


@dataclass(frozen=True)
class ClockSettings:
    """What `pw-metadata -n settings` reports, as the strings it prints.

    ``reason`` is empty when the daemon answered; otherwise it says why not,
    in the words the report prints, so an absent value and a zero never look
    alike in a pasted report.
    """
    rate: str = ""
    quantum: str = ""
    min_quantum: str = ""
    max_quantum: str = ""
    force_quantum: str = ""
    force_rate: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


class NodeRow(NamedTuple):
    """One node as a `pw-top` snapshot prints it."""
    state: str      # S suspended, I idle, R running, C creating (pre-info)
    err: int        # the ERR column — see `Dropouts`
    quant: int      # QUANT/RATE: the clock a driver runs at; 0 on followers
    rate: int
    driver: str     # the driver this row runs under (itself for a driver)


@dataclass(frozen=True)
class Dropouts:
    """pw-top's xrun counters, read at both ends of a short window.

    Each counter is cumulative since its node was created — nothing rebases
    it; pw-top's ``c`` key clears only that instance's display — so the
    totals mean little without an age, and the growth over the window is
    what says whether dropouts are happening *now*. ``sink`` is the sink the
    chain plays into (``None`` when the caller named none or it isn't in the
    graph); on a driver node, which the sink usually is, pw-top's ERR counts
    every cycle the *graph* failed to complete — any node's fault — which
    ``sink_is_driver`` records. ``chain`` is the highest total among the
    chain's own nodes (EasyEffects', or a filter chain's) and ``chain_node``
    which one carries it; ``sink_recent`` / ``chain_recent`` the growth over
    ``window_s`` seconds (the latter the largest growth on any chain node);
    ``playing`` whether a chain node on the playback path was running at any
    point in the window — which any application's active playback stream
    causes, silent or not, and without which a zero says nothing.
    ``running_quantum`` / ``running_rate`` are the clock the sink's driver
    actually ran at during the window (0 when it never ran), as distinct
    from the session defaults `read_settings` reports. ``reason`` is set,
    and the rest empty, when nothing could be read.
    """
    sink: int | None = None
    chain: int | None = None
    chain_node: str = ""
    sink_recent: int | None = None
    chain_recent: int | None = None
    window_s: float = 0.0
    playing: bool = False
    sink_is_driver: bool = False
    running_quantum: int = 0
    running_rate: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def parse_settings(text: str) -> dict[str, str]:
    """`pw-metadata -n settings` output → ``{key: value}``.

    Each line reads ``update: id:0 key:'clock.rate' value:'48000' type:''``;
    the ``Found "settings" metadata 32`` preamble has neither marker and is
    skipped. Same split `tools/measure_perf/compare_paths.py` has used since
    the perf work.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "key:'" in line and "value:'" in line:
            key = line.split("key:'")[1].split("'")[0]
            value = line.split("value:'")[1].split("'")[0]
            values[key] = value
    return values


def _run(cmd: list[str], timeout: float = _TIMEOUT) -> str | None:
    """The subprocess boundary — stdout, or None when the tool couldn't run."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout


@dataclass(frozen=True)
class Version:
    """A component's version as its own tool reported it, or why not.

    ``text`` keeps every number the tool gave, not the two a comparison
    needs: the value is pasted into issues as well as judged, and "0.5" in
    a report reads as 0.5.0 — a build four years and fifteen patch releases
    away from the 0.5.15 that answered. ``parts`` is the same numbers for
    ordering; ``reason`` is why there is no version, in the words the report
    prints, so an absent value and a zero never look alike in a paste.
    """
    text: str = ""
    parts: tuple[int, ...] = ()
    reason: str = ""
    # Which claim the number makes — "running" (read from the live daemon)
    # or "installed" (the binary that answered) — printed beside it, because
    # the two can differ after an upgrade nobody restarted.
    claim: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def _version(out: str | None, no_answer: str, claim: str = "") -> Version:
    """A Version parsed off a tool's stdout, or the *no_answer* reason."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
    if not m:
        return Version(reason=no_answer)
    parts = tuple(int(g) for g in m.groups() if g is not None)
    return Version(text=".".join(str(v) for v in parts), parts=parts,
                   claim=claim)


def pipewire_version() -> Version:
    """The RUNNING daemon's version, off `pw-cli info 0` (its core object).

    Not `pipewire --version`: that is the installed binary's libpipewire,
    which is the wrong answer in exactly the case a crackle report cares
    about — an upgraded package under a daemon nobody restarted.
    """
    if shutil.which("pw-cli") is None:
        return Version(reason="pw-cli not found")
    out = _run(["pw-cli", "info", "0"])
    m = re.search(r'^\s*version:\s*"([^"]+)"', out or "", re.MULTILINE)
    return _version(m.group(1) if m else "", "no answer from pw-cli",
                    claim="running")


def wireplumber_version() -> Version:
    """The installed WirePlumber binary's version — the fallback when the
    running daemon's own number (its Client object in a pw-dump, read by
    the filter-chain doctor) isn't in hand, and the row says "installed"
    because that is all this probe can claim."""
    if shutil.which("wireplumber") is None:
        return Version(reason="wireplumber not found")
    return _version(_run(["wireplumber", "--version"]),
                    "no answer from wireplumber --version",
                    claim="installed")


def read_settings() -> ClockSettings:
    """The session clock, or a ``ClockSettings`` whose ``reason`` says why not."""
    if shutil.which("pw-metadata") is None:
        return ClockSettings(reason="pw-metadata not found")
    values = parse_settings(_run(["pw-metadata", "-n", "settings"]) or "")
    if not values:
        return ClockSettings(reason="no answer from pw-metadata")
    return ClockSettings(
        rate=values.get("clock.rate", ""),
        quantum=values.get("clock.quantum", ""),
        min_quantum=values.get("clock.min-quantum", ""),
        max_quantum=values.get("clock.max-quantum", ""),
        force_quantum=values.get("clock.force-quantum", ""),
        force_rate=values.get("clock.force-rate", ""),
    )


def parse_pwtop(text: str) -> list[dict[str, NodeRow]]:
    """Batch `pw-top` output → one dict per snapshot: node name → `NodeRow`.

    Columns are ``S ID QUANT RATE WAIT BUSY W/Q B/Q ERR FORMAT NAME``; every
    snapshot repeats the header, which is how snapshots are told apart.
    ``FORMAT`` is blank for follower nodes and three tokens (``F32LE 2
    48000``) for drivers, and a follower's name is prefixed with ``+`` (``=``
    for an async one) and listed under its driver, so the state, clock and
    ERR are read by position from the left, the name from the right, and the
    driver is the last unprefixed row above. A node that has never run
    prints ``---`` for its times, ``S`` for its state and ``0`` for ERR;
    ``R`` is a running node.
    """
    snapshots: list[dict[str, NodeRow]] = []
    current: dict[str, NodeRow] | None = None
    driver = ""
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        if fields[0] == "S" and fields[1] == "ID":
            if current is not None:
                snapshots.append(current)
            current = {}
            driver = ""
            continue
        if current is None:
            current = {}
        try:
            int(fields[1])
            quant, rate, err = int(fields[2]), int(fields[3]), int(fields[8])
        except ValueError:
            continue
        name = fields[-1]
        if fields[-2] not in ("+", "="):
            driver = name
        current[name] = NodeRow(fields[0], err, quant, rate, driver or name)
    if current:
        snapshots.append(current)
    return snapshots


# EasyEffects' input side runs for a microphone, not for sound: a chain node
# with one of these names does not count as "playing".
_CAPTURE_SIDE_PREFIXES = ("easyeffects_source", "ee_sie_")


def read_xruns(sink: str = "",
               chain_prefixes: Iterable[str] = EASYEFFECTS_NODE_PREFIXES,
               chain_names: Iterable[str] = (),
               iterations: int = WINDOW_ITERATIONS) -> Dropouts:
    """The chain's dropout counters over a ~``iterations``-2 s window.

    The chain's nodes are those whose names start with one of
    ``chain_prefixes`` (EasyEffects' by default) or equal one of
    ``chain_names`` (a filter chain's ``effect_input.X`` / ``effect_output.X``);
    ``sink`` is the exact name of the sink they play into, counted and
    reported on its own — a dropout there is heard just the same.
    """
    if shutil.which("pw-top") is None:
        return Dropouts(reason="pw-top not found")
    started = time.monotonic()
    out = _run(["pw-top", "-b", "-n", str(iterations)],
               timeout=iterations + _TIMEOUT)
    window = time.monotonic() - started
    snapshots = parse_pwtop(out or "")
    if not snapshots:
        return Dropouts(reason="pw-top didn't answer")
    # The very first snapshot is printed before the nodes' info has arrived:
    # every state reads `C` and every ERR 0 (seen live), so it cannot be the
    # window's baseline — the second one is the first with real counts.
    first = snapshots[1] if len(snapshots) > 1 else snapshots[0]
    last = snapshots[-1]
    prefixes, names = tuple(chain_prefixes), set(chain_names)
    chain_nodes = [n for n in last
                   if (prefixes and n.startswith(prefixes)) or n in names]
    sinks = [n for n in last if n == sink] if sink else []
    if not chain_nodes and not sinks:
        return Dropouts(reason="none of the chain's nodes are in the graph")

    def growth(name: str) -> int:
        before = first.get(name)
        return last[name].err - (before.err if before else last[name].err)

    chain_node = max(chain_nodes, key=lambda n: last[n].err) if chain_nodes else ""
    # "Playing" is judged on the chain's playback-path nodes, not the sink:
    # the sink runs whenever the chain's stream is attached to it, while
    # `easyeffects_sink`, the `ee_soe_*` filters and a filter chain's own
    # nodes run only while an app holds a playback stream into them (they
    # read `S` on an idle graph) — which a browser tab with an open audio
    # context does, silently.
    playback_side = [n for n in chain_nodes
                     if not n.startswith(_CAPTURE_SIDE_PREFIXES)]
    playing = any(snap[n].state == "R" for snap in snapshots
                  for n in playback_side if n in snap)
    # The clock the output really ran at: its driver's QUANT/RATE in any
    # real snapshot where that driver was running.
    running_quantum = running_rate = 0
    if sinks:
        drv = last[sink].driver
        for snap in snapshots[1:]:
            row = snap.get(drv)
            if row and row.state == "R" and row.quant:
                running_quantum, running_rate = row.quant, row.rate
    return Dropouts(
        sink=last[sink].err if sinks else None,
        chain=last[chain_node].err if chain_node else None,
        chain_node=chain_node,
        sink_recent=growth(sink) if sinks else None,
        chain_recent=max(growth(n) for n in chain_nodes) if chain_nodes else None,
        window_s=round(window, 1),
        playing=playing,
        sink_is_driver=bool(sinks) and last[sink].driver == sink,
        running_quantum=running_quantum,
        running_rate=running_rate,
    )


def age_from_stat(stat: str, uptime_s: float, clk_tck: int) -> float | None:
    """Pure: ``/proc/<pid>/stat`` + ``/proc/uptime`` → seconds since the
    process started. Field 22 is the start time in clock ticks since boot;
    the ``comm`` field (2) is parenthesised and may hold spaces, so the split
    starts after the last ``)``, where the state (field 3) comes first."""
    try:
        after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[22 - 3])
    except (IndexError, ValueError):
        return None
    return max(0.0, uptime_s - start_ticks / clk_tck)


def process_age(name: str) -> float | None:
    """Seconds since the oldest process called *name* started, or None."""
    if shutil.which("pgrep") is None:
        return None
    pid = (_run(["pgrep", "-x", "-o", name]) or "").strip()
    if not pid.isdigit():
        return None
    try:
        stat = open(f"/proc/{pid}/stat", encoding="utf-8").read()
        uptime = float(open("/proc/uptime", encoding="utf-8").read().split()[0])
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, AttributeError):
        return None
    return age_from_stat(stat, uptime, clk_tck)


def format_age(seconds: float) -> str:
    """``3 d 4 h`` / ``2 h 5 min`` / ``48 s`` — the two largest units."""
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} d {hours} h" if hours else f"{days} d"
    if hours:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"
