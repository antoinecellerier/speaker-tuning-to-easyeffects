"""PipeWire's session as `--doctor` reads it: versions, clock, dropouts.

Read-only wrappers around PipeWire's own tools: `pw-metadata -n settings`
for the clock (rate, quantum, its bounds, anything forced) and `pw-top -b`
for each node's xrun counter. Pure parsers turn their text into values, so
the rows they feed can be tested without a daemon. Issue #84 is why they
exist: a crackling report whose pasted `--doctor` output could not say what
quantum the chain was running at or whether the graph was dropping buffers
at all.

Never writes. Forcing a quantum is a whole-session change the user opts into
by hand (`docs/ee-to-pipewire.md`, "Small-quantum systems under load"). The
one place this project does it, `tools/measure_perf/compare_paths.py`, is a
measurement harness behind the audio handoff.

`lv2_loader` is the one probe that asks PipeWire to load something, and what
it asks for cannot load, so it too leaves the session as it found it.

Stdlib-only, deliberately: both doctors print from it through
`lib/report/doctor_layout.py`, and `tests/test_layout.py` lists it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, NamedTuple

from lib import host, tool_env

# Names PipeWire gives EasyEffects' playback-path nodes: its virtual
# sink/source pair and the `ee_soe_*` (stream output effects) / `ee_sie_*`
# (input) filters. Read off a live `pw-top` on EasyEffects 8.2.8, which also
# lists an `ee_test_signals` node. That is its test-tone generator, not on the
# playback path, and deliberately not matched.
EASYEFFECTS_NODE_PREFIXES = ("easyeffects_", "ee_soe_", "ee_sie_")

_TIMEOUT = 5  # seconds — the same ceiling as the doctors' other probes

# `pw-top -b -n K` prints two snapshots at once on startup, then one per
# second (its refresh timer), so K iterations are a window of about K-2 s.
# Measured: -n 4 returns in 2.0 s, -n 6 in 4.0 s. Five seconds is long enough
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

    Each counter is cumulative since its node was created. Nothing rebases
    it: pw-top's ``c`` key clears only that instance's display. So the
    totals mean little without an age, and the growth over the window is
    what says whether dropouts are happening *now*. ``sink`` is the sink the
    chain plays into (``None`` when the caller named none or it isn't in the
    graph). On a driver node, which the sink usually is, pw-top's ERR counts
    every cycle the *graph* failed to complete, whichever node was at fault;
    ``sink_is_driver`` records that. ``chain`` is the highest total among the
    chain's own nodes (EasyEffects', or a filter chain's) and ``chain_node``
    which one carries it. ``sink_recent`` / ``chain_recent`` are the growth
    over ``window_s`` seconds (the latter the largest growth on any chain
    node). ``playing`` is whether a chain node on the playback path was
    running at any point in the window. Any application's active playback
    stream causes that, silent or not, and without it a zero says nothing.
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
    # True when the busiest chain node IS the sink (EasyEffects' own sink as
    # the default output): one node, one count. The renderer must not hang
    # two labels on it, which reads as twice the dropouts.
    sink_is_chain_node: bool = False
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
    skipped. Same split `tools/measure_perf/compare_paths.py` uses.
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "key:'" in line and "value:'" in line:
            key = line.split("key:'")[1].split("'")[0]
            value = line.split("value:'")[1].split("'")[0]
            values[key] = value
    return values


def _run(cmd: list[str], timeout: float = _TIMEOUT) -> str | None:
    """The subprocess boundary: stdout, or None when the tool couldn't run."""
    try:
        result = tool_env.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return result.stdout


