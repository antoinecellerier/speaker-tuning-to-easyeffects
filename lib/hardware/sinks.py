"""Which PipeWire node the internal speakers are behind.

``pw-dump`` is read exactly once, in ``_enumerate_audio_sinks``, and
everything above it works on the dicts that come back — which is what lets the
whole tiered classification be exercised against synthetic graphs. Selection
runs in two tiers because the tagging cannot be trusted:
``device.icon_name == audio-speakers`` is the strict answer, and a laptop
whose UCM2 profile omits that icon (issue #18) falls back to a relaxed tier of
internal analog outputs — auto-applied when there is one candidate, prompted
for when there are several, and always overridable with ``--autoload-sink``.

Shared by both converters: ``ee_to_pipewire.py`` pins its smart filter to the
sink chosen here, so the two agree on what "the internal speaker" means and
their diagnostic lines stay in lockstep.

Not stdlib-only — it imports ``lib.console``, which owns the optional rich
dependency — but it stays clear of the DSP stack, so the converter can reach
it without paying for numpy.
"""

from __future__ import annotations

import json
import subprocess
import sys

from lib import console, doctor


# Speaker-sink detection for autoload / smart-filter targeting.
#
# NOTE: this is the device-detection (structural) path, not the audio-math
# path, so the "every emitted parameter must trace to an XML field" invariant
# (CLAUDE.md) does NOT apply here — runtime PipeWire node selection has no XML
# provenance. The heuristics below are pragmatic and always overridable by the
# user (--autoload-sink here, --target-sink in ee_to_pipewire.py).

# device.icon_name values that mark an output we never treat as the internal
# speaker, even under the relaxed tier.
_NON_SPEAKER_ICONS = {"audio-headphones", "audio-headset"}


def _enumerate_audio_sinks() -> list[dict]:
    """Return every PipeWire Audio/Sink node with the props we classify on.

    This is the single ``pw-dump`` boundary; tests monkeypatch it to feed
    synthetic sink lists. Each dict carries 'name', 'description', 'profile',
    and 'route' (the fields EasyEffects autoload needs) plus 'icon_name', 'bus',
    and 'api' (used to tell internal speakers from HDMI / Bluetooth / headsets
    and to explain the choice in diagnostics).

    'profile' is the card *profile* description (e.g. "Analog Stereo"); 'route'
    is the active output *route* description (e.g. "Speaker"). EasyEffects keys
    its autoload files on the route description — the node's
    ``device_route_description``, taken from the SPA_PARAM_Route ``description``
    — not the profile. On UCM "HiFi" cards the two happen to coincide
    ("Speaker"), but on a classic ``analog-stereo`` card the profile is
    "Analog Stereo" while the active output route is still "Speaker", so an
    autoload entry filed under the profile never matches and the fallback wins
    (issue #18). 'route' is "" when the active output route can't be resolved
    (virtual sinks, or an older pw-dump that omits Device params); the autoload
    caller skips such sinks rather than fall back to the profile, since guessing
    a filename EE won't match just silently recreates the #18 failure.
    """
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []
    if not isinstance(data, list):  # pw-dump normally emits an array; be defensive
        return []

    # Map PipeWire Device id -> {card-profile-device index -> output route
    # description}. A sink node carries 'device.id' (its Device object) and
    # 'card.profile.device' (the route's device index within that card), so we
    # can resolve the active output route description EasyEffects matches on.
    routes_by_device: dict = {}
    for obj in data:
        if not str(obj.get("type", "")).endswith("Device"):
            continue
        dev_id = obj.get("id")
        if dev_id is None:
            continue
        params = obj.get("info", {}).get("params", {})
        out_routes = {}
        for route in params.get("Route", []) or []:
            if route.get("direction") != "Output":
                continue
            dev_idx = route.get("device")
            desc = route.get("description")
            if dev_idx is not None and desc:
                out_routes[dev_idx] = desc
        if out_routes:
            routes_by_device[dev_id] = out_routes

    sinks = []
    for obj in data:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        route = routes_by_device.get(props.get("device.id"), {}).get(
            props.get("card.profile.device"))
        sinks.append({
            "name": props.get("node.name", ""),
            "description": props.get("node.description", ""),
            "profile": props.get("device.profile.description", ""),
            "route": route or "",
            "icon_name": props.get("device.icon_name", ""),
            "bus": props.get("device.bus", ""),
            "api": props.get("device.api", ""),
        })
    return sinks


