"""``--doctor`` for the PipeWire path: what is installed, and what it is doing.

The EasyEffects doctor (``lib/report/environment.py``) checks the environment a
preset lands in; this is the same idea one layer down, where there is more to
get wrong and less to see. Probing and judging are kept apart — ``_pw_dump``,
``_wireplumber_version``, ``parse_conf`` and ``installed_confs`` read the
system, and every ``check_*`` below is a pure function over what they returned,
so states this machine cannot produce are still unit-testable
(``tests/test_pw_doctor.py``).

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
tool. The constants and the plugin URIs are imported under bare names because
that is how these lines read in ``ee_to_pipewire.py`` and a move may not
re-point what it carries; they are string constants and a record type, so
there is nothing here a test would patch. The functions stay module-qualified.

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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from lib import console, doctor, version
from lib.doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from lib.pipewire.conf import CONF_HEADER_MARK, PIPEWIRE_RESTART_CMD
from lib.pipewire.plugins import (
    CALF_BE_URI,
    LSP_LIM_URI,
    LSP_MBC_URI,
    LSP_PEQ_URI,
)
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

# Both directories a conf can end up in. Only the first is loaded: the stock
# pipewire.conf auto-includes pipewire.conf.d/, while filter-chain.conf.d/ is
# for the standalone `pipewire -c filter-chain.conf` invocation and is never
# scanned by the running daemon (see DEFAULT_OUTPUT_DIR).
_UNSCANNED_CONF_DIR = Path.home() / ".config/pipewire/filter-chain.conf.d"

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
    readable: bool = True    # False when spa-json-dump couldn't parse it


@dataclass
class LiveChain:
    """A filter chain as the running graph reports it."""
    name: str                # the X in effect_input.X
    smart: bool = False
    target: str = ""
    pinned: str = ""


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


def _wireplumber_version() -> tuple[int, ...] | None:
    """(major, minor) of the running WirePlumber, or None if it won't say."""
    try:
        out = subprocess.run(["wireplumber", "--version"], capture_output=True,
                             text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"(\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


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
        return conf
    m = re.search(r"^# version:\s*(\S+)", head, re.MULTILINE)
    conf.version = m.group(1) if m else ""
    if shutil.which("spa-json-dump") is None:
        conf.readable = False
        return conf
    try:
        dumped = subprocess.run(["spa-json-dump", str(path)],
                                capture_output=True, text=True, timeout=10)
        data = json.loads(dumped.stdout)
        args = data["context.modules"][0]["args"]
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError,
            KeyError, IndexError, TypeError):
        conf.readable = False
        return conf
    capture = args.get("capture.props", {})
    playback = args.get("playback.props", {})
    conf.node_name = capture.get("node.name", "")
    conf.smart = bool(capture.get("filter.smart"))
    conf.target = (capture.get("filter.smart.target") or {}).get("node.name", "")
    conf.pinned = playback.get("target.object", "")
    # Deduplicated: the stereo convolver is two nodes reading the same file,
    # so a missing IRS would otherwise be reported once per channel.
    conf.irs = list(dict.fromkeys(
        Path(n["config"]["filename"])
        for n in args.get("filter.graph", {}).get("nodes", [])
        if isinstance(n, dict) and "filename" in n.get("config", {})))
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


