#!/usr/bin/env python3
"""Print the `--doctor` report under states this machine isn't in.

`preview_output.py` solves this for the generation run by hunting the corpus
for an XML that triggers each finding. A diagnostic has no corpus: what it
says is decided by the machine it runs on, so the states worth reviewing —
the output that isn't the speakers, the install that isn't there — are
exactly the ones the developer's laptop can't be in while you look at them.

So a scenario stubs the *probes* and nothing else. The checks, the wording,
the summary and the verdict are the shipped ones, reached through the real
`_gather_doctor_report` / `_print_doctor_report`; everything a scenario
doesn't name (EasyEffects version, presets, impulse files, kernel age,
hardware) is still read from this machine. A scenario that faked a
`CheckResult` would be reviewing a sentence this tool never prints.

Used by `tools/user_review_capture.py`, which redacts the `###` headers
before a reviewer sees the blocks — the slug names the answer.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import doctor, ee_autostart, ee_paths, ee_socket  # noqa: E402
from lib.hardware import sinks                         # noqa: E402
from lib.pipewire import checks, session               # noqa: E402
from lib.preset import autoload                        # noqa: E402
from lib.report import doctor_run                      # noqa: E402


# Two synthetic sinks, shaped like `_enumerate_audio_sinks` output. The
# speaker one carries the `audio-speakers` icon the strict tier matches; the
# headset is the shape `_classify_sink` excludes on both its api and its
# icon, so it classifies "other" regardless of which arm fires first.
_SPEAKER = {"name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
            "description": "Built-in Audio Speaker", "profile": "Speaker",
            "route": "Speaker",
            "icon_name": "audio-speakers", "bus": "pci", "api": "alsa"}
_HEADSET = {"name": "bluez_output.AA_BB_CC_DD_EE_FF.1",
            "description": "Wireless Headphones", "profile": "a2dp-sink",
            "route": "a2dp-sink",
            "icon_name": "audio-headset-bluetooth", "bus": "bluetooth",
            "api": "bluez5"}
# EasyEffects' own node: a real sink the classifier can say nothing about,
# which is how a report legitimately reaches "unknown".
_VIRTUAL = {"name": "easyeffects_sink", "description": "EasyEffects Sink",
            "profile": "", "route": "", "icon_name": "", "bus": "", "api": ""}

_SPEAKER_AUTOLOAD = {_SPEAKER["name"]: (_SPEAKER["route"], "Dolby-Balanced")}
_BYPASS = "Nothing"          # lib.preset.autoload.BYPASS_PRESET_NAME

# The presets a run would have written. Staged for real, in a temp tree the
# scenario owns, because the report globs them off disk and reads each one:
# on a machine with no EasyEffects install (CI, or a reviewer's laptop) the
# real folder is empty, the generated-preset set comes back empty with it,
# and every check that asks "is the loaded preset one of ours?" answers no —
# which silently rendered the wrong branch under the right slug.
_PRESETS = ("Dolby-Balanced", "Dolby-Detailed")


def _stage_install(root: Path, stamp: str | None = None) -> tuple[Path, Path]:
    """Write the preset + impulse tree a finished run leaves behind. *stamp*
    overrides the `_generator` value, for the stale-version scenario — the
    default is the running build's, exactly as the writers stamp it."""
    out, irs = root / "output", root / "irs"
    out.mkdir(parents=True, exist_ok=True)
    irs.mkdir(parents=True, exist_ok=True)
    for name in _PRESETS:
        kernel = f"{name}-impulse"
        (irs / f"{kernel}.irs").write_bytes(b"")
        # Stamped as the generator stamps them: the report only checks
        # presets it recognises as its own, so an unstamped stage would
        # render the "nothing here is ours" branch under every slug.
        (out / f"{name}.json").write_text(json.dumps({
            "_generator": stamp or autoload.generator_stamp(),
            "output": {"convolver#0": {"kernel-name": kernel, "bypass": False},
                       "blocklist": [], "plugins_order": ["convolver#0"]},
        }, indent=4) + "\n")
    autoload.write_bypass_preset(out, _BYPASS)
    return out, irs


