"""Speaker-sink detection for autoload / smart-filter targeting.

Covers the tiered detection added for issue #18 (a Lenovo IdeaPad Pro 5
14AHP9 whose internal speaker is mis-tagged ``audio-card-analog`` instead of
``audio-speakers`` because it falls back to the generic HDA ``HiFi-analog.conf``
UCM2 profile, which sets no ``DeviceIcon``).

The single ``pw-dump`` boundary is ``_enumerate_audio_sinks()``; every test
monkeypatches it to feed synthetic sink lists, except one that patches
``subprocess.run`` directly to exercise the JSON parse + error guards. No test
touches a real PipeWire daemon, and interactive paths are guarded so nothing
ever blocks.
"""

from __future__ import annotations

import builtins
import json
import subprocess

import pytest

import dolby_to_easyeffects as d
import ee_to_pipewire as pw


# --- Synthetic sinks (in the _enumerate_audio_sinks() dict shape) -----------

STRICT_SPEAKER = {
    "name": "alsa_output.pci-0000_00_1f.3.analog-stereo",
    "description": "Built-in Audio Analog Stereo",
    "profile": "Analog Stereo",
    "icon_name": "audio-speakers",
    "bus": "pci",
    "api": "alsa",
}

# Issue #18: internal speaker mis-tagged as a generic analog card.
IDEAPAD_ANALOG = {
    "name": "alsa_output.pci-0000_63_00.6.analog-stereo",
    "description": "Ryzen HD Audio Controller Analog Stereo",
    "profile": "Analog Stereo",
    "icon_name": "audio-card-analog",
    "bus": "pci",
    "api": "alsa",
}

HDMI_SINK = {
    "name": "alsa_output.pci-0000_00_1f.3.hdmi-stereo",
    "description": "Built-in Audio Digital Stereo (HDMI)",
    "profile": "Digital Stereo (HDMI)",
    "icon_name": "video-display",
    "bus": "pci",
    "api": "alsa",
}

IEC958_SINK = {
    "name": "alsa_output.pci-0000_00_1f.3.iec958-stereo",
    "description": "Built-in Audio Digital Stereo (IEC958)",
    "profile": "Digital Stereo (IEC958)",
    "icon_name": "audio-card-analog",  # icon doesn't save it — name does
    "bus": "pci",
    "api": "alsa",
}

BLUEZ_SINK = {
    "name": "bluez_output.AA_BB_CC_DD_EE_FF.1",
    "description": "Some Bluetooth Headphones",
    "profile": "",
    "icon_name": "audio-headset",
    "bus": "",
    "api": "bluez5",
}

USB_HEADSET = {
    "name": "alsa_output.usb-Generic_USB_Headset-00.analog-stereo",
    "description": "USB Headset Analog Stereo",
    "profile": "Analog Stereo",
    "icon_name": "audio-headset",
    "bus": "usb",
    "api": "alsa",
}

# A digital output whose node.name lacks the usual hdmi/iec958 token but whose
# profile description marks it digital — must still be excluded (F3).
SPDIF_SINK = {
    "name": "alsa_output.pci-0000_00_1f.3.pro-output-0",
    "description": "Built-in Audio Digital Stereo (S/PDIF)",
    "profile": "Digital Stereo (S/PDIF)",
    "icon_name": "audio-card-analog",
    "bus": "pci",
    "api": "alsa",
}

# A second internal analog sink that is NOT a headset (e.g. a USB DAC) — a
# legitimate relaxed candidate, used for the ambiguous case.
USB_DAC_ANALOG = {
    "name": "alsa_output.usb-Some_DAC-00.analog-stereo",
    "description": "Some USB DAC Analog Stereo",
    "profile": "Analog Stereo",
    "icon_name": "audio-card-analog",
    "bus": "usb",
    "api": "alsa",
}


def _patch_sinks(monkeypatch, sinks):
    """Make both modules' detection see `sinks` (single pw-dump boundary)."""
    monkeypatch.setattr(d, "_enumerate_audio_sinks", lambda: list(sinks))


def _set_tty(monkeypatch, *, stdin=True, stdout=True):
    """Force stdin/stdout isatty() — a prompt requires both to be TTYs."""
    monkeypatch.setattr(d.sys.stdin, "isatty", lambda: stdin)
    monkeypatch.setattr(d.sys.stdout, "isatty", lambda: stdout)


# --- Classification ---------------------------------------------------------

