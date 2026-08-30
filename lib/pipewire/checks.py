"""``--doctor`` for the PipeWire path: what is installed, and what it is doing.

The EasyEffects doctor (``lib/report/environment.py``) checks the environment a
preset lands in; this is the same idea one layer down, where there is more to
get wrong and less to see. Probing and judging are kept apart — ``_pw_dump``,
``parse_conf`` and ``installed_confs`` read the
system — ``_probe_plugins`` too, which is why the plugin listing and the plugin
check read one answer between them — and every ``check_*`` below is a pure
function over what those returned, so states this machine cannot produce are
still unit-testable (``tests/test_pw_doctor.py``).

``warn_if_stacked`` lives here rather than in ``install.py`` despite firing at
write time: it is the stacked-chain diagnosis of ``check_stacked_chains``,
asked of the confs on disk instead of the live graph, and it reads them through
the same ``installed_confs``.

``DEFAULT_OUTPUT_DIR`` is defined here too, beside the ``_UNSCANNED_CONF_DIR``
it is the counterpart of: every check that names the directory reads it here —
``check_conf_directory``, ``gather_pw_doctor``, ``report_pw_doctor`` — and it
is the one constant the suite patches to point the doctor at a temporary tree,
so it has to be one binding, not a name copied in from elsewhere. Its readers
outside are the two converters, which import this module either way:
``ee_to_pipewire.py`` reaches it through ``default_conf_path``, and
``dolby_to_pipewire.py`` spells it twice, for ``--output-dir``'s help default
and again in ``main``.

The report vocabulary — PASS/WARN/FAIL/UNKNOWN, ``CheckResult``, the summary
counter, the check printer and the ``~``-collapsing path renderer — comes from
``lib/doctor.py``, shared with the EasyEffects doctor so the two read as one
tool. The constants and the plugin URIs come in under bare names because
string constants and a record type hold no state a patch would have to reach.
The functions stay module-qualified.

This doctor ends on the same hardware dump the EasyEffects one prints, because
hardware sits under the whole chain and the questions it answers are the same
either side. It comes from ``lib/report/speaker.py``, imported at the top now
that it is a sibling under ``lib/`` — it used to be a deferred ``import
dolby_to_easyeffects`` guarded by a ``try``, because reaching it meant paying
the generator's NumPy/SciPy on a path that does no DSP. It no longer does: the
report costs a few milliseconds of stdlib on top of what this module already
imports, and an ImportError on a lib sibling is a bug rather than a condition
to degrade around. It keeps the local name ``gen`` the deferred import gave
it — the two call lines are moved lines, and a move commit may not re-point
what it carries.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lib import console, doctor, ee_socket, packages, version
from lib.hardware import sinks as hw_sinks
from lib.doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from lib.pipewire.conf import CONF_HEADER_MARK, PIPEWIRE_RESTART_CMD
from lib.pipewire import session, vbe
from lib.pipewire.plugins import (
    CALF_BE_URI,
    CALF_ST_URI,
    LSP_AUTOGAIN_URI,
    LSP_LIM_URI,
    LSP_MBC_URI,
    LSP_PEQ_URI,
)
from lib.report import doctor_layout as layout
from lib.report import speaker as gen


# PipeWire's stock pipewire.conf only auto-includes the
# pipewire.conf.d/*.conf overlay set; filter-chain.conf.d/ is *not*
# scanned by the daemon (it's the path for the standalone
# `pipewire -c filter-chain.conf` invocation pattern, used by the
# measurement rig). Drop the conf in pipewire.conf.d/ so the running
# daemon picks it up on the next restart.
DEFAULT_OUTPUT_DIR = Path.home() / ".config/pipewire/pipewire.conf.d"


# --- PipeWire-side diagnostics (--doctor) -----------------------------------
#
# The EasyEffects doctor checks the environment a preset lands in. This is the
# same idea for the PipeWire path, where there is more to get wrong and less
# to see: the chain lives in conf files nobody reads, nothing reports its own
# state, and the remedy is almost always "delete a file and restart PipeWire",
# which nobody would guess.
#
# Probing and judging are kept apart — every check below is a pure function
# over already-gathered data, so the states this machine can't produce (an old
# WirePlumber, a missing plugin, a target sink that vanished) are still
# testable.

# Both directories a conf can end up in. Only the first is loaded by the
# daemon: the stock pipewire.conf auto-includes pipewire.conf.d/, while
# filter-chain.conf.d/ is for the standalone `pipewire -c filter-chain.conf`
# invocation and is never scanned by the running daemon (see
# DEFAULT_OUTPUT_DIR). It is not dead either way — the stock
# filter-chain.service runs that standalone invocation and does read it, so a
# conf there is live for anyone running that unit, which is why the check
# below offers moving it rather than declaring it inert.
_UNSCANNED_CONF_DIR = Path.home() / ".config/pipewire/filter-chain.conf.d"


def live_conf_dirs() -> tuple[Path, Path]:
    """Every directory a conf can be live from: the daemon's, and the one
    filter-chain.service reads. For a writer about to remove a file a conf
    names — a convolver whose file is gone stops the whole conf loading,
    whichever unit was loading it. A function so a test can repoint either
    directory."""
    return (DEFAULT_OUTPUT_DIR, _UNSCANNED_CONF_DIR)

# effect_input.X and effect_output.X are the two halves of chain X. In
# smart-filter mode node.link-group joins them; the v1 virtual-sink conf sets
# no link group, so the name is the only thing that does.
_CHAIN_NODE_RE = re.compile(r"^effect_(input|output)\.(.+)$")


@dataclass
class InstalledConf:
    """A filter-chain conf found on disk, as far as we could read it."""
    path: Path
    version: str = ""        # from the "# version:" header
    node_name: str = ""      # capture node.name
    smart: bool = False
    target: str = ""         # filter.smart.target node.name
    pinned: str = ""         # playback target.object
    irs: list = field(default_factory=list)
    plugins: list = field(default_factory=list)   # the LV2 URIs it names, so
                             # a check can judge the plugins this machine's
                             # chain needs rather than every one the converter
                             # is capable of emitting
    readable: bool = True    # False when spa-json-dump couldn't parse it
    unreadable: str = ""     # and why not. "unreadable" on its own reads as a
                             # damaged conf, while the commonest cause is a
                             # tool this machine simply hasn't got — which the
                             # converter's own path already names and this one
                             # did not, two answers on one machine.


@dataclass
class LiveChain:
    """A filter chain as the running graph reports it."""
    name: str                # the X in effect_input.X
    smart: bool = False
    target: str = ""
    pinned: str = ""


@dataclass
class DefaultSink:
    """Which sink audio follows, and which one the user chose by hand."""
    effective: str = ""      # default.audio.sink — where streams go now
    configured: str = ""     # default.configured.audio.sink — the explicit pick


def _pw_dump() -> list | None:
    """Every PipeWire object, or None when the daemon can't be reached.

    The doctor's single ``pw-dump`` boundary — tests monkeypatch it to feed
    synthetic graphs. Deliberately not lib.hardware.sinks._enumerate_audio_sinks:
    that reduces to Audio/Sink nodes with a fixed field set, and these checks
    need filter.smart*, node.link-group and target.object on every node.
    """
    try:
        result = subprocess.run(["pw-dump"], capture_output=True, text=True,
                                timeout=5)
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, list) else None


# The one unreadable cause with a remedy, so the reason is a constant rather
# than a phrase spelled twice: `check_conf_contents` keys off it to offer the
# package, and the Environment line prints it. The other two causes have no
# command behind them, so they are only ever text.
NO_SPA_JSON_DUMP = "spa-json-dump not installed"


def parse_conf(path: Path) -> InstalledConf:
    """Read back one of our confs. Values come from spa-json-dump rather than
    a hand-rolled parser (CLAUDE.md: wrap the existing tool); without it the
    conf is marked unreadable and the checks that need its contents say so
    instead of guessing."""
    conf = InstalledConf(path=path)
    try:
        head = path.read_text(errors="replace")
    except OSError:
        conf.readable = False
        conf.unreadable = "couldn't open the file"
        return conf
    m = re.search(r"^# version:\s*(\S+)", head, re.MULTILINE)
    conf.version = m.group(1) if m else ""
    if shutil.which("spa-json-dump") is None:
        conf.readable = False
        conf.unreadable = NO_SPA_JSON_DUMP
        return conf
    try:
        dumped = subprocess.run(["spa-json-dump", str(path)],
                                capture_output=True, text=True, timeout=10)
        data = json.loads(dumped.stdout)
        args = data["context.modules"][0]["args"]
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError,
            KeyError, IndexError, TypeError):
        # Kept apart from the missing-tool case above: this one is a conf
        # nothing can make sense of, and no package fixes it.
        conf.readable = False
        conf.unreadable = "spa-json-dump couldn't parse it"
        return conf
    capture = args.get("capture.props", {})
    playback = args.get("playback.props", {})
    conf.node_name = capture.get("node.name", "")
    conf.smart = bool(capture.get("filter.smart"))
    conf.target = (capture.get("filter.smart.target") or {}).get("node.name", "")
    conf.pinned = playback.get("target.object", "")
    nodes = [n for n in args.get("filter.graph", {}).get("nodes", [])
             if isinstance(n, dict)]
    # Deduplicated: the stereo convolver is two nodes reading the same file,
    # so a missing IRS would otherwise be reported once per channel.
    conf.irs = list(dict.fromkeys(
        Path(n["config"]["filename"])
        for n in nodes if "filename" in n.get("config", {})))
    conf.plugins = list(dict.fromkeys(
        n["plugin"] for n in nodes
        if n.get("type") == "lv2" and n.get("plugin")))
    return conf


def installed_confs(*dirs: Path) -> list[InstalledConf]:
    """Every conf this tool wrote, across the directories it might be in."""
    found = []
    for d in dirs:
        try:
            paths = sorted(d.glob("*.conf"))
        except OSError:
            continue
        for p in paths:
            try:
                if not p.read_text(errors="replace").startswith(CONF_HEADER_MARK):
                    continue
            except OSError:
                continue
            found.append(parse_conf(p))
    return found


def _target_node_name(target) -> str:
    """The node.name out of a filter.smart.target, whichever shape it arrives in.

    The conf declares it as a SPA-JSON object, and spa-json-dump hands it back
    as a dict — but pw-dump reports the *property* verbatim, so from the live
    graph the same value is the string `{ node.name = "..." }`. Taking that
    string as the name made every live smart filter look like it pointed at a
    sink that doesn't exist.
    """
    if isinstance(target, dict):
        return target.get("node.name", "")
    if isinstance(target, str):
        m = re.search(r'node\.name\s*=\s*"([^"]+)"', target)
        return m.group(1) if m else target.strip()
    return ""


def live_chains(dump) -> list[LiveChain]:
    """The filter chains present in a pw-dump, joined across their two nodes."""
    chains: dict[str, LiveChain] = {}
    for obj in dump or []:
        if not str(obj.get("type", "")).endswith("Node"):
            continue
        props = obj.get("info", {}).get("props", {})
        m = _CHAIN_NODE_RE.match(str(props.get("node.name", "")))
        if not m:
            continue
        half, name = m.group(1), m.group(2)
        chain = chains.setdefault(name, LiveChain(name=name))
        if half == "input":
            chain.smart = bool(props.get("filter.smart"))
            chain.target = _target_node_name(props.get("filter.smart.target"))
        else:
            chain.pinned = props.get("target.object", "") or ""
    return list(chains.values())


def sink_names(dump) -> set[str]:
    """node.name of every Audio/Sink in a pw-dump."""
    names = set()
    for obj in dump or []:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Sink":
            names.add(props.get("node.name", ""))
    return names - {""}


def _metadata_node_name(value) -> str:
    """The node.name inside a `{ "name": ... }` metadata value.

    pw-dump parses Spa:String:JSON values into dicts, but the same two-shape
    tolerance ``_target_node_name`` needs applies here for the same reason:
    guessing wrong makes every check below read an empty default and go quiet,
    which is indistinguishable from "nothing to report".
    """
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value.strip()
        return str(parsed.get("name") or "") if isinstance(parsed, dict) else ""
    return ""


# The two keys the "default" Metadata object carries for outputs. `effective` is
# what the session is doing; `configured` is the user's own pick, which
# WirePlumber stores in ~/.local/state/wireplumber/default-nodes and re-applies
# whenever a node of that name exists again. That second one is why this is
# worth reading at all: no sound-settings UI shows it, it outlives the sink it
# names, and default-nodes/find-selected-default-node.lua scores it 30000 +
# priority.session — far above anything a conf we write could declare.
_DEFAULT_SINK_KEYS = {"default.audio.sink": "effective",
                      "default.configured.audio.sink": "configured"}


def default_sinks(dump) -> DefaultSink:
    """The default-sink metadata out of a pw-dump, empty when it isn't there."""
    found = DefaultSink()
    for obj in dump or []:
        if not str(obj.get("type", "")).endswith("Metadata"):
            continue
        # A Metadata object carries its props at the top level, where a Node
        # keeps them under "info". Accepting both costs one `or` and makes a
        # dump shape we guessed wrong about degrade to "no answer" rather than
        # a confident wrong one.
        props = obj.get("props") or obj.get("info", {}).get("props", {})
        if props.get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata") or []:
            field_name = _DEFAULT_SINK_KEYS.get(entry.get("key"))
            if field_name:
                setattr(found, field_name,
                        _metadata_node_name(entry.get("value")))
    return found