@dataclass(frozen=True)
class Version:
    """A component's version as its own tool reported it, or why not.

    ``text`` keeps every number the tool gave, not the two a comparison
    needs: the value is pasted into issues as well as judged. "0.5" in a
    report reads as 0.5.0, a build four years and fifteen patch releases
    away from the 0.5.15 that answered. ``parts`` is the same numbers for
    ordering. ``reason`` is why there is no version, in the words the report
    prints, so an absent value and a zero never look alike in a paste.
    """
    text: str = ""
    parts: tuple[int, ...] = ()
    reason: str = ""
    # Which claim the number makes: "running" (read from the live daemon)
    # or "installed" (the binary that answered). It is printed beside the
    # number, because the two can differ after an upgrade nobody restarted.
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
    about: an upgraded package under a daemon nobody restarted.
    """
    if tool_env.which("pw-cli") is None:
        return Version(reason="pw-cli not found")
    out = _run(["pw-cli", "info", "0"])
    m = re.search(r'^\s*version:\s*"([^"]+)"', out or "", re.MULTILINE)
    return _version(m.group(1) if m else "", "no answer from pw-cli",
                    claim="running")


def wireplumber_version() -> Version:
    """The installed WirePlumber binary's version.

    The fallback when the running daemon's own number (its Client object in
    a pw-dump, read by the filter-chain doctor) isn't in hand. The row says
    "installed" because that is all this probe can claim."""
    if tool_env.which("wireplumber") is None:
        return Version(reason="wireplumber not found")
    return _version(_run(["wireplumber", "--version"]),
                    "no answer from wireplumber --version",
                    claim="installed")


# The graph rate a copy preview needs, which no XML can reach and this machine
# will not be in: the fault is a session-wide PipeWire setting, so the only way
# to render the warning it raises is to answer as a session that has it. Same
# convention as `DEMO_SPEAKER_PIN` and friends (`lib/hardware/speakers.py`).
# `DEMO_GRAPH_RATE=192000` sets the session default; `384000:192000` sets a
# default the hardware caps below, which is issue #84's own shape and the one
# arm whose wording differs.
_DEMO_GRAPH_RATE = "DEMO_GRAPH_RATE"


def _maybe_demo_clock() -> "ClockSettings | None":
    """A stubbed clock when the demo hook is set, else None.

    Read here rather than in the report because the report must render the
    *shipped* sentence: a preview that fabricated a warning would prove only
    that the harness can print. This substitutes the machine's answer and lets
    the real check decide, exactly as the speaker-pin hook does.
    """
    raw = (os.environ.get(_DEMO_GRAPH_RATE) or "").strip()
    if not raw:
        return None
    rate = raw.split(":")[0]
    if not rate.isdigit():
        return None
    return ClockSettings(rate=rate, quantum="1024", min_quantum="32",
                         max_quantum="2048", force_quantum="0", force_rate="0")


def read_settings() -> ClockSettings:
    """The session clock, or a ``ClockSettings`` whose ``reason`` says why not."""
    demo = _maybe_demo_clock()
    if demo is not None:
        return demo
    if tool_env.which("pw-metadata") is None:
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
    reported on its own: a dropout there is heard just the same.
    """
    if tool_env.which("pw-top") is None:
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
    # window's baseline. The second one is the first with real counts.
    first = snapshots[1] if len(snapshots) > 1 else snapshots[0]
    last = snapshots[-1]
    prefixes, names = tuple(chain_prefixes), set(chain_names)
    chain_nodes = [n for n in last
                   if (prefixes and n.startswith(prefixes)) or n in names]
    sinks = [n for n in last if n == sink] if sink else []
    if not chain_nodes and not sinks:
        # Name every miss: with a sink requested, "the chain's nodes" alone
        # blames the wrong half when the sink was the absentee.
        return Dropouts(reason=("neither the output sink nor any of the "
                                "chain's nodes is in the graph") if sink
                        else "none of the chain's nodes are in the graph")

    def growth(name: str) -> int:
        before = first.get(name)
        return last[name].err - (before.err if before else last[name].err)

    chain_node = max(chain_nodes, key=lambda n: last[n].err) if chain_nodes else ""
    # "Playing" is judged on the chain's playback-path nodes, not the sink:
    # the sink runs whenever the chain's stream is attached to it, while
    # `easyeffects_sink`, the `ee_soe_*` filters and a filter chain's own
    # nodes run only while an app holds a playback stream into them (they
    # read `S` on an idle graph). A browser tab with an open audio context
    # holds one, silently.
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
        sink_is_chain_node=bool(sink and chain_node == sink),
        sink_recent=growth(sink) if sinks else None,
        chain_recent=max(growth(n) for n in chain_nodes) if chain_nodes else None,
        window_s=round(window, 1),
        playing=playing,
        sink_is_driver=bool(sinks) and last[sink].driver == sink,
        running_quantum=running_quantum,
        running_rate=running_rate,
    )


