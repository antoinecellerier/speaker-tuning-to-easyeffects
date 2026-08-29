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
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import ee_paths, ee_socket                    # noqa: E402
from lib.hardware import sinks                         # noqa: E402
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
    "output-unknown": {
        "why": "default sink is EasyEffects' own virtual node, which says "
               "nothing about where the audio physically comes out",
        "sinks": [_VIRTUAL], "default": _VIRTUAL["name"],
        "autoload": {}, "preset": _BYPASS},
}


@contextlib.contextmanager
def _scenario(slug: str):
    """Stub the probes one scenario needs, and restore every one after.

    Four levers, because the report reads four things: the sink graph, the
    default sink, the autoload directory, and what EasyEffects says it has
    loaded. They are restored on the way out — all scenarios run in one
    process, and a leak would let one decide the next one's answer while the
    block map still claimed otherwise.
    """
    spec = SCENARIOS[slug]
    saved = {
        "enum": sinks._enumerate_audio_sinks,
        "default": sinks.live_default_sink,
        "query": doctor_run._ee_query,
        "read_rc": autoload.read_ee_rc,
    }
    sinks._enumerate_audio_sinks = lambda: list(spec["sinks"])
    sinks.live_default_sink = lambda: spec["default"]

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
    # pinned sink. The rest of the rc stays real: it is the reader's own
    # install this is describing.
    def read_rc(rc_text):
        rc = saved["read_rc"](rc_text)
        rc["use_default_output_device"] = True
        return rc

    doctor_run._ee_query = query
    autoload.read_ee_rc = read_rc
    with tempfile.TemporaryDirectory() as tmp:
        for device, (route, preset) in spec["autoload"].items():
            autoload.write_autoload(Path(tmp), device_name=device,
                                    device_description="", device_profile=route,
                                    preset_name=preset)
        try:
            yield Path(tmp)
        finally:
            sinks._enumerate_audio_sinks = saved["enum"]
            sinks.live_default_sink = saved["default"]
            doctor_run._ee_query = saved["query"]
            autoload.read_ee_rc = saved["read_rc"]


def render(slug: str) -> None:
    """Print one scenario's report, exactly as `--doctor` would print it."""
    with _scenario(slug) as autoload_dir:
        report = doctor_run._gather_doctor_report(
            ee_paths.DEFAULT_OUTPUT_DIR, ee_paths.DEFAULT_IRS_DIR,
            ee_paths.DEFAULT_EASYEFFECTS_RC, custom_dirs=False,
            autoload_dir=autoload_dir)
        doctor_run._print_doctor_report(report)


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
    args = ap.parse_args(argv)

    if args.list:
        for slug, spec in SCENARIOS.items():
            print(f"{slug}\n    {spec['why']}")
        return 0

    unknown = [s for s in args.scenario if s not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)} "
                 f"(--list prints them)")
    slugs = args.scenario or list(SCENARIOS)

    # The scripts wrap to the real terminal, and a capture is a pipe — pin
    # the width through the environment, the same lever preview_output.py
    # uses (lib.console reads it via shutil.get_terminal_size).
    os.environ["COLUMNS"] = str(args.width)
    # Same rule/header/rule shape preview_output.py prints, so one redactor
    # in user_review_capture.py strips both — the slug names the answer and
    # must never reach a reviewer.
    rule = "─" * min(args.width, 100)
    for slug in slugs:
        print(f"\n{rule}\n### {slug}  —  {SCENARIOS[slug]['why']}\n{rule}")
        render(slug)
    return 0


if __name__ == "__main__":
    sys.exit(main())