@pytest.mark.parametrize("sink,expected", [
    (STRICT_SPEAKER, "strict"),
    (IDEAPAD_ANALOG, "relaxed"),
    (USB_DAC_ANALOG, "relaxed"),
    (HDMI_SINK, "excluded"),
    (IEC958_SINK, "excluded"),
    (SPDIF_SINK, "excluded"),  # digital by profile description, not by node.name
    (BLUEZ_SINK, "excluded"),
    (USB_HEADSET, "excluded"),
])
def test_classify_sink(sink, expected):
    assert d._classify_sink(sink) == expected


def test_classify_excludes_virtual_sink():
    """Non-alsa_output nodes (virtual / our own chain) are never relaxed."""
    virtual = {"name": "effect_input.dolby", "icon_name": "", "bus": "", "api": ""}
    assert d._classify_sink(virtual) == "excluded"


# --- select_speaker_sinks tiers ---------------------------------------------

def test_strict_match_wins(monkeypatch):
    """A correctly-tagged speaker takes the strict tier even alongside analog."""
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, STRICT_SPEAKER])
    sel = d.select_speaker_sinks()
    assert sel["tier"] == "strict"
    assert [s["name"] for s in sel["selected"]] == [STRICT_SPEAKER["name"]]
    # selected carries the full enumerated dict (autoload reads name/desc/profile).
    assert sel["selected"][0]["profile"] == STRICT_SPEAKER["profile"]


def test_relaxed_single_ideapad(monkeypatch):
    """No strict tag → the lone internal analog sink is the relaxed pick."""
    _patch_sinks(monkeypatch, [HDMI_SINK, IDEAPAD_ANALOG, BLUEZ_SINK])
    sel = d.select_speaker_sinks()
    assert sel["tier"] == "relaxed"
    assert [s["name"] for s in sel["selected"]] == [IDEAPAD_ANALOG["name"]]


def test_exclusions_yield_none(monkeypatch):
    """HDMI / iec958 / headset / bluez only → no candidate at all."""
    _patch_sinks(monkeypatch, [HDMI_SINK, IEC958_SINK, USB_HEADSET, BLUEZ_SINK])
    sel = d.select_speaker_sinks()
    assert sel["tier"] == "none"
    assert sel["selected"] == []
    # all_sinks is preserved for diagnostics.
    assert len(sel["all_sinks"]) == 4


def test_relaxed_ambiguous_sorted_pci_first(monkeypatch):
    """Two internal analog sinks → relaxed tier, pci preferred over usb."""
    _patch_sinks(monkeypatch, [USB_DAC_ANALOG, IDEAPAD_ANALOG])
    sel = d.select_speaker_sinks()
    assert sel["tier"] == "relaxed"
    names = [s["name"] for s in sel["selected"]]
    assert names == [IDEAPAD_ANALOG["name"], USB_DAC_ANALOG["name"]]


def test_empty_yields_none(monkeypatch):
    _patch_sinks(monkeypatch, [])
    sel = d.select_speaker_sinks()
    assert sel["tier"] == "none"
    assert sel["all_sinks"] == []


# --- _enumerate_audio_sinks parse + error guards ----------------------------

def test_enumerate_parses_pwdump(monkeypatch):
    """The pw-dump JSON boundary maps props and drops non-Audio/Sink nodes."""
    dump = [
        {"info": {"props": {
            "media.class": "Audio/Sink",
            "node.name": IDEAPAD_ANALOG["name"],
            "node.description": IDEAPAD_ANALOG["description"],
            "device.profile.description": IDEAPAD_ANALOG["profile"],
            "device.icon_name": IDEAPAD_ANALOG["icon_name"],
            "device.bus": IDEAPAD_ANALOG["bus"],
            "device.api": IDEAPAD_ANALOG["api"],
        }}},
        {"info": {"props": {"media.class": "Audio/Source",
                            "node.name": "some.mic"}}},  # dropped
        {"info": {"props": {"media.class": "Video/Sink"}}},  # dropped
    ]

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    sinks = d._enumerate_audio_sinks()
    assert len(sinks) == 1
    # No Device object in the dump, so the route can't be resolved → "".
    assert sinks[0] == {**IDEAPAD_ANALOG, "route": ""}