def sink_volumes(dump) -> dict[str, float]:
    """node.name → the linear volume PipeWire is applying, per Audio/Sink.

    Linear amplitude, not the percentage sound settings show: PulseAudio maps
    its 0-100 % through a cube, so 40 % is 0.064 and −23.8 dB. Both renderings
    matter — the percentage is what the reader recognises from their own
    settings, the dB is the size of the problem — so this returns the raw value
    and ``_volume_reading`` formats it.
    """
    vols = {}
    for obj in dump or []:
        info = obj.get("info") or {}
        if (info.get("props") or {}).get("media.class") != "Audio/Sink":
            continue
        name = (info.get("props") or {}).get("node.name")
        for param in (info.get("params") or {}).get("Props", []) or []:
            channels = param.get("channelVolumes")
            if name and channels:
                vols[name] = max(float(v) for v in channels)
    return vols


def _volume_reading(linear: float) -> str:
    """"40 % (−23.8 dB)" — the way the reader's own settings would show it."""
    percent = round(100 * (linear ** (1 / 3)))
    if linear <= 0:
        return "0 % (silent)"
    return f"{percent} % ({20 * math.log10(linear):.1f} dB)"


def downstream_sink(dump, chain_name: str) -> str:
    """The sink a chain's playback is actually linked into, or "".

    Read from the graph's Links rather than the conf, because the case that
    needs it most is the one the conf cannot answer: an unpinned v1 chain has
    no ``target.object``, and WirePlumber picks its downstream at link time.
    Measured on this hardware, it picks the speaker sink and keeps it — but
    "measured once" is not something a report should assert, so it is read.
    """
    ids, sinks = {}, {}
    for obj in dump or []:
        info = obj.get("info") or {}
        props = info.get("props") or {}
        name = props.get("node.name")
        if not name:
            continue
        ids[obj.get("id")] = name
        if props.get("media.class") == "Audio/Sink":
            sinks[obj.get("id")] = name
    want = f"effect_output.{chain_name}"
    for obj in dump or []:
        if not str(obj.get("type", "")).endswith("Link"):
            continue
        info = obj.get("info") or {}
        if ids.get(info.get("output-node-id")) == want:
            target = sinks.get(info.get("input-node-id"))
            if target:
                return target
    return ""