def _conf_for(chain_name: str, confs) -> str:
    """The conf file a live chain came from — the remedy is deleting one, and
    the file name is the part a reader can't derive from a node name. The
    basename only: check detail is wrapped, and a folded absolute path is
    worse than useless."""
    for c in confs:
        if c.node_name == f"effect_input.{chain_name}":
            return c.path.name
    return f"{chain_name}.conf"


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
    files = ", ".join(_conf_for(n, confs) for n in names)
    return CheckResult(
        DOCTOR_FAIL, "Stacked filter chains",
        f"{len(names)} chains ({', '.join(names)}) attach to the same sink, so "
        "PipeWire runs them one after another instead of offering a choice — "
        f"every stage is applied that many times over. Keep one of {files} and "
        "delete the others, then restart PipeWire; the full paths are in the "
        "block below.")


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
    """
    if not confs:
        return None
    if dump is None:
        return CheckResult(DOCTOR_UNKNOWN, "Chains loaded",
                           "pw-dump didn't answer, so whether the confs "
                           "actually loaded couldn't be checked.")
    live = {f"effect_input.{c.name}" for c in chains}
    missing = [c for c in confs if c.node_name and c.node_name not in live]
    if not missing:
        return CheckResult(DOCTOR_PASS, "Chains loaded",
                           f"{len(confs)} conf(s), all present in the graph.")
    return CheckResult(
        DOCTOR_FAIL, "Chains loaded",
        f"{len(missing)} of {len(confs)} conf(s) are on disk but absent from "
        f"the graph ({', '.join(str(c.path.name) for c in missing)}). Usually "
        "an LSP or Calf LV2 plugin is missing, which makes PipeWire drop the "
        "whole file — see the README's plugin dependencies — or PipeWire "
        "hasn't been restarted since the file was written.")


def check_irs_present(confs) -> CheckResult | None:
    """An impulse response a conf names but that isn't on disk.

    The convolver then loads nothing and the speaker correction — the part
    that makes this device-specific at all — is simply absent, with the rest
    of the chain still running so it doesn't sound broken enough to notice.
    """
    missing = {irs.name: c.path.name for c in confs for irs in c.irs
               if not irs.exists()}
    if not missing:
        return None
    shown = ", ".join(sorted(missing)[:2])
    more = f" (+{len(missing) - 2} more)" if len(missing) > 2 else ""
    return CheckResult(
        DOCTOR_FAIL, "Impulse response missing",
        f"{len(missing)} impulse file(s) named by a conf aren't there: "
        f"{shown}{more}. The speaker correction is silently doing nothing — "
        "the rest of the chain still runs, so it won't sound broken enough to "
        "notice. Re-run the converter to copy it back beside the conf.")


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
    return CheckResult(
        DOCTOR_FAIL, "Target sink missing",
        f"the chain is attached to {', '.join(orphans)}, which isn't among "
        "this machine's sinks, so it never joins the audio path. Re-run the "
        "converter to pick up the current speaker sink, or pass "
        "--target-sink with the right node.name.")


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
        "PipeWire's stock config does not load — only pipewire.conf.d/ is "
        f"auto-included. Move them to {doctor.tilde(DEFAULT_OUTPUT_DIR)} and "
        "restart PipeWire.")


def check_wireplumber(version) -> CheckResult:
    """Smart-filter routing needs WirePlumber 0.5+."""
    if version is None:
        return CheckResult(DOCTOR_UNKNOWN, "WirePlumber",
                           "`wireplumber --version` didn't answer, so its "
                           "version wasn't checked.")
    vstr = ".".join(str(v) for v in version)
    if version < (0, 5):
        return CheckResult(
            DOCTOR_FAIL, "WirePlumber",
            f"{vstr} has no smart-filter support, so a chain written the "
            "default way never attaches to your speakers. Upgrade, or re-run "
            "the converter with --target-sink '' for a plain virtual sink you "
            "select as your output.")
    return CheckResult(DOCTOR_PASS, "WirePlumber",
                       f"{vstr} — smart-filter routing supported.")


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
        "EasyEffects is running, and anything routed through it is processed "
        "twice — once by its own chain, once by this one. Quit it and stop it "
        "starting again (its Background Service and autostart, or remove its "
        "autoload).")


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


def gather_pw_doctor() -> tuple[list, list[InstalledConf], list[LiveChain], dict]:
    """Probe everything once, then judge. Returns (checks, confs, chains, facts)."""
    dump = _pw_dump()
    chains = live_chains(dump)
    sinks = sink_names(dump)
    # Only the directory the daemon reads. A conf in _UNSCANNED_CONF_DIR is
    # not installed in any meaningful sense — counting it inflated "Confs: N"
    # and, because it shares a node name with the real one, let "Chains
    # loaded" pass for a file that had loaded for nobody.
    confs = installed_confs(DEFAULT_OUTPUT_DIR.expanduser())
    running = version.get_version()
    wireplumber = _wireplumber_version()

    checks = [c for c in (
        check_stacked_chains(chains, confs),
        check_unpinned_siblings(chains),
        check_confs_loaded(confs, chains, dump),
        check_irs_present(confs),
        check_targets_exist(chains, sinks, dump),
        check_conf_directory(),
        check_wireplumber(wireplumber),
        check_easyeffects_conflict(sinks, chains, dump),
        check_conf_versions(confs, running),
    ) if c is not None]

    if not confs:
        checks.insert(0, CheckResult(
            DOCTOR_WARN, "Installed confs",
            f"no filter-chain conf from this tool in "
            f"{doctor.tilde(DEFAULT_OUTPUT_DIR)} — run dolby_to_pipewire.py on your "
            "tuning XML first."))
    if dump is None:
        checks.append(CheckResult(
            DOCTOR_UNKNOWN, "PipeWire",
            "pw-dump didn't answer — is the PipeWire daemon running? Most of "
            "the checks above need the live graph."))

    facts = {
        "confs": confs,
        "chains": chains,
        "sinks": sorted(sinks),
        "wireplumber": wireplumber,
        "version": running,
    }
    return checks, confs, chains, facts


def _plugin_presence() -> list[str]:
    """Which LV2 packages the chain needs are installed — the usual reason a
    conf loads nothing. Reported as facts, not judged: lv2info is the only
    way to ask, and it isn't always installed."""
    if shutil.which("lv2info") is None:
        return ["lv2info not installed — LV2 plugin presence unknown"]
    out = []
    for label, uri in (("LSP PEQ", LSP_PEQ_URI), ("LSP MBC", LSP_MBC_URI),
                       ("LSP limiter", LSP_LIM_URI),
                       ("Calf bass enhancer", CALF_BE_URI)):
        try:
            rc = subprocess.run(["lv2info", uri], capture_output=True,
                                text=True, timeout=10).returncode
        except (subprocess.SubprocessError, OSError):
            rc = 1
        out.append(f"{label}: {'present' if rc == 0 else 'MISSING'}")
    return out