def _classify_sink(sink: dict) -> str:
    """Classify a sink as 'strict', 'relaxed', or 'excluded'.

    'strict'   — tagged as an internal speaker (device.icon_name ==
                 'audio-speakers'); the only tier used when tagging is correct.
    'relaxed'  — an internal *analog* output that isn't tagged as a speaker but
                 also isn't obviously HDMI / Bluetooth / a headset. Fallback for
                 laptops whose UCM2 profile omits the speaker icon (issue #18:
                 the generic HDA HiFi-analog.conf sets no DeviceIcon, so
                 WirePlumber assigns the generic 'audio-card-analog' icon).
    'excluded' — everything else: HDMI/DisplayPort/SPDIF, Bluetooth, headsets,
                 and virtual / loopback / combine sinks.
    """
    if sink.get("icon_name") == "audio-speakers":
        return "strict"

    name_l = sink.get("name", "").lower()
    icon_l = sink.get("icon_name", "").lower()
    profile_l = sink.get("profile", "").lower()

    # Must be a real ALSA output sink (excludes virtual/loopback/combine sinks
    # and our own effect_input.* chain node).
    if not name_l.startswith("alsa_output"):
        return "excluded"
    # Not Bluetooth.
    if sink.get("api") == "bluez5" or "bluez" in name_l:
        return "excluded"
    # Not HDMI / DisplayPort / SPDIF (digital passthrough). Match on node.name
    # and icon, and also on the profile description ("Digital Stereo (HDMI)",
    # "... (IEC958)", DisplayPort/SPDIF variants) so a digital output whose
    # node.name lacks the usual hdmi/iec958 token is still excluded.
    _DIGITAL = ("hdmi", "iec958", "spdif", "s/pdif", "displayport")
    if ("hdmi" in name_l or "iec958" in name_l or "hdmi" in icon_l
            or any(m in profile_l for m in _DIGITAL)):
        return "excluded"
    # Not headphones / a headset.
    if sink.get("icon_name") in _NON_SPEAKER_ICONS:
        return "excluded"
    if "headphone" in name_l or "headset" in name_l:
        return "excluded"
    return "relaxed"


def _relaxed_sort_key(sink: dict) -> tuple:
    """Preference order for relaxed candidates (lower sorts first).

    Tie-break only — never excludes. Prefer internal buses (pci/soundwire) over
    usb/unknown, then the exact issue-#18 symptom (audio-card-analog).
    """
    bus_rank = 0 if sink.get("bus") in ("pci", "soundwire") else 1
    icon_rank = 0 if sink.get("icon_name") == "audio-card-analog" else 1
    return (bus_rank, icon_rank, sink.get("name", ""))


def select_speaker_sinks() -> dict:
    """Select internal-speaker sink(s) from PipeWire, with tier reporting.

    Returns a dict {'tier', 'selected', 'all_sinks'}:
      - tier 'strict':  one or more sinks tagged device.icon_name=audio-speakers.
      - tier 'relaxed': no strict match, but internal analog sink(s) found
                        (sorted by preference). The caller decides whether to
                        auto-apply (a unique candidate) or prompt (ambiguous).
      - tier 'none':    no candidate at all.
    'selected' and 'all_sinks' both hold full enumerated dicts (name,
    description, profile, icon_name, bus, api) so callers can both write
    autoload entries and render diagnostics. ('all_sinks' is everything seen.)
    """
    all_sinks = _enumerate_audio_sinks()
    # Single classification pass — keeps the strict/relaxed/excluded partition
    # total and mutually exclusive (no double classify, no drift between arms).
    by_tier: dict[str, list[dict]] = {"strict": [], "relaxed": [], "excluded": []}
    for s in all_sinks:
        by_tier[_classify_sink(s)].append(s)
    if by_tier["strict"]:
        return {"tier": "strict", "selected": by_tier["strict"], "all_sinks": all_sinks}
    if by_tier["relaxed"]:
        relaxed = sorted(by_tier["relaxed"], key=_relaxed_sort_key)
        return {"tier": "relaxed", "selected": relaxed, "all_sinks": all_sinks}
    return {"tier": "none", "selected": [], "all_sinks": all_sinks}


def _sink_diag_line(sink: dict, with_description: bool = True) -> str:
    """One-line diagnostic: the sink's node.name (what --autoload-sink/
    --target-sink take) plus icon/bus detail, and optionally a human
    description, to identify the device. Shared by both converters so their
    candidate/diagnostic lines stay in lockstep."""
    desc = sink.get("description") or ""
    desc_part = f'  "{desc}"' if (with_description and desc) else ""
    # Redacted here, at the one renderer both scripts share, rather than at its
    # five call sites: every listing that reaches a user goes through this, and
    # the widest of them prints every sink in the graph. The dict keeps the real
    # name — callers classify and build autoload filenames from it.
    return doctor.no_bt_address(
        f"node.name={sink.get('name', '?')}{desc_part}  "
        f"(icon={sink.get('icon_name') or '?'}, bus={sink.get('bus') or '?'})")