def _conf_for(chain_name: str, confs) -> str:
    """The conf file a live chain came from, or "" when none of ours matches.

    The remedy is deleting a file, and the file name is the part a reader
    can't derive from a node name. The basename only: check detail is wrapped,
    and a folded absolute path is worse than useless.

    Empty rather than a guessed ``<name>.conf``: ``live_chains`` matches any
    node called ``effect_input.*``, which is what module-filter-chain names
    every chain — the stock PipeWire examples included — while ``confs`` holds
    only files this tool wrote into one directory. So a smart filter someone
    else installed reaches here, and the guess told a reader to delete a file
    that does not exist under a name we invented for it.
    """
    for c in confs:
        if c.node_name == f"effect_input.{chain_name}":
            return c.path.name
    return ""


def check_stacked_chains(chains, confs) -> CheckResult | None:
    """Chains sharing a filter.smart.target run in SERIES, not as alternatives.

    Measured on WirePlumber 0.5.15: get_filter_from_target returns the first
    filter matching a target and get_filter_target "the next filter with
    matching target", so two installed confs put both chains in the path —
    every stage twice, convolver included. The tool no longer creates this
    state, but a machine that reached it before the fix stays broken silently.
    """
    by_target: dict[str, list[str]] = {}
    for c in chains:
        if c.smart and c.target:
            by_target.setdefault(c.target, []).append(c.name)
    stacked = {t: n for t, n in by_target.items() if len(n) > 1}
    if not stacked:
        return None
    names = sorted(n for group in stacked.values() for n in group)
    ours = {n: _conf_for(n, confs) for n in names}
    files = ", ".join(f for f in (ours[n] for n in names) if f)
    foreign = [n for n in names if not ours[n]]
    detail = (
        f"{len(names)} chains ({', '.join(names)}) attach to the same sink, so "
        "PipeWire runs them one after another instead of offering a choice — "
        "every stage is applied that many times over. ")
    if files:
        detail += (f"Keep one of {files} and delete the others, then restart "
                   "PipeWire; the Environment block above gives the full path "
                   "of each conf this tool installed.")
    else:
        # Every one of them is somebody else's. There is no file of ours to
        # name, and telling a reader to delete one would name a file we made
        # up — so the check reports the state and stops there.
        detail += ("None of them is a conf this tool installed, so there is "
                   "nothing of ours to delete; whatever put them on that sink "
                   "has to take one off.")
    if foreign and files:
        detail += (f" The count includes {len(foreign)} other smart filter(s) "
                   f"on the same speakers ({', '.join(foreign)}) that this "
                   "tool didn't install and can't name a file for.")
    return CheckResult(DOCTOR_FAIL, "Stacked filter chains", detail)


def check_unpinned_siblings(chains) -> CheckResult | None:
    """Several virtual sinks with no playback target of their own.

    They follow the *default* sink, so selecting one of them in sound settings
    makes the others follow it and chain into it (measured: Balanced → Warm).
    One on its own is fine — there is nothing for it to chain into.
    """
    loose = [c.name for c in chains if not c.smart and not c.pinned]
    if len(loose) < 2:
        return None
    return CheckResult(
        DOCTOR_WARN, "Unpinned virtual sinks",
        f"{len(loose)} chains ({', '.join(sorted(loose))}) have no playback "
        "target of their own, so they follow whichever sink is default. Pick "
        "one of them as your output and the others feed into it. Re-run the "
        "converter with --target-object <your speaker sink> to pin them.")


def check_confs_loaded(confs, chains, dump) -> CheckResult | None:
    """A conf on disk with no node in the graph.

    module-filter-chain drops the *whole file* when a plugin it names is
    missing, so one absent LSP or Calf package silently costs the entire
    chain. Nothing else reports this outside the seconds after an activation.

    Only confs whose contents were readable can be judged: the node name is
    what there is to look for, and without `spa-json-dump` there isn't one.
    """
    if not confs:
        return None
    if dump is None:
        return CheckResult(DOCTOR_UNKNOWN, "Chains loaded",
                           "pw-dump didn't answer, so whether the confs "
                           "actually loaded couldn't be checked.")
    live = {f"effect_input.{c.name}" for c in chains}
    # A conf whose node name we never read cannot be looked for, and the
    # filter below drops it silently — so with spa-json-dump absent every conf
    # dropped out and this reported PASS, "all present in the graph", about
    # files nothing had read. An answer nothing checked must not be the
    # all-clear.
    readable = [c for c in confs if c.node_name]
    missing = [c for c in readable if c.node_name not in live]
    if not readable:
        # Only point at the conf-contents check when it will be there:
        # it fires for a missing spa-json-dump and nothing else, so a conf
        # that is truncated or unreadable on disk left this sending the
        # reader to a block that was never printed. Those causes have no
        # package behind them, so this line carries them itself.
        causes = sorted({c.unreadable for c in confs if c.unreadable})
        pointer = ("see the conf-contents check above"
                   if NO_SPA_JSON_DUMP in causes
                   else "; ".join(causes) or "no reason was recorded")
        return CheckResult(
            DOCTOR_UNKNOWN, "Chains loaded",
            f"none of the {len(confs)} conf(s) on disk could be read, so the "
            "node each one would create isn't known and whether they loaded "
            f"couldn't be checked — {pointer}.")
    if not missing:
        unread = len(confs) - len(readable)
        return CheckResult(
            DOCTOR_PASS, "Chains loaded",
            f"{len(readable)} conf(s), all present in the graph."
            + (f" {unread} more couldn't be read and weren't checked."
               if unread else ""))
    return CheckResult(
        DOCTOR_FAIL, "Chains loaded",
        f"{len(missing)} of {len(readable)} conf(s) are on disk but absent from "
        f"the graph ({', '.join(str(c.path.name) for c in missing)}). A missing "
        "LSP or Calf LV2 plugin (or impulse file) stops the whole conf loading "
        "— the LV2 plugins and impulse response checks below say whether one "
        "is missing — or PipeWire hasn't been restarted since the file was "
        "written.",
        steps=(("dim", "To try loading it, restart PipeWire "
                       "(re-run --doctor to confirm):"),
               ("cta", f"  {PIPEWIRE_RESTART_CMD}")))


def check_conf_contents(confs) -> CheckResult | None:
    """Confs on disk that nothing here could read, for want of the reader.

    `parse_conf` shells out to spa-json-dump rather than hand-rolling a
    SPA-JSON parser, so without it a conf yields nothing past its version
    header — not which sink it attaches to, not whether it is a smart filter,
    not which impulse files it names. The converter path names the tool and
    its package when validation can't run; this one marked the conf
    `unreadable` and left the cause to the reader's imagination, which is the
    same machine answering two ways.
    """
    blind = [c for c in confs if c.unreadable == NO_SPA_JSON_DUMP]
    if not blind:
        return None
    return CheckResult(
        DOCTOR_UNKNOWN, "Conf contents",
        f"spa-json-dump isn't installed, so {len(blind)} of {len(confs)} "
        "conf(s) on disk couldn't be read past their version header — this "
        "report knows they exist and nothing more. Which sink they attach to "
        "and which impulse files they name are what the checks below rest on. "
        "PipeWire doesn't need the tool to load a conf; reading one back "
        "needs it:",
        steps=packages.install_steps([packages.SPA_TOOLS]))