def age_from_stat(stat: str, uptime_s: float, clk_tck: int) -> float | None:
    """Seconds since a process started, from its stat line and the uptime.

    Pure: ``/proc/<pid>/stat`` + ``/proc/uptime`` → seconds since the
    process started. Field 22 is the start time in clock ticks since boot.
    The ``comm`` field (2) is parenthesised and may hold spaces, so the split
    starts after the last ``)``, where the state (field 3) comes first."""
    try:
        after_comm = stat.rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[22 - 3])
    except (IndexError, ValueError):
        return None
    return max(0.0, uptime_s - start_ticks / clk_tck)


# The plugin the LV2 probe asks for. A URN no LV2 bundle declares, named after
# the entry point that owns the check (`dolby_to_pipewire.py` runs it through
# `ee_to_pipewire.py`), so the one line it can leave in a log says whose it is.
LV2_PROBE_URI = "urn:ee-to-pipewire:lv2-loader-probe"

# A graph of one LV2 node naming that plugin. It can never load, so the module
# fails and creates nothing; where it fails is the answer.
_LV2_PROBE_GRAPH = ("{ filter.graph = { nodes = [ { type = lv2 name = probe "
                    f'plugin = "{LV2_PROBE_URI}" }} ] }} }}')

# What PipeWire logs on the way to that failure, at warning level. Read off
# PipeWire 1.6.9 here, loader present and loader absent (an `SPA_PLUGIN_DIR`
# tree without it), and found verbatim in the 1.2.7 source
# (`lv2_plugin.c`, `module-filter-chain.c`), where the loader is a module
# rather than an SPA plugin. Matched as substrings, never on the errno text
# after them.
_LV2_LOOKED_UP = f"can't load plugin {LV2_PROBE_URI}"   # the loader ran lilv
_LV2_TYPE_FAILED = "can't load plugin type 'lv2'"
_LV2_NOT_BUILT = "compiled without lv2 support"         # PipeWire <= 0.3.72


@dataclass(frozen=True)
class Lv2Loader:
    """Whether PipeWire can load LV2 plugins here, as PipeWire answered.

    ``present`` is None when the question went unanswered, and ``reason``
    then says why in the words the report prints. ``built_without`` marks the
    absent case no package can fix: a PipeWire built without LV2 at all.
    ``line`` is PipeWire's own error line when the loader is absent, worth
    having verbatim in a pasted report.
    """
    present: bool | None
    reason: str = ""
    line: str = ""
    built_without: bool = False


# `[E][21:32:56.993136] spa.filter-graph | [  filter-graph.c:  934
# plugin_load()] message`: level, timestamp, topic, source location. The
# topic is absent on older builds, the location under PIPEWIRE_LOG_LINE=false.
_PW_LOG_LINE = re.compile(
    r"^\[[A-Z]\]\[[^]]*\]\s*(?:(?P<topic>[\w.-]+)\s*\|\s*)?"
    r"(?:\[[^]]*\]\s*)?(?P<msg>.*)$")


def _journal_form(line: str) -> str:
    """A PipeWire stderr log line as the journal shows it: `topic: message`.

    That is the form a reader has already met in `journalctl`, and the one a
    search for the error finds."""
    m = _PW_LOG_LINE.match(line.strip())
    if not m:
        return line.strip()
    topic, msg = m.group("topic"), m.group("msg").strip()
    return f"{topic}: {msg}" if topic else msg