def test_enumerate_resolves_route_for_analog_stereo(monkeypatch):
    """#18: a classic analog-stereo card whose active output route is "Speaker".

    EasyEffects keys autoload on the route description, so 'route' must be
    "Speaker" even though the card *profile* is "Analog Stereo" — filing the
    entry under the profile is the bug that made the Nothing fallback win.
    """
    dump = [
        {"type": "PipeWire:Interface:Device", "id": 50, "info": {"params": {"Route": [
            {"direction": "Output", "device": 0, "description": "Speaker"},
            {"direction": "Input", "device": 1, "description": "Internal Microphone"},
        ]}}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Sink",
            "node.name": IDEAPAD_ANALOG["name"],
            "node.description": IDEAPAD_ANALOG["description"],
            "device.profile.description": "Analog Stereo",
            "device.icon_name": IDEAPAD_ANALOG["icon_name"],
            "device.bus": "pci",
            "device.api": "alsa",
            "device.id": 50,
            "card.profile.device": 0,
        }}},
    ]

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    sinks = d._enumerate_audio_sinks()
    assert len(sinks) == 1
    assert sinks[0]["profile"] == "Analog Stereo"
    assert sinks[0]["route"] == "Speaker"


def test_enumerate_route_matches_profile_on_ucm_hifi(monkeypatch):
    """UCM 'HiFi' cards expose profile == route == "Speaker"; route stays "Speaker"
    (the dev-machine case — confirms the new resolution doesn't regress it)."""
    dump = [
        {"type": "PipeWire:Interface:Device", "id": 48, "info": {"params": {"Route": [
            {"direction": "Output", "device": 3, "description": "Speaker"},
        ]}}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Sink",
            "node.name": "alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink",
            "node.description": "Speaker",
            "device.profile.description": "Speaker",
            "device.icon_name": "audio-speakers",
            "device.bus": "pci",
            "device.api": "alsa",
            "device.id": 48,
            "card.profile.device": 3,
        }}},
    ]

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    sinks = d._enumerate_audio_sinks()
    assert sinks[0]["profile"] == "Speaker"
    assert sinks[0]["route"] == "Speaker"


def test_enumerate_route_empty_when_unresolved(monkeypatch):
    """No matching Output route (only an Input route here) → 'route' is "".

    The profile is deliberately NOT used as a fallback: keying an autoload entry
    on the profile is exactly the #18 mismatch, so the caller skips such sinks.
    """
    dump = [
        {"type": "PipeWire:Interface:Device", "id": 7, "info": {"params": {"Route": [
            {"direction": "Input", "device": 0, "description": "Mic"},
        ]}}},
        {"type": "PipeWire:Interface:Node", "info": {"props": {
            "media.class": "Audio/Sink",
            "node.name": "alsa_output.usb-Some_DAC-00.analog-stereo",
            "node.description": "Some USB DAC Analog Stereo",
            "device.profile.description": "Analog Stereo",
            "device.icon_name": "audio-card-analog",
            "device.bus": "usb",
            "device.api": "alsa",
            "device.id": 7,
            "card.profile.device": 0,
        }}},
    ]

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout=json.dumps(dump), stderr="")

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    sinks = d._enumerate_audio_sinks()
    assert sinks[0]["profile"] == "Analog Stereo"
    assert sinks[0]["route"] == ""


@pytest.mark.parametrize("exc", [
    FileNotFoundError("pw-dump"),
    subprocess.TimeoutExpired("pw-dump", 5),
])
def test_enumerate_subprocess_errors_return_empty(monkeypatch, exc):
    def fake_run(*a, **k):
        raise exc
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d._enumerate_audio_sinks() == []


def test_enumerate_bad_json_returns_empty(monkeypatch):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="not json", stderr="")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d._enumerate_audio_sinks() == []


def test_enumerate_non_list_json_returns_empty(monkeypatch):
    """Valid JSON that isn't an array (e.g. an error object) must not crash."""
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout='{"error": "oops"}', stderr="")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d._enumerate_audio_sinks() == []


# --- _prompt_pick_sink guards -----------------------------------------------

def test_prompt_pick_skips_when_stdin_not_tty(monkeypatch):
    _set_tty(monkeypatch, stdin=False, stdout=True)
    # input() must never be called when stdin isn't a TTY.
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted on non-TTY stdin"))
    assert d._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


def test_prompt_pick_skips_when_stdout_piped(monkeypatch):
    # Piping stdout (`--autoload | cat`) leaves stdin a TTY but must NOT prompt,
    # or the program blocks on a prompt the user may not see.
    _set_tty(monkeypatch, stdin=True, stdout=False)
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted with stdout piped"))
    assert d._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