def check_irs_present(confs) -> CheckResult | None:
    """Whether the impulse responses the confs name are on disk.

    A convolver whose file can't be read fails to instantiate, which fails the
    graph — and the conf carries `nofail`, so PipeWire skips the whole file
    rather than refusing to start. The loss is therefore the entire tuning,
    not just the speaker correction: static analysis of module-filter-chain,
    not something heard on a machine, which is why the sentence stops at what
    PipeWire does with the conf.

    Judged only when a conf names one: a convolver-less conf (or no conf)
    leaves nothing to check, and None over a PASS keeps "all present" from
    being said about an empty set. The healthy case prints — the chains-loaded
    FAIL sends its reader here to rule this cause out, and a check that says
    nothing when satisfied reads as a check that never ran.
    """
    named = list(dict.fromkeys(irs for c in confs for irs in c.irs))
    if not named:
        return None
    missing = {irs: c.path.name for c in confs for irs in c.irs
               if not irs.exists()}
    if not missing:
        return CheckResult(
            DOCTOR_PASS, "Impulse responses",
            f"all {len(named)} impulse file(s) your conf(s) name are on "
            "disk.")
    names = sorted({irs.name for irs in missing})
    shown = ", ".join(names[:2])
    more = f" (+{len(names) - 2} more)" if len(names) > 2 else ""
    return CheckResult(
        DOCTOR_FAIL, "Impulse responses",
        f"{len(missing)} impulse file(s) named by a conf aren't there: "
        f"{shown}{more}. A convolver with no file stops the whole conf "
        "loading, so none of the tuning runs (PipeWire keeps playing "
        "unprocessed). Re-run the converter: it copies the impulse back "
        "beside the conf (or, with --no-copy-irs, points the conf at the "
        "current one).")


def check_targets_exist(chains, sinks, dump) -> CheckResult | None:
    """A smart filter aimed at a sink that no longer exists.

    Nothing errors: WirePlumber simply never inserts the chain, so audio plays
    untreated and the conf looks fine. Sink names change when a card's UCM
    profile changes, or when a conf is copied from another machine.
    """
    if dump is None or not chains:
        return None
    orphans = sorted({c.target for c in chains
                      if c.smart and c.target and c.target not in sinks})
    if not orphans:
        return None
    # Several chains can name several missing sinks — a conf per voicing copied
    # from another machine is the ordinary way to get there — and the singular
    # then reads as one chain with a list of targets.
    plural = len(orphans) > 1
    names = doctor.no_bt_address(", ".join(orphans))
    subject = "chains are" if plural else "the chain is"
    among = "are not" if plural else "is not"
    joins = "they never join" if plural else "it never joins"
    return CheckResult(
        DOCTOR_FAIL, "Target sink missing",
        f"{subject} attached to {names}, which {among} among this machine's "
        f"sinks, so {joins} the audio path. Re-run the converter to pick up "
        "the current speaker sink, or pass --target-sink with the right "
        "node.name.")


def _runnable(name: str) -> bool:
    """Whether a sink name may be printed inside a command.

    A Bluetooth name carries the device's address, and `doctor.no_bt_address`
    takes it out of everything this report prints — but a redacted name is not
    something a shell can run, and printing the real one in a step would put
    the address back into the same pasted block the redaction just cleaned.
    So a check that would name such a sink in a command drops the command and
    keeps its prose route instead (issue #63 follow-up).
    """
    return name == doctor.no_bt_address(name)