def _print_sink_candidates(sinks: list[dict]) -> None:
    """Print a numbered candidate list (shared by the picker and skip paths)."""
    for i, s in enumerate(sinks, 1):
        console.cprint("dim", f"  [{i}] {_sink_diag_line(s)}")


def _prompt_pick_sink(candidates: list[dict]) -> dict | None:
    """Prompt for a 1-based choice among already-listed `candidates`, or None.

    The caller is expected to have printed the numbered candidate list. Only
    prompts when both stdin AND stdout are TTYs — piping stdout (e.g.
    ``--autoload | tee log``) would otherwise block on a prompt the user can't
    see — and treats EOF / interrupt / empty / invalid input as a skip, so
    non-interactive runs (pipes, CI, pytest) never block.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        raw = input(f"Select speaker sink [1-{len(candidates)}], "
                    "or Enter to skip: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw:
        return None
    try:
        idx = int(raw)
    except ValueError:
        console.cprint("warn", f"  Not a number: {raw!r} — skipping autoload.")
        return None
    if not (1 <= idx <= len(candidates)):
        console.cprint("warn", f"  Out of range: {idx} — skipping autoload.")
        return None
    return candidates[idx - 1]


def _resolve_autoload_sinks(override_names: list[str], dry_run: bool) -> list[dict]:
    """Resolve which sink(s) to write autoload entries for.

    Honors the --autoload-sink override first; otherwise runs tiered speaker
    detection (strict audio-speakers tag → relaxed internal-analog fallback)
    and prints diagnostics explaining the choice. Returns a list of sink dicts
    (with name/description/profile keys) to write, or [] to skip autoload (with
    the reason already printed to the user).
    """
    # Explicit override: bypass detection entirely.
    if override_names:
        by_name = {s["name"]: s for s in _enumerate_audio_sinks()}
        resolved = []
        for name in override_names:
            sink = by_name.get(name)
            if sink is None:
                # Echoed verbatim, unlike the enumerations below: this is the
                # reader's own argument, and the whole point of the message is
                # letting them compare it against what they meant to type. A
                # redacted echo would hide a one-character mistake, which is
                # the mistake this message exists to catch.
                console.cprint("warn", f"  --autoload-sink {name!r}: not currently "
                               "in pw-dump, so its output route is unknown.")
                sink = {"name": name, "description": name, "profile": "", "route": ""}
            resolved.append(sink)
        return resolved

    sel = select_speaker_sinks()
    tier = sel["tier"]

    if tier == "strict":
        return sel["selected"]

    if tier == "relaxed":
        candidates = sel["selected"]
        console.cprint("warn", "\nNo sink is tagged as an internal speaker "
                       "(device.icon_name=audio-speakers).")
        if len(candidates) == 1:
            sink = candidates[0]
            console.cprint("warn", "  Falling back to the only internal analog output found:")
            console.cprint("dim", f"    {_sink_diag_line(sink)}")
            console.cprint("dim", "  If this is wrong, re-run with --autoload-sink <node.name>.")
            return [sink]
        # Ambiguous: list, then prompt on a TTY (never under --dry-run).
        console.cprint("warn", f"  Found {len(candidates)} internal analog sinks:")
        _print_sink_candidates(candidates)
        chosen = None if dry_run else _prompt_pick_sink(candidates)
        if chosen is not None:
            return [chosen]
        console.cprint("dim", "  Re-run with --autoload-sink <node.name> (repeatable) to choose.")
        return []

    # tier == "none"
    all_sinks = sel["all_sinks"]
    if not all_sinks:
        console.cprint("warn", "\nWarning: no Audio/Sink nodes found via pw-dump; "
                       "cannot configure autoload.")
        console.cprint("dim", "  Is PipeWire running? Run this from your logged-in "
                      "desktop session.")
    else:
        console.cprint("warn", "\nWarning: no internal-speaker sink found (none tagged "
                       "device.icon_name=audio-speakers, and no internal analog "
                       "output).")
        console.cprint("head", "  Audio/Sink nodes seen:")
        _print_sink_candidates(all_sinks)
        console.cprint("dim", "  Re-run with --autoload-sink <node.name> to bind autoload manually.")
    return []