@pytest.mark.parametrize("answer,expected_idx", [("1", 0), ("2", 1)])
def test_prompt_pick_valid(monkeypatch, answer, expected_idx):
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: answer)
    cands = [IDEAPAD_ANALOG, USB_DAC_ANALOG]
    assert d._prompt_pick_sink(cands) is cands[expected_idx]


@pytest.mark.parametrize("answer", ["", "abc", "0", "3", "-1"])
def test_prompt_pick_invalid_or_empty_skips(monkeypatch, answer):
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: answer)
    assert d._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


def test_prompt_pick_eof_skips(monkeypatch):
    _set_tty(monkeypatch)
    def raise_eof(*a):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert d._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


# --- _resolve_autoload_sinks ------------------------------------------------

def test_resolve_override_short_circuits_detection(monkeypatch):
    """--autoload-sink resolves via pw-dump lookup, never via select_*()."""
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, HDMI_SINK])
    monkeypatch.setattr(d, "select_speaker_sinks",
                        lambda: pytest.fail("detection consulted despite override"))
    out = d._resolve_autoload_sinks([IDEAPAD_ANALOG["name"]], dry_run=True)
    assert [s["name"] for s in out] == [IDEAPAD_ANALOG["name"]]
    assert out[0]["profile"] == "Analog Stereo"  # recovered from pw-dump


def test_resolve_override_unknown_name_empty_profile(monkeypatch):
    _patch_sinks(monkeypatch, [])  # name not present in pw-dump
    out = d._resolve_autoload_sinks(["alsa_output.made.up"], dry_run=True)
    assert len(out) == 1
    assert out[0]["name"] == "alsa_output.made.up"
    assert out[0]["profile"] == ""
    assert out[0]["description"] == "alsa_output.made.up"


def test_resolve_strict(monkeypatch):
    _patch_sinks(monkeypatch, [STRICT_SPEAKER, HDMI_SINK])
    out = d._resolve_autoload_sinks([], dry_run=True)
    assert [s["name"] for s in out] == [STRICT_SPEAKER["name"]]


def test_resolve_relaxed_single_auto_applies(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, HDMI_SINK])
    out = d._resolve_autoload_sinks([], dry_run=True)
    assert [s["name"] for s in out] == [IDEAPAD_ANALOG["name"]]


def test_resolve_relaxed_ambiguous_dry_run_never_prompts(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, USB_DAC_ANALOG])
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted under --dry-run"))
    out = d._resolve_autoload_sinks([], dry_run=True)
    assert out == []  # ambiguous + can't prompt → skip


def test_resolve_relaxed_ambiguous_tty_uses_pick(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, USB_DAC_ANALOG])
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: "2")
    out = d._resolve_autoload_sinks([], dry_run=False)
    # selected is sorted (pci first), so index 2 is the USB DAC.
    assert [s["name"] for s in out] == [USB_DAC_ANALOG["name"]]


def test_resolve_none_returns_empty(monkeypatch):
    _patch_sinks(monkeypatch, [HDMI_SINK, BLUEZ_SINK])
    assert d._resolve_autoload_sinks([], dry_run=True) == []


# --- ee_to_pipewire._autodetect_speaker_sink --------------------------------

def test_ee_autodetect_strict_single(monkeypatch):
    _patch_sinks(monkeypatch, [STRICT_SPEAKER, HDMI_SINK])
    name, warnings = pw._autodetect_speaker_sink()
    assert name == STRICT_SPEAKER["name"]
    assert warnings == []


def test_ee_autodetect_relaxed_single_warns(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, HDMI_SINK])
    name, warnings = pw._autodetect_speaker_sink()
    assert name == IDEAPAD_ANALOG["name"]
    assert warnings  # relaxed fallback returns a name AND a warning
    assert "audio-speakers" in warnings[0]


def test_ee_autodetect_ambiguous_none(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, USB_DAC_ANALOG])
    name, warnings = pw._autodetect_speaker_sink()
    assert name is None
    assert warnings


def test_ee_autodetect_strict_multiple_none(monkeypatch):
    other = dict(STRICT_SPEAKER, name="alsa_output.pci-0000_aa.analog-stereo")
    _patch_sinks(monkeypatch, [STRICT_SPEAKER, other])
    name, warnings = pw._autodetect_speaker_sink()
    assert name is None
    assert warnings


def test_ee_autodetect_none(monkeypatch):
    _patch_sinks(monkeypatch, [HDMI_SINK])
    name, warnings = pw._autodetect_speaker_sink()
    assert name is None
    assert warnings