def classify_lv2_probe(output: str) -> Lv2Loader:
    """Pure: the probe's combined output → an `Lv2Loader`.

    Our URI in a "can't load plugin" line means the LV2 loader loaded and
    asked lilv for it, so the loader is there. A failure to load the `lv2`
    plugin *type* without that line means it isn't: the reporter's
    `spa.filter-graph: can't load plugin type 'lv2': No such file or
    directory` (issue #123). Anything else is not an answer. A bare
    `Error: "Could not load module"` is what a missing filter-chain module and
    a missing graph core (Arch's `pipewire-audio`) both print, so it stays
    unknown rather than guess between them.
    """
    lines = output.splitlines()
    if any(_LV2_LOOKED_UP in ln for ln in lines):
        return Lv2Loader(present=True)
    for marker, built_without in ((_LV2_NOT_BUILT, True),
                                  (_LV2_TYPE_FAILED, False)):
        hit = next((ln for ln in lines if marker in ln), None)
        if hit is not None:
            return Lv2Loader(present=False, line=_journal_form(hit),
                             built_without=built_without)
    if "Could not load module" in output:
        return Lv2Loader(present=None, reason="PipeWire's filter-chain module "
                         "didn't load at all, so its LV2 support couldn't be "
                         "checked")
    return Lv2Loader(present=None, reason="pw-cli gave no answer about LV2 "
                     "support")


def _probe_lv2_loader() -> Lv2Loader:
    """Ask PipeWire to load the probe graph, and classify what it says.

    `pw-cli load-module` loads the module in pw-cli's own process, with the
    same plugin search the daemon uses: `SPA_PLUGIN_DIR` or the compiled-in
    directory, and the 1.2 or 1.4 layout, without this code knowing either.
    pw-cli connects before it parses its command, so with no daemon it exits
    at once. It exits 0 on a failed load, hence the text.

    `PIPEWIRE_LOG_SYSTEMD=false` keeps the probe out of the journal, where
    its absent-case line would be indistinguishable from a real chain
    failing. `PIPEWIRE_LOG` would divert the lines to a file, so it goes.
    The environment is ours, not the daemon's: a `SPA_PLUGIN_DIR` set only in
    the daemon's unit is not seen.
    """
    try:
        if host.path("/run/.containerenv").exists():
            # Toolbox and distrobox: pw-cli would load the container's
            # modules, and the daemon is the host's.
            return Lv2Loader(present=None, reason="running in a container, "
                             "where pw-cli loads the container's PipeWire "
                             "modules rather than the host's")
    except OSError:
        pass
    if tool_env.which("pw-cli") is None:
        return Lv2Loader(present=None, reason="pw-cli not found")
    try:
        result = tool_env.run(
            ["pw-cli", "load-module", "libpipewire-module-filter-chain",
             _LV2_PROBE_GRAPH],
            env_extra={"PIPEWIRE_DEBUG": "2", "PIPEWIRE_LOG_SYSTEMD": "false",
                       "PIPEWIRE_LOG": None},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            timeout=_TIMEOUT)
    except (subprocess.SubprocessError, OSError):
        return Lv2Loader(present=None, reason="pw-cli didn't answer")
    return classify_lv2_probe(result.stdout or "")


_lv2_loader_answer: Lv2Loader | None = None


def lv2_loader() -> Lv2Loader:
    """`_probe_lv2_loader`, asked once per process once it has an answer.

    The wrapper converts up to three presets per run and the activation check
    asks again after the restart. A restart installs nothing, so a present or
    absent answer holds for the run. An unknown one doesn't: a daemon that
    was down or slow before the restart can answer after it.
    """
    global _lv2_loader_answer
    if _lv2_loader_answer is not None:
        return _lv2_loader_answer
    answer = _probe_lv2_loader()
    if answer.present is not None:
        _lv2_loader_answer = answer
    return answer


def forget_lv2_loader() -> None:
    """Drop the cached answer (tests, which stub the probe per case)."""
    global _lv2_loader_answer
    _lv2_loader_answer = None


def process_age(name: str) -> float | None:
    """Seconds since the oldest process called *name* started, or None."""
    if tool_env.which("pgrep") is None:
        return None
    pid = (_run(["pgrep", "-x", "-o", name]) or "").strip()
    if not pid.isdigit():
        return None
    try:
        stat = host.path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        uptime = float(host.path("/proc/uptime").read_text(
            encoding="utf-8").split()[0])
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