def check_default_sink(chains, confs, defaults, sinks, dump) -> CheckResult | None:
    """Which output is selected, and whether that is the one the reader wants.

    Three states, most-live first, and all three are about the same question, so
    they share one label:

    (a) A smart filter selected as the output (issue #63). It works — measured
        on this hardware, the graph is identical either way and nothing is
        processed twice — but the chain's sink and the speaker are then two
        sinks in series, each with its own volume, and `pactl list sink-inputs`
        shows the stream on one and the chain's own output on the other. The
        speaker's level still applies underneath, so a speaker left at 40 %
        takes -23.8 dB off a chain that reads 100 %.
    (b) A virtual-sink chain that loaded but is selected nowhere: it only
        processes what is played to it, so it is doing nothing at all.
    (c) A remembered pick naming a chain that is gone. Nothing is wrong at that
        moment, which is why nothing else reports it — but WirePlumber restores
        the pick the moment a sink of that name exists again, so the next chain
        installed under the same name silently becomes the selected output.

    The remembered pick is the part nothing else can see: it lives in
    ~/.local/state/wireplumber/default-nodes, no sound-settings UI shows it, and
    default-nodes/find-selected-default-node.lua scores it 30000 + priority, so
    it outranks every priority.session actually in play — 0 for a filter chain,
    1000 for an ALSA speaker, 1010 for Bluetooth, the 600s for HDMI.
    """
    if dump is None:
        return None
    chain_sinks = {f"effect_input.{c.name}": c for c in chains}
    picked = chain_sinks.get(defaults.effective)
    volumes = sink_volumes(dump)

    # The downstream level is the half of this the reader cannot see: their
    # slider is on the chain, and whatever the sink underneath is set to comes
    # off on top of it. Naming the figure is the whole point — "leave it at
    # 100 %" is advice, "it is at 40 %, which is -23.8 dB" is a diagnosis.
    def _hidden_attenuation(chain) -> str:
        below = chain.target or chain.pinned or downstream_sink(dump, chain.name)
        level = volumes.get(below)
        if not below or level is None or level > 0.99:
            return ""
        return (f" The sink it feeds is itself at {_volume_reading(level)}, and "
                "that comes off everything on top of this — raise it there, or "
                "you are turning one control down against the other.")

    if picked is not None and picked.smart:
        # The target sink is named by the step below, unwrapped, and by the
        # environment block's `smart→` line. Naming it here too put a
        # 70-character node name inside wrapped prose, which folds it mid-name.
        # ...and only claim it is named below when a step will actually name
        # it: a smart chain whose filter.smart.target didn't parse reaches this
        # same detail with no steps, and a pointer to nothing is worse than
        # none. Same for a target whose name carries an address — that step is
        # replaced by prose that names nothing, so the pointer must go with it.
        names_it = (" (named in the fix below)"
                    if picked.target and _runnable(picked.target) else "")
        detail = (
            f"{doctor.no_bt_address(defaults.effective)} is your selected output, but it is a filter "
            f"rather than a device — it attaches itself to your speaker "
            f"sink{names_it}, so everything playing there goes through "
            # "can be", not "is why": the two-stage arrangement is what the
            # check detected, but it is not the only thing that makes a machine
            # quiet, and a diagnosis this line cannot make would send a reader
            # who fixes it and hears no change away with nothing left to try.
            "it either way. Selecting it as well leaves you two volume sliders "
            "for the same sound and both apply, so sound can be quieter than "
            "the slider you are moving suggests.")
        if defaults.configured == defaults.effective:
            # Only when the user picked it: WirePlumber restores the *current*
            # configured sink, not every sink ever chosen, so this sentence is
            # false for a chain that merely won the automatic pick.
            detail += (" WirePlumber restores the output you picked last, so "
                       "this comes back after every restart until you pick "
                       "your speakers once.")
        detail += _hidden_attenuation(picked)
        steps = ()
        if picked.target and _runnable(picked.target):
            steps = (("dim", "Pick your speakers as the output — in sound "
                             "settings, or:"),
                     ("cta", f"  pactl set-default-sink {picked.target}"))
        elif picked.target:
            steps = (("dim", "Pick your speakers as the output in sound "
                             "settings."),)
        return CheckResult(DOCTOR_WARN, "Default output", detail, steps)

    # A v1 chain that IS selected is the mode working as intended, so this is
    # silent — unless the sink underneath is turned down, which is the one thing
    # that arrangement hides. The reader's slider is on the chain; the level
    # below it is invisible from there, survives reboots, and is subtracted from
    # everything. Smart-filter routing has no equivalent exposure: there the
    # single slider a reader sees is the one that applies.
    if picked is not None and not picked.smart:
        hidden = _hidden_attenuation(picked)
        if hidden:
            below = (picked.target or picked.pinned
                     or downstream_sink(dump, picked.name))
            steps = (("dim", "Put that one back to full and use this chain's "
                             "own control for volume:"),
                     ("cta", f"  pactl set-sink-volume {below} 100%")
                     ) if _runnable(below) else (
                ("dim", "Put that one back to full in sound settings, and use "
                        "this chain's own control for volume."),)
            return CheckResult(
                DOCTOR_WARN, "Default output",
                f"{doctor.no_bt_address(defaults.effective)} is your selected output, which is how "
                "this mode is meant to run — but it feeds another sink, and "
                "both levels apply." + hidden, steps)
        return None

    # Gate on *none* of them being selected rather than on each chain: with
    # --target-sink '' the tool installs several by design and only one can be
    # the output, so a per-chain warning would fire on the mode it fires most on.
    virtual = sorted(c.name for c in chains if not c.smart and not c.pinned)
    if virtual and picked is None:
        names = ", ".join(f"effect_input.{n}" for n in virtual)
        plural = len(virtual) > 1
        where = (f"your selected output is {doctor.no_bt_address(defaults.effective)}"
                 if defaults.effective else "nothing here is selected as the output")
        detail = (
            f"{names} {'are plain virtual sinks' if plural else 'is a plain virtual sink'}, "
            f"so {'each' if plural else 'it'} only processes what is played to "
            f"{'them' if plural else 'it'} — and {where}, so nothing is going "
            f"through {'any of them' if plural else 'it'} unless an app was "
            f"pointed at {'one' if plural else 'it'} directly. Pick "
            f"{'one' if plural else 'it'} in your sound settings.")
        if not plural:
            # Only for a single chain: the converter refuses smart-filter
            # routing for multi-chain installs, so suggesting it to someone who
            # has several would be advice the tool turns down.
            # Conditional on how the conf got here, because this branch cannot
            # tell: an identical v1 conf is written both by an explicit
            # --target-sink '' and by autodetection finding no speaker sink,
            # and in the second case re-running without the flag just repeats
            # the fallback. Neither InstalledConf nor LiveChain records which.
            #
            # "the one you use", not "one volume control": the chain sink keeps
            # a volume control in smart-filter mode too, and it still
            # attenuates — measured, gain 7.9 with the speaker selected. What
            # changes is that nothing puts you on that slider, which is why
            # check_chain_volume exists to notice when it is left down.
            detail += (" If you installed it with --target-sink '', re-running "
                       "without that flag attaches it to your speakers instead "
                       "— nothing to select, and the speaker's is then the only "
                       "control you touch.")
        steps = (("dim", "Or from a terminal:"),) + tuple(
            ("cta", f"  pactl set-default-sink effect_input.{n}") for n in virtual)
        return CheckResult(DOCTOR_WARN, "Default output", detail, steps)

    if (defaults.configured.startswith("effect_input.")
            and defaults.configured not in sinks
            # A conf of that name on disk means the chain failed to load, which
            # check_confs_loaded already FAILs on. Two remedies for one file
            # reads as two problems.
            and not any(c.node_name == defaults.configured for c in confs)):
        # The reassurance is conditional, and so is its punctuation: "nothing is
        # wrong right now" holds only while audio has somewhere to go. With no
        # effective default there is no sink it is landing on, which is not a
        # state to reassure anyone about.
        #
        # The effective sink is also named by the step below and marked in the
        # environment block, so it is not repeated here — a 70-character node
        # name inside wrapped prose folds across two lines mid-name.
        reassurance = (" Nothing is wrong right now — audio follows the sink "
                       "marked ← default above — but" if defaults.effective
                       else " But")
        detail = (
            f"your remembered output is {doctor.no_bt_address(defaults.configured)}, which isn't in "
            "the graph — a filter chain that was deleted or renamed."
            + reassurance
            + " WirePlumber restores that choice as soon as a sink of that "
            "name exists again, so the next chain installed under the same name "
            "becomes your selected output, with its volume control in front of "
            "your speakers'.")
        steps = ()
        if defaults.effective:
            # "replaces it", not "clears it": set-default-sink moves the new
            # name to the head of WirePlumber's stack and pushes the stale one
            # down. It is never erased — it just stops being the one restored.
            detail += " Selecting the output you actually use, once, replaces it."
            steps = ((("dim", "Make the one you use the remembered choice:"),
                      ("cta", f"  pactl set-default-sink {defaults.effective}"))
                     if _runnable(defaults.effective) else
                     (("dim", "Make the one you use the remembered choice by "
                              "picking it in sound settings."),))
        return CheckResult(DOCTOR_WARN, "Default output", detail, steps)

    return None


def check_chain_volume(chains, dump) -> CheckResult | None:
    """A chain sink left turned down — the attenuation nothing points at.

    A filter-chain sink carries its own volume, and it applies whether or not
    the chain is the selected output: measured with the speaker selected and
    the chain at 0.125, the output came back 7.9x down. In smart-filter mode
    nothing ever puts a reader on that slider — their sound settings move the
    speaker's — so a chain turned down once, by someone who selected it and
    then switched back, stays down through reboots with no visible cause.

    Ahead of the graph, too, so it is not only quieter: the tuning's compressor
    and limiter see the attenuated signal (docs/design-notes.md, issue #63).
    """
    if dump is None or not chains:
        return None
    volumes = sink_volumes(dump)
    turned_down = sorted(
        (f"effect_input.{c.name}", volumes[f"effect_input.{c.name}"])
        for c in chains
        if volumes.get(f"effect_input.{c.name}", 1.0) <= 0.99)
    if not turned_down:
        return None
    name, level = turned_down[0]
    more = (f" ({len(turned_down) - 1} more like it.)"
            if len(turned_down) > 1 else "")
    return CheckResult(
        DOCTOR_WARN, "Chain volume",
        f"{name} is itself at {_volume_reading(level)}. That is the chain's own "
        "volume, and it comes off everything the chain processes — including "
        "when your speakers are the selected output and their slider is the one "
        f"you are moving.{more} Nothing in sound settings will show you this "
        "one; put it back to full unless you meant it.",
        (("dim", "Put it back to full:"),
         ("cta", f"  pactl set-sink-volume {name} 100%")))


def check_conf_directory() -> CheckResult | None:
    """Confs in filter-chain.conf.d/, which the running daemon never reads."""
    try:
        strays = [p for p in sorted(_UNSCANNED_CONF_DIR.glob("*.conf"))
                  if p.read_text(errors="replace").startswith(CONF_HEADER_MARK)]
    except OSError:
        return None
    if not strays:
        return None
    return CheckResult(
        DOCTOR_WARN, "Conf in an unread directory",
        f"{len(strays)} conf(s) are in {doctor.tilde(_UNSCANNED_CONF_DIR)}, which "
        "the PipeWire daemon itself doesn't read (only the optional "
        "filter-chain.service does). This tool installs into "
        f"{doctor.tilde(DEFAULT_OUTPUT_DIR)} — move them there and restart "
        "PipeWire, or leave them if you run filter-chain.service on purpose.")