def report_pw_doctor() -> int:
    """Print the PipeWire-side diagnostic report. Returns a process exit code.

    Every line lands on stdout, console-styled or not, so
    `--doctor > report.txt` captures the whole report — which is the point of
    a block written to be redirected to a file or pasted into an issue.
    """
    checks, confs, chains, facts = gather_pw_doctor()

    # The project name, not this module's: dolby_to_pipewire.py --doctor runs
    # the same report, and a header naming the other script reads as a
    # mis-invocation.
    console.cprint("head", f"speaker-tuning-to-easyeffects {facts['version']}")
    console.cprint("head", "=== PipeWire filter-chain doctor ===")
    print()
    for c in checks:
        doctor.emit_check(c, console.cprint, console._wrap_width())
    print()
    doctor.print_summary(checks, console.cprint)
    print()

    # Raw probed facts, always shown: a verdict can be wrong or UNKNOWN and
    # the report still has to be diagnosable by someone reading it remotely.
    console.cprint("head", "=== Environment (paste this into your issue) ===")
    wp = facts["wireplumber"]
    print(f"  Tool:         speaker-tuning-to-easyeffects {facts['version']}"
          " (PipeWire path)")
    print(f"  WirePlumber:  {'.'.join(map(str, wp)) if wp else 'unknown'}")
    print(f"  Confs:        {len(confs)} in {doctor.tilde(DEFAULT_OUTPUT_DIR)}")
    for c in confs:
        state = "unreadable" if not c.readable else (
            f"smart→{c.target}" if c.smart
            else (f"pinned→{c.pinned}" if c.pinned else "virtual sink, unpinned"))
        print(f"                {doctor.tilde(c.path)} [{c.version or '?'}] {state}")
    print(f"  Live chains:  {len(chains)}"
          + (": " + ", ".join(sorted(c.name for c in chains)) if chains else ""))
    print(f"  Sinks:        {len(facts['sinks'])}")
    for s in facts["sinks"]:
        print(f"                {s}")
    for line in _plugin_presence():
        print(f"  {line}")
    print()

    doctor.print_verdict(checks, console.cprint)
    print()

    # Removing a conf and restarting is the answer to most of the above, and
    # it is the one step a reader can't derive from a diagnosis.
    console.cprint("dim", "To remove a chain: delete its .conf (and matching .irs), "
                  "then restart PipeWire:")
    console.cprint("cta", f"  {PIPEWIRE_RESTART_CMD}")
    print()

    info = gen._gather_speaker_info()
    gen._print_speaker_info(info)
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
    console.cprint("dim", "   PipeWire runs them one after another, not as "
                  "alternatives — every")
    console.cprint("dim", "   stage applies twice. If you were trying a different "
                  "voicing or profile,")
    console.cprint("dim", "   delete the one you don't want (and its .irs), then "
                  "restart PipeWire.")
    console.cprint("dim", "   --doctor lists what is installed and what it does.")