# A scenario says what EasyEffects is doing as well as what the graph looks
# like. Both, because the check under review reads both, and stubbing only
# the graph left the rendered block at the mercy of whichever preset the
# capture machine happened to have loaded — reviewers would then read wording
# the run never printed while the block map still named this scenario
# (code review 2026-08-29).
SCENARIOS: dict[str, dict] = {
    "output-speakers": {
        "why": "playing through the laptop's own speakers, with a generated "
               "preset loaded — the healthy baseline",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced"},
    "output-muted": {
        "why": "playing through the laptop's own speakers with a generated "
               "preset loaded, but the speakers are muted",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "level": (0.064548, True)},
    "bypass-on-speakers": {
        "why": "playing through the laptop's own speakers with the silent "
               "bypass preset loaded — the fault the check exists for",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": _BYPASS},
    "output-other-autoloaded": {
        "why": "playing through a Bluetooth headset; the speakers have an "
               "autoload entry naming a generated preset",
        "sinks": [_SPEAKER, _HEADSET], "default": _HEADSET["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": _BYPASS},
    "output-other-no-autoload": {
        "why": "playing through a Bluetooth headset, and nothing autoloads on "
               "the speakers (the run was never given --autoload)",
        "sinks": [_SPEAKER, _HEADSET], "default": _HEADSET["name"],
        "autoload": {}, "preset": _BYPASS},
    "dolby-on-headset": {
        "why": "playing through a Bluetooth headset with a generated speaker "
               "preset loaded, and no fallback set — the over-processed "
               "headphones fault",
        "sinks": [_SPEAKER, _HEADSET], "default": _HEADSET["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced"},
    "output-unknown": {
        "why": "default sink is EasyEffects' own virtual node, which says "
               "nothing about where the audio physically comes out",
        "sinks": [_VIRTUAL], "default": _VIRTUAL["name"],
        "autoload": {}, "preset": _BYPASS},
    "presets-from-another-version": {
        "why": "the presets on disk were written by an older build of this "
               "tool — the report has to send the reader back to re-run it",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "stamp": f"{autoload.GENERATOR_PREFIX} v2026.01"},
    "no-pipewire": {
        "why": "PipeWire isn't answering — the section that describes it has "
               "to say so rather than print nothing",
        "sinks": [], "default": "", "no_pipewire": True,
        "clear_saved_sink": True, "autoload": {}, "preset": _BYPASS},
    "speaker-routed-past-volume": {
        "why": "a machine whose kernel entry reroutes the bass speaker off a "
               "widget with no volume amp — the routing check's fault state, "
               "reached through the same demo hook every copy preview uses",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "env": {"DEMO_SPEAKER_ROUTE": "17AA3906"}},
    "speaker-level-unlisted": {
        "why": "the same dead volume path on a machine no table lists (a "
               "made-up id): the hedged copy, with no upstream fix to cite "
               "(issue #95)",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "env": {"DEMO_SPEAKER_ROUTE": "1D059999"}},
    "graph-rate-too-high": {
        "why": "a session whose PipeWire graph runs above 48 kHz — EasyEffects "
               "resamples the correction filter up without compensating its "
               "gain, so the preset plays hot and the report has to say so",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "graph_rate": 192000},
    "session-launched": {
        "why": "EasyEffects' own autostart toggle is off, and the desktop "
               "session's startup file launches it with --service-mode "
               "instead (issue #117)",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "rc": "[Window]\nautostartOnLogin=false\n",
        "launched_by": "~/.config/hypr/hyprland.conf", "launch_service": True,
        "service_argv": True, "compositor": "Hyprland"},
    "service-mode-untraced": {
        "why": "the autostart toggle is off and EasyEffects runs with "
               "--service-mode, but no startup file the check reads launches "
               "it: the report can't tell whether it comes back after a reboot",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "rc": "[Window]\nautostartOnLogin=false\n",
        "launched_by": "", "service_argv": True},
    "compositor-toggle-only": {
        "why": "a Hyprland session with EasyEffects' own autostart toggle on and "
               "nothing in Hyprland's startup: the toggle only writes an "
               "autostart entry, which Hyprland runs only through a helper",
        "sinks": [_SPEAKER, _HEADSET], "default": _SPEAKER["name"],
        "autoload": _SPEAKER_AUTOLOAD, "preset": "Dolby-Balanced",
        "rc": "[Window]\nautostartOnLogin=true\n",
        "launched_by": "", "service_argv": True, "compositor": "Hyprland",
        "startup_hint": ("In ~/.config/hypr/hyprland.conf, add:",
                         "exec-once = easyeffects --hide-window --service-mode")},
}


@contextlib.contextmanager
def _scenario(slug: str):
    """Stub the probes one scenario needs, and restore every one after.

    One lever per kind of machine state the report reads: the sink graph and
    its soft-mixer flag, the default sink, the autoload directory, the presets
    and impulse files on disk, what EasyEffects says it has loaded, how it
    gets started (its startup files and running arguments), and for some
    scenarios the PipeWire session. Any left real makes the rendered block
    depend on the laptop rather than on the scenario. ``rc`` stages the rc a scenario about its toggles
    describes. One more, ``env``, reaches the states
    keyed to hardware the capture machine isn't: it sets the same DEMO_*
    variables the copy previews use, so the hardware checks fire through
    their shipped detection rather than a faked CheckResult. The stubs are restored on the way
    out — all scenarios run in one process, and a leak would let one decide
    the next one's answer while the block map still claimed otherwise.

    Yields the staged install as (output_dir, irs_dir, autoload_dir, rc_path).
    """
    spec = SCENARIOS[slug]
    saved_env = {k: os.environ.get(k) for k in spec.get("env", ())}
    saved = {
        "enum": sinks._enumerate_audio_sinks,
        "default": sinks.live_session,
        "soft_mixer": sinks.soft_mixer_in_use,
        "query": doctor_run._ee_query,
        "read_rc": autoload.read_ee_rc,
        "launch": (ee_autostart.background_launch_source,
                   ee_autostart.service_mode_launch, ee_autostart.own_processes),
        "session": (session.read_settings, session.read_xruns,
                    session.process_age, session.pipewire_version,
                    session.wireplumber_version),
    }
    sinks._enumerate_audio_sinks = lambda: list(spec["sinks"])
    # The fixed-level check reads PipeWire's soft-mixer flag before warning;
    # a capture machine with it set would render no WARN.
    sinks.soft_mixer_in_use = lambda: False
    # (default sink, running WirePlumber, sink levels) — the version and
    # levels ride the same probe, so a scenario with no daemon shows all of
    # them in their unread forms.
    sinks.live_session = lambda: (
        (checks.DefaultSink(reason=checks.NO_DUMP_REASON), None, {})
        if spec.get("no_pipewire")
        else (checks.DefaultSink(effective=spec["default"]),
              session.Version("0.5.15", (0, 5, 15), claim="running"),
              {spec["default"]: sinks.SinkLevel(*spec.get("level", (1.0,)))}))
    # A daemon that isn't answering must not answer anything: the clock, the
    # dropout window, the uptimes and the daemon version all show their
    # unread forms, which is the whole point of that scenario.
    if spec.get("no_pipewire"):
        session.read_settings = lambda: session.ClockSettings(
            reason="no answer from pw-metadata")
        session.read_xruns = lambda *a, **kw: session.Dropouts(
            reason="pw-top didn't answer")
        session.process_age = lambda name: None
        session.pipewire_version = lambda: session.Version(
            reason="no answer from pw-cli")
        # The graph named no WirePlumber either, so the row falls back to the
        # installed binary — which this scenario's machine cannot run.
        session.wireplumber_version = lambda: session.Version(
            reason="wireplumber not found")

    # The one scenario that varies the clock rather than the daemon's health:
    # everything else about the machine is the healthy baseline's, so the only
    # thing the report can be reacting to is the rate.
    if spec.get("graph_rate"):
        rate = spec["graph_rate"]
        session.read_settings = lambda: session.ClockSettings(
            rate=str(rate), quantum="1024", min_quantum="32",
            max_quantum="2048", force_quantum="0", force_rate="0")
        session.read_xruns = lambda *a, **kw: session.Dropouts(
            sink=0, chain=0, chain_node="easyeffects_sink", sink_recent=0,
            chain_recent=0, window_s=5.0, playing=True, sink_is_driver=True,
            running_quantum=1024, running_rate=rate)

    # A running daemon is the authoritative source for the loaded preset, so
    # the scenario has to answer as one or the real EasyEffects on the capture
    # machine decides what the block says.
    def query(request):
        value = (spec["preset"] if request == ee_socket.PRESET_REQUEST
                 else "2")            # 2 = global bypass off
        return ee_socket.EEReply(value=value, reached=True, answered=True)

    # `useDefaultOutputDevice` off means EasyEffects is pinned to a device and
    # the rc is authoritative — which would route around the stubbed graph
    # entirely and collapse every scenario onto the capture machine's own
    # pinned sink. The rest of the rc stays real, the reader's own install,
    # unless the scenario stages its own rc (``rc``).
    def read_rc(rc_text):
        rc = saved["read_rc"](rc_text)
        rc["use_default_output_device"] = True
        # With no graph to follow, the rc's cached sink would fill the row
        # with the capture machine's device and hide the unread form.
        if spec.get("clear_saved_sink"):
            rc["output_device"] = ""
        return rc

    doctor_run._ee_query = query
    autoload.read_ee_rc = read_rc
    # How EasyEffects gets started is machine state too: the startup files in
    # the capture machine's home and its running process's arguments.
    ee_autostart.own_processes = lambda: []
    ee_autostart.background_launch_source = lambda *a: ee_autostart.Launch(
        spec.get("launched_by", ""), spec.get("launch_service", False),
        compositor=spec.get("compositor", ""),
        startup_hint=spec.get("startup_hint", ("", "")))
    ee_autostart.service_mode_launch = lambda *a: spec.get("service_argv", False)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        out_dir, irs_dir = _stage_install(root, spec.get("stamp"))
        autoload_dir = root / "autoload"
        autoload_dir.mkdir()
        # A scenario about the rc's own toggles stages the rc it describes;
        # the others read the capture machine's, as the reader's install.
        rc_path = ee_paths.DEFAULT_EASYEFFECTS_RC
        if "rc" in spec:
            rc_path = root / "easyeffectsrc"
            rc_path.write_text(spec["rc"])
        for device, (route, preset) in spec["autoload"].items():
            autoload.write_autoload(autoload_dir, device_name=device,
                                    device_description="", device_profile=route,
                                    preset_name=preset)
        try:
            # Set inside the try, unlike the stubs above: this one escapes
            # into subprocesses, so a staging failure that skipped the
            # restore would inject the synthetic codec into every later
            # scenario and test in the process.
            os.environ.update(spec.get("env", {}))
            yield out_dir, irs_dir, autoload_dir, rc_path
        finally:
            sinks._enumerate_audio_sinks = saved["enum"]
            sinks.live_session = saved["default"]
            sinks.soft_mixer_in_use = saved["soft_mixer"]
            doctor_run._ee_query = saved["query"]
            autoload.read_ee_rc = saved["read_rc"]
            (ee_autostart.background_launch_source,
             ee_autostart.service_mode_launch,
             ee_autostart.own_processes) = saved["launch"]
            (session.read_settings, session.read_xruns,
             session.process_age, session.pipewire_version,
             session.wireplumber_version) = saved["session"]
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def render(slug: str) -> None:
    """Print one scenario's report, exactly as `--doctor` would print it."""
    with _scenario(slug) as (out_dir, irs_dir, autoload_dir, rc_path):
        # custom_dirs=False on purpose: these *are* temp dirs, but the run
        # being portrayed wrote to the default ones, and passing True would
        # swap the install check for the "skipping location checks" UNKNOWN
        # a real reader never sees.
        report = doctor_run._gather_doctor_report(
            out_dir, irs_dir, rc_path,
            custom_dirs=False, autoload_dir=autoload_dir)
        # The two rows that would otherwise name the staging tree. The run
        # being portrayed wrote to the default install; the temp tree exists
        # only because a preview must not touch the reader's own. Corrected
        # on the facts, not by rewriting rendered text, so a check that names
        # a path still says whatever the code made it say.
        report.facts["output_dir"] = doctor.tilde(ee_paths.DEFAULT_OUTPUT_DIR)
        report.facts["irs_dir"] = doctor.tilde(ee_paths.DEFAULT_IRS_DIR)
        report.facts["rc_path"] = doctor.tilde(ee_paths.DEFAULT_EASYEFFECTS_RC)
        doctor_run._print_doctor_report(report)


# --- The PipeWire doctor (dolby_to_pipewire.py / ee_to_pipewire.py --doctor) ---
#
# The same idea for the other doctor. What it judges is the confs on disk and
# the graph pw-dump returns, so a scenario stages the first and answers as the
# second; the checks, the wording and the layout are the shipped
# `checks.report_pw_doctor`. Hardware and the plugin probe stay real, like the
# EasyEffects scenarios' kernel and hardware.

_PW_SPEAKER = "alsa_output.pci-0000_00_1f.3.analog-stereo"

# Each chain is (name, mode): "smart" attaches to the speaker sink as a smart
# filter, "pinned" is a virtual sink whose playback is pinned to it, "loose"
# a virtual sink with no playback target. ``default`` is the sink selected as
# the output; ``configured`` the remembered pick, when it differs.
PW_SCENARIOS: dict[str, dict] = {
    "pw-healthy": {
        "why": "one chain attached to the speakers, the speakers selected — "
               "the default install, healthy",
        "chains": [("Dolby_Balanced", "smart")],
        "default": _PW_SPEAKER,
    },
    "pw-stacked": {
        "why": "two chains attached to the same speakers by two separate runs, "
               "so PipeWire runs them one after the other",
        "chains": [("Dolby_Balanced", "smart"), ("Dolby_Warm", "smart")],
        "default": _PW_SPEAKER,
    },
    "pw-several-virtual-sinks": {
        "why": "the three voicings installed as separate outputs to compare "
               "(--variant all --target-sink ''), one of them selected "
               "(issue #144)",
        "chains": [("Dolby_Balanced", "pinned"), ("Dolby_Detailed", "pinned"),
                   ("Dolby_Warm", "pinned")],
        "default": "effect_input.Dolby_Balanced",
    },
    "pw-several-unpinned": {
        "why": "two virtual-sink chains from separate runs, neither pinned to "
               "the speakers, one of them selected",
        "chains": [("Dolby_Balanced", "loose"), ("Dolby_Music_Balanced", "loose")],
        "default": "effect_input.Dolby_Balanced",
    },
    "pw-chain-selected": {
        "why": "the default install, but the chain itself was picked as the "
               "output in sound settings (issue #63)",
        "chains": [("Dolby_Balanced", "smart")],
        "default": "effect_input.Dolby_Balanced",
    },
    "pw-chain-muted": {
        "why": "the default install, the speakers selected and unmuted, but "
               "the chain's own sink came up muted (issue #152)",
        "chains": [("Dolby_Balanced", "smart")],
        "default": _PW_SPEAKER,
        "levels": {"effect_input.Dolby_Balanced": (1.0, True)},
    },
}


def _pw_conf_text(name: str, mode: str) -> str:
    """A conf as installed_confs reads it: the header and the routing props."""
    from lib import version
    from lib.pipewire import conf as pw_conf
    capture = [f'node.name = "effect_input.{name}"', 'media.class = "Audio/Sink"']
    playback = [f'node.name = "effect_output.{name}"']
    if mode == "smart":
        capture += ["filter.smart = true",
                    f'filter.smart.target = {{ node.name = "{_PW_SPEAKER}" }}']
    elif mode == "pinned":
        playback.append(f'target.object = "{_PW_SPEAKER}"')
    return (f"{pw_conf.CONF_HEADER_MARK} — see\n"
            f"# version: {version.get_version()}\n"
            "context.modules = [ { name = \"libpipewire-module-filter-chain\"\n"
            "  args = {\n"
            f"    capture.props = {{ {' '.join(capture)} }}\n"
            f"    playback.props = {{ {' '.join(playback)} }}\n"
            "  } } ]\n")


def _pw_dump(spec: dict) -> list:
    """The pw-dump a machine in this scenario returns."""
    def node(name, **props):
        # Every sink at full and unmuted unless the scenario says otherwise.
        obj = {"type": "PipeWire:Interface:Node",
               "info": {"props": {"node.name": name, **props}}}
        if props.get("media.class") == "Audio/Sink":
            linear, mute = spec.get("levels", {}).get(name, (1.0, False))
            obj["info"]["params"] = {"Props": [
                {"channelVolumes": [linear, linear], "mute": mute}]}
        return obj
    dump = [node(_PW_SPEAKER, **{"media.class": "Audio/Sink",
                                 "node.description": "Built-in Audio Speaker"}),
            {"type": "PipeWire:Interface:Client",
             "info": {"props": {"wireplumber.daemon": True,
                                "application.version": "0.5.18"}}}]
    for name, mode in spec["chains"]:
        capture = {"media.class": "Audio/Sink"}
        playback = {"node.passive": True}
        if mode == "smart":
            capture.update({"filter.smart": True,
                            "node.link-group": f"{name}_smart_filter",
                            "filter.smart.target":
                                f'{{ node.name = "{_PW_SPEAKER}" }}'})
            playback["node.link-group"] = f"{name}_smart_filter"
        elif mode == "pinned":
            playback["target.object"] = _PW_SPEAKER
        dump += [node(f"effect_input.{name}", **capture),
                 node(f"effect_output.{name}", **playback)]
    entries = [{"subject": 0, "key": key, "type": "Spa:String:JSON",
                "value": {"name": value}}
               for key, value in (
                   ("default.audio.sink", spec["default"]),
                   ("default.configured.audio.sink",
                    spec.get("configured", spec["default"])))]
    dump.append({"type": "PipeWire:Interface:Metadata",
                 "props": {"metadata.name": "default"}, "metadata": entries})
    return dump


@contextlib.contextmanager
def _pw_scenario(slug: str):
    """Stage one PipeWire scenario's confs and graph; restore every probe after.

    The conf directory is a temp dir, so the report would name it. Paths reach
    the screen only through `doctor.tilde`, so that one function maps the
    staging dir back to the directory the portrayed run installed into: the
    rendered rows then name the path a reader's report would."""
    spec = PW_SCENARIOS[slug]
    saved = (checks._pw_dump, checks.DEFAULT_OUTPUT_DIR,
             checks._UNSCANNED_CONF_DIR, doctor.tilde,
             session.read_settings, session.read_xruns, session.process_age)
    real_dir, real_tilde = checks.DEFAULT_OUTPUT_DIR, doctor.tilde
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp)
        for name, mode in spec["chains"]:
            (staged / f"{name}.conf").write_text(_pw_conf_text(name, mode))
        dump = _pw_dump(spec)

        def tilde(path):
            text = str(path)
            if text.startswith(str(staged)):
                text = str(real_dir) + text[len(str(staged)):]
            return real_tilde(text)

        try:
            checks._pw_dump = lambda: dump
            checks.DEFAULT_OUTPUT_DIR = staged
            # filter-chain.conf.d too: its check reads that directory, and the
            # capture machine's own would add a WARN no scenario describes.
            checks._UNSCANNED_CONF_DIR = staged / "filter-chain.conf.d"
            doctor.tilde = tilde
            # The clock and the five-second dropout window describe the
            # capture machine's session, not the scenario's: a healthy one.
            session.read_settings = lambda: session.ClockSettings(
                rate="48000", quantum="1024", min_quantum="32",
                max_quantum="2048", force_quantum="0", force_rate="0")
            session.read_xruns = lambda *a, **kw: session.Dropouts(
                sink=0, chain=0, chain_node="", sink_recent=0, chain_recent=0,
                window_s=5.0, playing=True, sink_is_driver=True,
                running_quantum=1024, running_rate=48000)
            session.process_age = lambda name: 3 * 3600.0
            yield
        finally:
            (checks._pw_dump, checks.DEFAULT_OUTPUT_DIR,
             checks._UNSCANNED_CONF_DIR, doctor.tilde,
             session.read_settings, session.read_xruns,
             session.process_age) = saved


def render_pw(slug: str) -> None:
    """Print one PipeWire scenario's report, exactly as `--doctor` would."""
    with _pw_scenario(slug):
        checks.report_pw_doctor()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="print the scenario slugs and what each one means")
    ap.add_argument("--scenario", action="append", default=[], metavar="SLUG",
                    help="render only this scenario (repeatable; "
                         "default: all of them, in registry order)")
    ap.add_argument("--width", type=int, default=80, metavar="COLS",
                    help="terminal width to wrap at (default: 80)")
    ap.add_argument("--pipewire", action="store_true",
                    help="render the PipeWire doctor's scenarios "
                         "(dolby_to_pipewire.py --doctor) instead")
    args = ap.parse_args(argv)
    registry, draw = ((PW_SCENARIOS, render_pw) if args.pipewire
                      else (SCENARIOS, render))

    if args.list:
        for slug, spec in registry.items():
            print(f"{slug}\n    {spec['why']}")
        return 0

    unknown = [s for s in args.scenario if s not in registry]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)} "
                 f"(--list prints them)")
    slugs = args.scenario or list(registry)

    # The scripts wrap to the real terminal, and a capture is a pipe — pin
    # the width through the environment, the same lever preview_output.py
    # uses (lib.console reads it via shutil.get_terminal_size).
    os.environ["COLUMNS"] = str(args.width)
    # Same rule/header/rule shape preview_output.py prints, so one redactor
    # in user_review_capture.py strips both — the slug names the answer and
    # must never reach a reviewer.
    rule = "─" * min(args.width, 100)
    for slug in slugs:
        print(f"\n{rule}\n### {slug}  —  {registry[slug]['why']}\n{rule}")
        draw(slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