def check_wireplumber(version: session.Version) -> CheckResult:
    """Smart-filter routing needs WirePlumber 0.5+."""
    if not version.ok:
        # The probe's own reason, not a fixed sentence: "wireplumber not
        # found" and "didn't answer" are different remedies.
        return CheckResult(DOCTOR_UNKNOWN, "WirePlumber",
                           f"{version.reason}, so its version wasn't checked.")
    if version.parts < (0, 5):
        return CheckResult(
            DOCTOR_FAIL, "WirePlumber",
            f"{version.text} has no smart-filter support, so a chain written "
            "the default way never attaches to your speakers. Upgrade, or "
            "re-run the converter with --target-sink '' for a plain virtual "
            "sink you select as your output.")
    return CheckResult(DOCTOR_PASS, "WirePlumber",
                       f"{version.text} — smart-filter routing supported.")


def check_easyeffects_conflict(sinks, chains, dump) -> CheckResult | None:
    """EasyEffects processing the same audio as the chain.

    Both apply their own convolver, compressor and limiter, so the result is
    neither tuning. This is a conflict check, not a report on an EasyEffects
    install — on this path EasyEffects is only ever an intermediate format.

    Silent when no chain of ours is live: EasyEffects on its own is not a
    conflict, and saying "processed twice" when there is nothing to process it
    a second time would be plainly false.
    """
    if dump is None or "easyeffects_sink" not in sinks or not chains:
        return None
    return CheckResult(
        DOCTOR_WARN, "EasyEffects also running",
        # "can be", as the install-time twin says: the gate is a live chain of
        # ours plus an EasyEffects sink, which a v1 chain nobody has selected
        # satisfies without anything reaching it.
        "EasyEffects is running, and anything routed through it can be "
        "processed twice — once by its own chain, once by this one. Quit it "
        "and stop it starting again (its Background Service and autostart, or "
        "remove its autoload).")


def check_conf_versions(confs, running: str) -> CheckResult | None:
    """A conf written by a different build than the one being run."""
    stale = sorted({c.version for c in confs if c.version and c.version != running})
    if not stale:
        return None
    return CheckResult(
        DOCTOR_WARN, "Conf from another version",
        f"the installed conf(s) were written by {', '.join(stale)} and this "
        f"is {running}. If a fix since then was meant to reach your audio, "
        "re-run the converter — a conf is a snapshot, it doesn't update "
        "itself.")


# Every URI the converter can emit, with the label the report gives it.
# Autogain and stereo tools were the two omissions: autogain is on by default
# on SoundWire devices, so the doctor could report a full house while the one
# plugin that run needs is the missing one.
_PLUGIN_URIS = (
    ("LSP PEQ", LSP_PEQ_URI),
    ("LSP MBC", LSP_MBC_URI),
    ("LSP limiter", LSP_LIM_URI),
    ("LSP autogain", LSP_AUTOGAIN_URI),
    ("Calf bass enhancer", CALF_BE_URI),
    ("Calf stereo tools", CALF_ST_URI),
    ("LSP filter (virtual-bass)", vbe.LSP_FILTER_URI),
    ("Calf saturator (virtual-bass)", vbe.CALF_SATURATOR_URI),
)


@dataclass
class PluginProbe:
    """What ``lv2info`` was able to say about those URIs."""
    has_lv2info: bool = True   # False when the tool itself isn't installed,
                               # and then `entries` is empty — an unasked
                               # question, which nothing may read as an answer
    entries: tuple[tuple[str, str, bool], ...] = ()   # (label, uri, present)


def _probe_plugins() -> PluginProbe:
    """Ask lv2info about each URI in turn.

    The probe half of the plugin question, kept apart from both its readers
    for the reason the module docstring gives — and, here, because it is eight
    subprocesses: the Environment block and `check_plugins_present` are two
    readers of one answer, not two spawns.
    """
    if shutil.which("lv2info") is None:
        return PluginProbe(has_lv2info=False)
    entries = []
    for label, uri in _PLUGIN_URIS:
        try:
            rc = subprocess.run(["lv2info", uri], capture_output=True,
                                text=True, timeout=10).returncode
        except (subprocess.SubprocessError, OSError):
            # An lv2info that cannot run at all is not a plugin that is there.
            rc = 1
        entries.append((label, uri, rc == 0))
    return PluginProbe(entries=tuple(entries))


def _plugin_presence(probe: PluginProbe | None = None) -> list[str]:
    """Which LV2 packages the chain needs are installed, as Environment facts.

    Inventory, not diagnosis: this block is the listing a reader
    cross-references, and the fix for a missing one lives in the check block
    with the command that applies it (`.claude/rules/user-messages.md`).
    Probes for itself only when handed nothing, so the report — which probes
    once, in `gather_pw_doctor` — pays for one.
    """
    if probe is None:
        probe = _probe_plugins()
    if not probe.has_lv2info:
        return ["lv2info not installed — LV2 plugin presence unknown"]
    return [f"{label}: {'present' if present else 'MISSING'}"
            for label, _uri, present in probe.entries]


# Which vendor a plugin URI belongs to, and the `lib.packages` key naming the
# package that carries its LV2 build. Keyed on the namespace rather than on
# each URI: the eight above are two namespaces — the virtual-bass pair
# included — so a plugin either vendor adds later needs no second edit here.
# `ee_to_pipewire.py`'s `_PLUGIN_VENDORS` is the same table for the
# converter's own hint, restated rather than shared because lib/ does not
# import a root entry point.
_PLUGIN_VENDORS = (
    ("http://lsp-plug.in/", "LSP", packages.LSP_LV2),
    ("http://calf.sourceforge.net/", "Calf", packages.CALF_LV2),
)


def check_plugins_present(probe, confs=()) -> CheckResult | None:
    """Whether the LV2 plugins *this machine's confs* name are installed.

    module-filter-chain drops the *whole file* when one plugin in it won't
    load, so an absent package is the commonest reason a conf on disk does
    nothing at all — and the Environment block lists eight answers without
    saying what to do about any of them.

    Judged against the confs, not against the eight URIs the converter is
    *able* to emit. Which ones a chain uses depends on the profile and the
    flags, and scoring the whole catalogue made a fault out of a plugin
    nothing on the machine asks for: an LSP-only chain that loads and plays
    perfectly FAILed for want of Calf — and openSUSE has no Calf package to
    offer, so its readers, following this project's own instructions, would
    have met a permanent FAIL with no command under it.

    Vendors, not URIs: what a reader installs is a package, and a chain
    missing LSP is missing every LSP plugin in it. Eight URIs is a list nobody
    can act on.

    With no readable conf there is nothing to judge, and this returns None
    rather than guess — before the missing-lv2info branch, not after it: on a
    machine with nothing installed, an UNKNOWN about the plugins "a conf
    names" is a question asked of no conf, and it offered a package to a
    reader whose report already says the directory is empty. The presence of
    all eight still reaches a pasted report through the Environment block,
    which is inventory and says only what it found.
    """
    wanted = {uri for c in confs for uri in c.plugins}
    if not wanted:
        # No conf, or none readable. `check_conf_contents` and the
        # installed-confs check each say so in their own words; a verdict here
        # would be about plugins nothing has asked for yet.
        return None
    if not probe.has_lv2info:
        return CheckResult(
            DOCTOR_UNKNOWN, "LV2 plugins",
            "lv2info isn't installed, so whether the LSP and Calf plugins a "
            "conf names are there couldn't be checked — and a missing plugin "
            "(or impulse file) stops the whole conf loading. PipeWire doesn't "
            "need lv2info, it loads plugins through the lilv library; this "
            "check needs it:",
            steps=packages.install_steps([packages.LV2INFO]))
    if not probe.entries:
        # Nothing was asked, so there is nothing to report. A PASS here would
        # say "all present" about an empty set — the shape a stubbed probe
        # takes, and the one a report must not turn into an all-clear.
        return None
    entries = [e for e in probe.entries if e[1] in wanted]
    if not entries:
        # A conf built entirely from module-filter-chain builtins — a
        # convolver-only preset has no LV2 node at all.
        return None
    missing = [(label, uri) for label, uri, present in entries if not present]
    if not missing:
        return CheckResult(
            DOCTOR_PASS, "LV2 plugins",
            f"all {len(entries)} LSP and Calf plugins your conf(s) name are "
            "installed.")
    vendors = [(name, key) for prefix, name, key in _PLUGIN_VENDORS
               if any(uri.startswith(prefix) for _label, uri in missing)]
    if not vendors:
        # A URI from a namespace the table above doesn't know. Nothing emits
        # one today; the fallback names the plugins themselves rather than
        # printing a FAIL with no fix under it, so adding a third vendor
        # degrades to a worse message instead of a silent gap.
        return CheckResult(
            DOCTOR_FAIL, "LV2 plugins",
            f"{len(missing)} plugin(s) your conf(s) name aren't installed: "
            f"{', '.join(label for label, _uri in missing)}. PipeWire drops "
            "the whole file when one plugin in it won't load, so the chain "
            "never appears in the graph and audio plays untreated.")
    vendor_names = " and ".join(name for name, _key in vendors)
    return CheckResult(
        DOCTOR_FAIL, "LV2 plugins",
        f"{len(missing)} of the {len(entries)} LV2 plugins your conf(s) name "
        f"aren't installed, all from {vendor_names}. PipeWire drops the "
        "whole file when one plugin in it won't load, so the chain never "
        "appears in the graph and audio plays untreated. Install them:",
        steps=packages.install_steps([key for _name, key in vendors]))


def gather_pw_doctor() -> tuple[list, list[InstalledConf], list[LiveChain], dict]:
    """Probe everything once, then judge. Returns (checks, confs, chains, facts)."""
    dump = _pw_dump()
    chains = live_chains(dump)
    sinks = sink_names(dump)
    defaults = default_sinks(dump)
    # Only the directory the daemon reads. A conf in _UNSCANNED_CONF_DIR is
    # not installed in any meaningful sense — counting it inflated "Confs: N"
    # and, because it shares a node name with the real one, let "Chains
    # loaded" pass for a file that had loaded for nobody.
    confs = installed_confs(DEFAULT_OUTPUT_DIR.expanduser())
    running = version.get_version()
    wireplumber = session.wireplumber_version()
    pipewire = session.pipewire_version()
    # Probed here for the reason the WirePlumber version is: the Environment
    # block renders these facts and the check below judges them, and eight
    # lv2info spawns is not a thing to pay for twice.
    plugin_probe = _probe_plugins()

    checks = [c for c in (
        # First: everything under it that reads a conf's *contents* is blind
        # without it — no node name to look for in the graph, no target sink,
        # no impulse file.
        check_conf_contents(confs),
        check_stacked_chains(chains, confs),
        check_unpinned_siblings(chains),
        check_confs_loaded(confs, chains, dump),
        # Directly under the check whose detail points at it: a conf that
        # never loaded is usually a conf naming a plugin that isn't there.
        check_plugins_present(plugin_probe, confs),
        check_irs_present(confs),
        check_targets_exist(chains, sinks, dump),
        # After the target check, so the block reads in the order a reader
        # debugs in: does the chain exist → did it load → are its files there →
        # is its target there → is it, or should it be, the selected output.
        check_default_sink(chains, confs, defaults, sinks, dump),
        # After it: which output is selected is the question a reader arrives
        # with, and a turned-down chain is the one that survives getting that
        # answer right.
        check_chain_volume(chains, dump),
        check_conf_directory(),
        check_wireplumber(wireplumber),
        check_easyeffects_conflict(sinks, chains, dump),
        check_conf_versions(confs, running),
    ) if c is not None]

    if not confs:
        # Both routes, because either script's --doctor lands here: naming
        # only the wrapper told an ee_to_pipewire.py reader to run something
        # else. And the directory is the whole of what was looked at — a conf
        # someone put elsewhere with --output is not absent, just unread.
        checks.insert(0, CheckResult(
            DOCTOR_WARN, "Installed confs",
            f"no filter-chain conf from this tool in "
            f"{doctor.tilde(DEFAULT_OUTPUT_DIR)} — run dolby_to_pipewire.py on your "
            "tuning XML (or ee_to_pipewire.py on a preset) first; confs "
            "written elsewhere with --output aren't checked."))
    if dump is None:
        checks.append(CheckResult(
            DOCTOR_UNKNOWN, "PipeWire",
            # Not "the checks above": a check that needed the graph returned
            # None and is absent from the block, so there is nothing above for
            # the reader to go back and re-read.
            "pw-dump didn't answer — is the PipeWire daemon running? Several "
            "checks need the live graph and were skipped."))

    facts = {
        "confs": confs,
        "chains": chains,
        "sinks": sorted(sinks),
        "default": defaults,
        # The default sink's description, by the rule the EasyEffects doctor
        # labels its sink with — off this dump, so the row and the checks
        # never describe two different graphs.
        "default_label": hw_sinks.sink_label(hw_sinks.sinks_from_dump(dump),
                                             defaults.effective),
        "wireplumber_version": wireplumber,
        "pipewire_version": pipewire,
        "version": running,
        "plugins": plugin_probe,
    }
    return checks, confs, chains, facts


def _environment_lines(confs, chains, facts) -> list[str]:
    """The `=== PipeWire filter-chain setup ===` body: what this tool has
    installed and what PipeWire is doing with it. Labels pad to
    `doctor_layout.GUTTER` — the same column as every block either doctor
    prints — and the per-conf and per-sink lines hang under it. No `Tool:`
    row: the report's first line already carries the version."""
    hang = " " * layout.GUTTER
    lines = [
        layout.row("Confs",
                   f"{len(confs)} in {doctor.tilde(DEFAULT_OUTPUT_DIR)}",
                   layout.GUTTER),
    ]
    for c in confs:
        if not c.readable:
            # Why, not just that. A bare "unreadable" reads as a damaged file
            # and sends the reader looking for one, where the usual answer is
            # a missing spa-json-dump and a package away.
            state = f"unreadable ({c.unreadable})" if c.unreadable else "unreadable"
        elif c.smart:
            state = f"smart→{c.target}"
        elif c.pinned:
            state = f"pinned→{c.pinned}"
        else:
            state = "virtual sink, unpinned"
        # Hanging lines carry paths, which are never wrapped — split across
        # lines they stop being greppable — so a long one overflows instead.
        lines.append(f"{hang}{doctor.tilde(c.path)} "
                     f"[{c.version or '?'}] {state}")
    lines += layout.wrapped_row(
        "Live chains",
        f"{len(chains)}" + (": " + ", ".join(sorted(c.name for c in chains))
                            if chains else ""), layout.GUTTER)
    # `.get` because tests stub `facts`, and because a report whose whole job is
    # to print what it knows must not die on a key it doesn't have.
    default = facts.get("default") or DefaultSink()
    lines.append(layout.row("Sinks", str(len(facts["sinks"])), layout.GUTTER))
    lines += [f"{hang}{s}"
              + ("   ← default" if s == default.effective else "")
              for s in facts["sinks"]]
    # The remembered pick is the report's one forward-looking routing fact:
    # WirePlumber re-applies it the moment a node of that name exists again,
    # so a dead name here is a same-name chain reinstall grabbing the output,
    # or an absent device the default will snap back to. Only worth a line
    # when the name is missing from the graph — a remembered pick that is
    # present but not effective predicts nothing anyone can act on.
    if default.configured and default.configured not in facts["sinks"]:
        lines.append(layout.row(
            "Remembered", f"{default.configured} (not in the graph)",
            layout.GUTTER))
    # `.get` for the same reason `default` uses it: the run's own probe comes
    # through `facts`, and a stubbed facts dict must render, not raise.
    # A labelled hanging list like Confs and Sinks: unlabelled, the eight rows
    # read as what a conf needs, and the LV2 check's "3 your conf(s) name"
    # below then looks like a count that lost five.
    plugin_rows = _plugin_presence(facts.get("plugins"))
    if plugin_rows:
        lines += layout.wrapped_row(
            "Plugins",
            "every LV2 plugin this tool can use; a conf may need fewer",
            layout.GUTTER)
        lines += [f"{hang}{row}" for row in plugin_rows]
    # Once, over the whole block, rather than at each of the four sites that
    # interpolate a node name: this is the densest listing the tool prints, the
    # `Sinks:` lines are every sink in the graph, and a line added later would
    # otherwise have to remember. Nothing here is a command, so nothing here
    # needs to stay runnable.
    return [doctor.no_bt_address(line) for line in lines]


def _probe_pipewire(chains, default: DefaultSink
                    ) -> tuple[session.ClockSettings, session.Dropouts, float | None]:
    """The `=== PipeWire ===` facts: the clock, the dropout counters on the
    default sink and the live chains' own nodes, and PipeWire's uptime. One
    function so tests can stand in for the five-second pw-top window."""
    names = [f"effect_{half}.{c.name}" for c in chains
             for half in ("input", "output")]
    return (session.read_settings(),
            session.read_xruns(sink=default.effective, chain_prefixes=(),
                             chain_names=names),
            session.process_age("pipewire"))


def _pipewire_lines(default: DefaultSink, settings: session.ClockSettings,
                    dropouts: session.Dropouts, pw_age: float | None,
                    label: str = "",
                    pipewire: session.Version | None = None,
                    wireplumber: session.Version | None = None) -> list[str]:
    """The `=== PipeWire ===` body for this path: the versions, the default
    sink, the clock, the dropouts — rendered by the frame both doctors
    share, worded for a filter chain that lives inside PipeWire (no app
    uptime to bound it)."""
    lines = []
    if pipewire is not None and wireplumber is not None:
        lines += layout.version_rows(pipewire, wireplumber, layout.GUTTER)
    if default.effective:
        lines += layout.output_sink_rows(
            label, doctor.no_bt_address(default.effective), "", layout.GUTTER)
    lines += layout.clock_rows(settings, dropouts, layout.GUTTER)
    lines += layout.dropouts_rows(dropouts, pw_age, None, layout.GUTTER,
                                  app="the filter chain",
                                  nodes="the chain's nodes",
                                  busiest="the busiest chain node",
                                  into="into the chain")
    return lines


def report_pw_doctor() -> int:
    """Print the PipeWire-side diagnostic report. Returns a process exit code.

    Every line lands on stdout, console-styled or not, so
    `--doctor > report.txt` captures the whole report — which is the point of
    a block written to be redirected to a file or pasted into an issue.
    """
    checks, confs, chains, facts = gather_pw_doctor()

    layout.print_report_header(facts["version"])
    # Probed here rather than in gather_pw_doctor: nothing judges this block,
    # so it costs a ~2.5 s probe only on the path that prints it.
    gen._print_speaker_info(gen._gather_speaker_info())
    # Same reason: nothing judges these rows, and the dropout window is five
    # seconds nobody should pay on the gather path.
    default = facts.get("default") or DefaultSink()
    layout.print_environment(
        _pipewire_lines(default, *_probe_pipewire(chains, default),
                        label=facts.get("default_label", ""),
                        pipewire=facts.get("pipewire_version"),
                        wireplumber=facts.get("wireplumber_version")),
        "=== PipeWire ===")
    layout.print_environment(_environment_lines(confs, chains, facts),
                             "=== PipeWire filter-chain setup ===")
    layout.print_check_block("=== PipeWire filter-chain doctor ===", checks)
    layout.print_closing((
        ("dim", "To remove a chain: delete its .conf (and matching .irs), "
                "then restart PipeWire:"),
        ("cta", f"  {PIPEWIRE_RESTART_CMD}"),
    ))
    return 0


def warn_if_stacked(output_path: Path, target_sink: str | None) -> None:
    """Warn when the conf just written joins another aimed at the same sink.

    Trying a second voicing or profile is the obvious next thing to do — the
    wrapper's own output suggests it — and nothing stopped the two coexisting:
    --force guards one output path, so a differently-named conf lands beside
    the first with no collision. WirePlumber then runs both in series rather
    than offering a choice, and neither the run nor the audio says so. Caught
    here, at the moment it happens, rather than left for --doctor to find
    after someone notices the sound is wrong.
    """
    if not target_sink:
        return   # not a smart filter; nothing chains
    others = [c.path.name for c in installed_confs(output_path.parent)
              if c.smart and c.target == target_sink
              and c.path.resolve() != output_path.resolve()]
    if not others:
        return
    console.cprint("warn", "")
    console.cprint("warn", "⚠  Another filter chain is already attached to the same "
                   "speakers:")
    for name in others:
        console.cprint("warn", f"     {name}")
    # "that many times over", not "twice": `others` is every other conf on the
    # same target, so a third one makes it three — the count the doctor's own
    # stacked-chains check already words this way.
    console.cprint("dim", "   PipeWire runs them one after another, not as "
                  "alternatives — every")
    console.cprint("dim", "   stage applies that many times over. If you were "
                  "trying a different")
    console.cprint("dim", "   voicing or profile, delete the one you don't want "
                  "(and its .irs), then")
    console.cprint("dim", "   restart PipeWire.")
    console.cprint("dim", "   --doctor lists what is installed and what it does.")


def warn_if_easyeffects_running(running: bool | None = None) -> None:
    """Warn when EasyEffects is up as a chain conf lands.

    The likeliest reader is an EasyEffects user crossing over for a
    PipeWire-only feature (``--enable virtual-bass`` says so in as many
    words) — and for them EasyEffects is still running when they get here.
    In smart-filter mode its output plays into the very sink this chain
    attaches to, so everything gets the EE preset *and* the chain, in
    series; nothing in the run or the audio says so. Same shape as
    ``warn_if_stacked`` above: caught at the moment it happens instead of
    left for someone to notice the sound is wrong.
    """
    if running is None:
        running = ee_socket.easyeffects_running()
    if not running:
        return
    console.cprint("warn", "")
    console.cprint("warn", "⚠  EasyEffects is running. This chain replaces an "
                   "EasyEffects preset —")
    console.cprint("warn", "   running both can process your audio twice.")
    console.cprint("dim", "   Quit EasyEffects while you use the chain. To switch "
                  "back later: delete")
    console.cprint("dim", "   the conf (and its .irs), restart PipeWire, start "
                  "EasyEffects again.")
