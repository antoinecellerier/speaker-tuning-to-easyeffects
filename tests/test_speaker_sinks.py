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
from lib.pipewire import install as pw
from lib import console
from lib.data import speaker_pin_quirks
from lib.hardware import amps, codecs, speakers
# Aliased: several tests bind a local named `sinks` for a synthetic sink
# list, which would shadow the module.
from lib.hardware import sinks as hw_sinks
from lib.report import environment
# Aliased like the generator's own import, and for the same reason: one
# letter apart from lib.hardware.speakers above.
from lib.report import speaker as report_speaker


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
    """Make both converters' detection see `sinks` (single pw-dump boundary)."""
    monkeypatch.setattr(hw_sinks, "_enumerate_audio_sinks", lambda: list(sinks))


def _set_tty(monkeypatch, *, stdin=True, stdout=True):
    """Force stdin/stdout isatty() — a prompt requires both to be TTYs."""
    monkeypatch.setattr(hw_sinks.sys.stdin, "isatty", lambda: stdin)
    monkeypatch.setattr(hw_sinks.sys.stdout, "isatty", lambda: stdout)


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
    assert hw_sinks._classify_sink(sink) == expected


def test_classify_excludes_virtual_sink():
    """Non-alsa_output nodes (virtual / our own chain) are never relaxed."""
    virtual = {"name": "effect_input.dolby", "icon_name": "", "bus": "", "api": ""}
    assert hw_sinks._classify_sink(virtual) == "excluded"


# --- select_speaker_sinks tiers ---------------------------------------------

def test_strict_match_wins(monkeypatch):
    """A correctly-tagged speaker takes the strict tier even alongside analog."""
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, STRICT_SPEAKER])
    sel = hw_sinks.select_speaker_sinks()
    assert sel["tier"] == "strict"
    assert [s["name"] for s in sel["selected"]] == [STRICT_SPEAKER["name"]]
    # selected carries the full enumerated dict (autoload reads name/desc/profile).
    assert sel["selected"][0]["profile"] == STRICT_SPEAKER["profile"]


def test_relaxed_single_ideapad(monkeypatch):
    """No strict tag → the lone internal analog sink is the relaxed pick."""
    _patch_sinks(monkeypatch, [HDMI_SINK, IDEAPAD_ANALOG, BLUEZ_SINK])
    sel = hw_sinks.select_speaker_sinks()
    assert sel["tier"] == "relaxed"
    assert [s["name"] for s in sel["selected"]] == [IDEAPAD_ANALOG["name"]]


def test_exclusions_yield_none(monkeypatch):
    """HDMI / iec958 / headset / bluez only → no candidate at all."""
    _patch_sinks(monkeypatch, [HDMI_SINK, IEC958_SINK, USB_HEADSET, BLUEZ_SINK])
    sel = hw_sinks.select_speaker_sinks()
    assert sel["tier"] == "none"
    assert sel["selected"] == []
    # all_sinks is preserved for diagnostics.
    assert len(sel["all_sinks"]) == 4


def test_relaxed_ambiguous_sorted_pci_first(monkeypatch):
    """Two internal analog sinks → relaxed tier, pci preferred over usb."""
    _patch_sinks(monkeypatch, [USB_DAC_ANALOG, IDEAPAD_ANALOG])
    sel = hw_sinks.select_speaker_sinks()
    assert sel["tier"] == "relaxed"
    names = [s["name"] for s in sel["selected"]]
    assert names == [IDEAPAD_ANALOG["name"], USB_DAC_ANALOG["name"]]


def test_empty_yields_none(monkeypatch):
    _patch_sinks(monkeypatch, [])
    sel = hw_sinks.select_speaker_sinks()
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

    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    sinks = hw_sinks._enumerate_audio_sinks()
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

    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    sinks = hw_sinks._enumerate_audio_sinks()
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

    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    sinks = hw_sinks._enumerate_audio_sinks()
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

    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    sinks = hw_sinks._enumerate_audio_sinks()
    assert sinks[0]["profile"] == "Analog Stereo"
    assert sinks[0]["route"] == ""


@pytest.mark.parametrize("exc", [
    FileNotFoundError("pw-dump"),
    subprocess.TimeoutExpired("pw-dump", 5),
])
def test_enumerate_subprocess_errors_return_empty(monkeypatch, exc):
    def fake_run(*a, **k):
        raise exc
    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    assert hw_sinks._enumerate_audio_sinks() == []


def test_enumerate_bad_json_returns_empty(monkeypatch):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout="not json", stderr="")
    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    assert hw_sinks._enumerate_audio_sinks() == []


def test_enumerate_non_list_json_returns_empty(monkeypatch):
    """Valid JSON that isn't an array (e.g. an error object) must not crash."""
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 0, stdout='{"error": "oops"}', stderr="")
    monkeypatch.setattr(hw_sinks.subprocess, "run", fake_run)
    assert hw_sinks._enumerate_audio_sinks() == []


# --- _prompt_pick_sink guards -----------------------------------------------

def test_prompt_pick_skips_when_stdin_not_tty(monkeypatch):
    _set_tty(monkeypatch, stdin=False, stdout=True)
    # input() must never be called when stdin isn't a TTY.
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted on non-TTY stdin"))
    assert hw_sinks._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


def test_prompt_pick_skips_when_stdout_piped(monkeypatch):
    # Piping stdout (`--autoload | cat`) leaves stdin a TTY but must NOT prompt,
    # or the program blocks on a prompt the user may not see.
    _set_tty(monkeypatch, stdin=True, stdout=False)
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted with stdout piped"))
    assert hw_sinks._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


@pytest.mark.parametrize("answer,expected_idx", [("1", 0), ("2", 1)])
def test_prompt_pick_valid(monkeypatch, answer, expected_idx):
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: answer)
    cands = [IDEAPAD_ANALOG, USB_DAC_ANALOG]
    assert hw_sinks._prompt_pick_sink(cands) is cands[expected_idx]


@pytest.mark.parametrize("answer", ["", "abc", "0", "3", "-1"])
def test_prompt_pick_invalid_or_empty_skips(monkeypatch, answer):
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: answer)
    assert hw_sinks._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


def test_prompt_pick_eof_skips(monkeypatch):
    _set_tty(monkeypatch)
    def raise_eof(*a):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert hw_sinks._prompt_pick_sink([IDEAPAD_ANALOG, USB_DAC_ANALOG]) is None


# --- _resolve_autoload_sinks ------------------------------------------------

def test_resolve_override_short_circuits_detection(monkeypatch):
    """--autoload-sink resolves via pw-dump lookup, never via select_*()."""
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, HDMI_SINK])
    monkeypatch.setattr(hw_sinks, "select_speaker_sinks",
                        lambda: pytest.fail("detection consulted despite override"))
    out = hw_sinks._resolve_autoload_sinks([IDEAPAD_ANALOG["name"]], dry_run=True)
    assert [s["name"] for s in out] == [IDEAPAD_ANALOG["name"]]
    assert out[0]["profile"] == "Analog Stereo"  # recovered from pw-dump


def test_resolve_override_unknown_name_empty_profile(monkeypatch):
    _patch_sinks(monkeypatch, [])  # name not present in pw-dump
    out = hw_sinks._resolve_autoload_sinks(["alsa_output.made.up"], dry_run=True)
    assert len(out) == 1
    assert out[0]["name"] == "alsa_output.made.up"
    assert out[0]["profile"] == ""
    assert out[0]["description"] == "alsa_output.made.up"


def test_resolve_strict(monkeypatch):
    _patch_sinks(monkeypatch, [STRICT_SPEAKER, HDMI_SINK])
    out = hw_sinks._resolve_autoload_sinks([], dry_run=True)
    assert [s["name"] for s in out] == [STRICT_SPEAKER["name"]]


def test_resolve_relaxed_single_auto_applies(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, HDMI_SINK])
    out = hw_sinks._resolve_autoload_sinks([], dry_run=True)
    assert [s["name"] for s in out] == [IDEAPAD_ANALOG["name"]]


def test_resolve_relaxed_ambiguous_dry_run_never_prompts(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, USB_DAC_ANALOG])
    monkeypatch.setattr(builtins, "input",
                        lambda *a: pytest.fail("prompted under --dry-run"))
    out = hw_sinks._resolve_autoload_sinks([], dry_run=True)
    assert out == []  # ambiguous + can't prompt → skip


def test_resolve_relaxed_ambiguous_tty_uses_pick(monkeypatch):
    _patch_sinks(monkeypatch, [IDEAPAD_ANALOG, USB_DAC_ANALOG])
    _set_tty(monkeypatch)
    monkeypatch.setattr(builtins, "input", lambda *a: "2")
    out = hw_sinks._resolve_autoload_sinks([], dry_run=False)
    # selected is sorted (pci first), so index 2 is the USB DAC.
    assert [s["name"] for s in out] == [USB_DAC_ANALOG["name"]]


def test_resolve_none_returns_empty(monkeypatch):
    _patch_sinks(monkeypatch, [HDMI_SINK, BLUEZ_SINK])
    assert hw_sinks._resolve_autoload_sinks([], dry_run=True) == []


# --- lib.pipewire.install._autodetect_speaker_sink --------------------------

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


# --- Smart-amp firmware-load gate (issue #17) -------------------------------
#
# Some laptops (e.g. Yoga Pro 9i, TI TAS2563/2781 smart amps) leave their
# woofers muted until an ALSA control is switched on. The parser turns
# ``amixer -c N contents`` text into gate records; the detector wraps it with
# the card scan; the warning is what the user actually sees.

# A realistic `amixer -c N contents` excerpt with the gate among other
# controls. The gate is iface=CARD (as modern tas2781 kernels expose it —
# issue #39) while its neighbours stay iface=MIXER.
SAMPLE_AMIXER_CONTENTS = """\
numid=1,iface=MIXER,name='Master Playback Volume'
  ; type=INTEGER,access=rw---R--,values=1,min=0,max=87,step=0
  : values=87
numid=3,iface=CARD,name='Speaker Force Firmware Load'
  ; type=BOOLEAN,access=rw------,values=1
  : values=off
numid=4,iface=MIXER,name='Headphone Playback Switch'
  ; type=BOOLEAN,access=rw------,values=2
  : values=on,on
"""


def test_parse_firmware_gate_among_other_controls():
    assert speakers.parse_firmware_gate_controls(SAMPLE_AMIXER_CONTENTS) == [
        ("3", "CARD", "Speaker Force Firmware Load", False)
    ]


@pytest.mark.parametrize("value,expected_on", [
    ("off", False), ("on", True), ("0", False), ("1", True),
])
def test_parse_firmware_gate_value(value, expected_on):
    # iface=MIXER here on purpose: older kernels exposed the gate that way,
    # and the parser must carry whichever iface it saw into the fix command.
    text = (
        "numid=3,iface=MIXER,name='Speaker Force Firmware Load'\n"
        "  ; type=BOOLEAN,access=rw------,values=1\n"
        f"  : values={value}\n"
    )
    assert speakers.parse_firmware_gate_controls(text) == [
        ("3", "MIXER", "Speaker Force Firmware Load", expected_on)
    ]


@pytest.mark.parametrize("text", [
    "",
    "garbage\nlines with no controls\n",
    # A real control block, but not a firmware gate.
    "numid=5,iface=MIXER,name='Master Playback Volume'\n  : values=50\n",
])
def test_parse_firmware_gate_absent_or_malformed(text):
    assert speakers.parse_firmware_gate_controls(text) == []


def test_detect_firmware_gates_no_amixer(monkeypatch):
    """A missing `amixer` binary must yield [] rather than raising."""
    def fake_run(*a, **k):
        raise FileNotFoundError("amixer")
    monkeypatch.setattr(speakers.subprocess, "run", fake_run)
    assert speakers.detect_speaker_firmware_gates() == []


@pytest.mark.parametrize("value,expected_on", [
    ("off", False), ("0", False), ("on", True), ("1", True),
])
def test_detect_firmware_gates_demo_env(monkeypatch, value, expected_on):
    """DEMO_FIRMWARE_GATE injects a synthetic gate (state = the value)."""
    monkeypatch.setenv("DEMO_FIRMWARE_GATE", value)
    gates = speakers.detect_speaker_firmware_gates()
    assert len(gates) == 1 and gates[0].on is expected_on


@pytest.mark.parametrize("hda,soundwire", [
    ([], []),                                             # CI: no sound card
    ([], [("025D", "1318")]),                             # a SoundWire laptop
    ([("10EC0287", "17AA22E6", "Realtek ALC287")], []),   # an HDA laptop
])
def test_demo_speaker_pin_reaches_the_warning(monkeypatch, hda, soundwire):
    """DEMO_SPEAKER_PIN stands in for an affected machine, the way
    DEMO_FIRMWARE_GATE stands in for a TI amp. Without it this message is
    unreachable on any machine but the handful upstream has fixed, so no
    preview or review round ever reads it.

    Parametrised by the *host's* hardware, which the demo must not depend on:
    injecting it inside the HDA branch made it a no-op wherever the real
    machine wasn't HDA — a SoundWire laptop, or CI, which has no /proc/asound
    to make bus_type "hda" at all. That version passed here and failed on CI.
    """
    monkeypatch.setattr(codecs, "get_hda_codec_ids", lambda: hda)
    monkeypatch.setattr(codecs, "get_soundwire_ids", lambda: soundwire)
    monkeypatch.setenv("DEMO_SPEAKER_PIN", "17aa386a")   # case-insensitive
    info = report_speaker._gather_speaker_pins()
    found = speakers.find_hidden_speaker_pin(info)
    assert found and found[2] == ["0x17"]
    assert [p.node for p in info.unconfigured_pins] == ["0x17", "0x1b", "0x1e"]

    # Unset, nothing is fabricated: the host's own codec list stands. Compared
    # against the stub rather than against the demo tuple, so the check can't
    # come out true by accident on a host that has no codecs to begin with.
    monkeypatch.delenv("DEMO_SPEAKER_PIN")
    assert report_speaker._gather_speaker_pins().hda_codecs == hda


def test_demo_speaker_pin_reaches_the_speaker_report(monkeypatch):
    """--speaker-info and --doctor gather through the other function, whose
    bus-type branch skipped the demo in exactly the same way."""
    monkeypatch.setattr(codecs, "get_hda_codec_ids", lambda: [])       # as on CI
    monkeypatch.setattr(codecs, "get_soundwire_ids", lambda: [])
    monkeypatch.setattr(speakers, "detect_speaker_firmware_gates", list)  # no amixer
    monkeypatch.setattr(amps, "_gather_amp_evidence", lambda info: None)
    monkeypatch.setenv("DEMO_SPEAKER_PIN", "17AA386A")
    info = report_speaker._gather_speaker_info()
    assert [p.node for p in info.unconfigured_pins] == ["0x17", "0x1b", "0x1e"]
    assert speakers.find_hidden_speaker_pin(info)


def _gate(on):
    return speakers.FirmwareGate(
        card_index="0", card_id="sofhdadsp", numid="3", iface="CARD",
        name="Speaker Force Firmware Load", on=on,
    )


def test_warn_firmware_gate_off_prints_fix(silence_console, capsys):
    silence_console(console)
    finding = report_speaker.warn_speaker_firmware_gate([_gate(on=False)])
    out = capsys.readouterr().out
    # iface= must be spelled out — bare name= means iface=MIXER to amixer and
    # fails on the iface=CARD gates modern kernels expose (#39 regression).
    # Double-quoted identifier with inner name quotes: verified against a
    # real iface=CARD control; also survives comma-containing names.
    assert ("amixer -c sofhdadsp cset "
            "\"iface=CARD,name='Speaker Force Firmware Load'\" on") in out
    assert "sudo alsactl store" in out  # one-liner persistence
    # self-check commands: control state, kernel log, firmware blob presence
    assert "cget \"iface=CARD,name='Speaker Force Firmware Load'\"" in out
    assert "journalctl -k" in out
    # unsuffixed glob: SteamOS ships the blobs as TAS2XXX….bin.zst (#39)
    assert "ls -l /lib/firmware/TAS2*" in out
    assert "/lib/firmware/TAS2*.bin" not in out
    assert "TAS2" in out          # names the amp / firmware blob
    # The "did that fix it?" ask used to be two dim lines here, kept quiet so
    # it wouldn't rival the closing CTA. It rides to that block now instead.
    assert finding is not None and finding.kind == "ask"
    assert "#17" in finding.ask
    assert "#17" not in out


@pytest.mark.parametrize("gates", [[], [_gate(on=True)]])
def test_warn_firmware_gate_silent_when_not_off(silence_console, capsys,
                                                gates):
    silence_console(console)
    assert report_speaker.warn_speaker_firmware_gate(gates) is None
    assert capsys.readouterr().out == ""


# --- Old-kernel end-of-run hint (issue #33) ----------------------------------
#
# 6.12 only gets older as wall-clock time passes, so the "old" case is stable;
# the silent case uses a far-future series (assumed recent by design), never a
# real recent one that would age past the cutoff and rot the test.

def test_warn_old_kernel_prints_hint(silence_console, capsys):
    silence_console(console)
    environment.warn_old_kernel("6.12.74+deb13+1-amd64")
    out = capsys.readouterr().out
    assert "6.12" in out
    assert "2024-11" in out                    # names the release month
    # The explanation is the one --doctor gives, verbatim (modulo line wrapping)
    # rather than a second hand-maintained copy that can drift from it. It
    # carries the confirm-symptom and the remedy with the acronym spelt out.
    assert " ".join(environment.kernel_old_message().split()) in " ".join(out.split())
    assert "EasyEffects off" in environment.kernel_old_message()
    assert "hardware-enablement/HWE" in environment.kernel_old_message()


@pytest.mark.parametrize("release", ["99.0.0-future", "not-a-kernel"])
def test_warn_old_kernel_silent_when_recent_or_unparseable(silence_console,
                                                           capsys, release):
    silence_console(console)
    environment.warn_old_kernel(release)
    assert capsys.readouterr().out == ""


# --- Amp channel count: probe, don't assume (issue #27) ---------------------
#
# Six mono cs35l56 SoundWire amps were reported as "12 speakers" because each
# enumerated amp defaulted to stereo (×2). The count now sums a *probed* per-amp
# channel count; each SoundWire slave is one amp, default 1.

def test_layout_summary_soundwire_amps_not_doubled():
    info = speakers.SpeakerInfo()
    info.speakers = [speakers.SpeakerPin(f"sdw:{i}", "cs35l56", "amplifier", channels=1)
                     for i in range(6)]
    assert info.layout_summary == "6 speakers → multi-way: 6x amplifier"


def test_layout_summary_hda_stereo_pin_unchanged():
    info = speakers.SpeakerInfo()
    info.speakers = [speakers.SpeakerPin("0x17", "Speaker", "tweeter", channels=2)]
    assert info.layout_summary == "2 speakers → full-range stereo"


def test_layout_summary_multiway_sums_channels_by_role():
    info = speakers.SpeakerInfo()
    info.speakers = [
        speakers.SpeakerPin("0x17", "Speaker", "tweeter", channels=2),
        speakers.SpeakerPin("0x1d", "Bass Speaker", "woofer", channels=2),
    ]
    assert info.layout_summary == "4 speakers → multi-way: 2x tweeter + 2x woofer"


def test_amp_channels_from_sysfs(tmp_path):
    dev = tmp_path / "sdw:0:1:01fa:3557:01:0"
    sink = dev / "dp1_sink"
    sink.mkdir(parents=True)
    (sink / "max_ch").write_text("1\n")
    assert speakers._amp_channels_from_sysfs(dev) == 1
    assert speakers._amp_channels_from_sysfs(tmp_path / "missing") is None  # no DisCo props


def test_read_sysfs_int_tolerates_bad_bytes(tmp_path):
    p = tmp_path / "max_ch"
    p.write_bytes(b"\xff\xfe")  # non-UTF-8 sysfs blob → None, not a traceback
    assert speakers._read_sysfs_int(p) is None


# --- Smart-amp firmware/log evidence: bus-agnostic, driver-keyed (issue #27) -

@pytest.mark.parametrize("driver,has_globs,kw", [
    ("cs35l56", True, "cs35l"),
    ("snd_soc_cs35l41", True, "cs35l"),   # Cirrus over HDA, not just SoundWire
    ("snd_soc_tas2781", True, "tas2"),    # TI smart amp (issue #17 family)
    ("max98373", False, "max98"),         # Maxim DSM — no separate fw blob
    ("rt1318", False, "rt13"),            # Realtek SoundWire — no separate fw blob
])
def test_amp_firmware_profile_known(driver, has_globs, kw):
    globs, keywords = amps._amp_firmware_profile(driver)
    assert bool(globs) is has_globs
    assert kw in keywords


@pytest.mark.parametrize("driver", [
    "snd_hda_codec_realtek",
    "snd_soc_max98090",   # Maxim jack CODEC, not a smart amp
    "snd_soc_max98357a",  # dumb I2S Class-D amp, no DSP firmware
])
def test_amp_firmware_profile_unknown(driver):
    # 'max98' must not be a bare substring match (it would catch these).
    assert amps._amp_firmware_profile(driver) is None


def test_amp_families_failure_markers():
    # Markers are co-located per family; every blob-loading family carries a
    # source-verified tell, and Maxim deliberately carries none (its missing DSM
    # param is silent/non-fatal — we must not invent a marker for it).
    markers = {fam[0][0]: fam[3] for fam in amps._AMP_FAMILIES}
    assert markers["cs35l"] and markers["tas2"] and markers["rt13"]
    assert markers["max98373"] == ""
    # The compiled union must be exactly the non-empty family markers, OR-joined.
    assert amps._AMP_LOG_ERROR_RE.pattern == "|".join(
        m for m in markers.values() if m)


@pytest.mark.parametrize("line,is_error", [
    # cs35l56/57 failure markers — verified verbatim against the driver source
    # AND printed by the real #27 device (Galaxy Book6 Ultra, missing machine fw).
    # FIRMWARE_MISSING & friends are the actual #27 tell: our first marker set
    # (boot/init timeouts only) reported "no errors" on this exact failure.
    ("cs35l56 sdw:0:1: Firmware boot timed out(3): HALO_STATE=0x2", True),
    ("cs35l56 sdw:0:3: init_completion timed out (SDW)", True),
    ("cs35l56 sdw:0:2:01fa:3557:01:5: FIRMWARE_MISSING", True),
    ("cs35l56 sdw:0:1:01fa:3557:01:2: Calibration disabled due to missing firmware controls", True),
    ("cs35l56 sdw:0:1:01fa:3557:01:2: Can't read tuning IDs", True),
    # Other covered families — markers from tas2781-* and rt1320-sdw.c.
    ("tas2781 i2c-TXNW2781:00: FW download failed = -2", True),
    ("tas2781 i2c-TXNW2781:00: Request firmware tas2781_RCA1.bin failed", True),
    ("rt1320 sdw:0:0:025d:1320:00: Failed to load rt1320 firmware", True),
    # benign / nuanced lines are NOT flagged — shown verbatim, never a verdict.
    # patched=0 in particular is not a failure marker (and was never a success one);
    # the DSP1/Firmware:/regulator lines are normal bring-up chatter from #27.
    ("cs35l56: Cirrus Logic CS35L57 Rev B2 OTP1 fw:4.2.1 (patched=0)", False),
    ("cs35l56 sdw:0:1: DSP1: Firmware: 1a00d6 vendor: 0x2 v4.2.1, 42 algorithms", False),
    ("cs35l56 sdw:0:1: DSP1: cirrus/cs35l56-b0-dsp1-misc-aabb.wmfw", False),
    ("cs35l56 sdw:0:1: supply VDD_A not found, using dummy regulator", False),
    # The generic loader's "Direct firmware load … failed" is not a driver tell;
    # we key only on the driver's own prints, so it stays unflagged.
    ("Direct firmware load for cirrus/cs35l56-x.wmfw failed", False),
    # The doc's ".bin file required but not found" is prose the driver never
    # prints — must NOT be treated as a marker (it would be dead code).
    ("cs35l56 sdw:0:1: .bin file required but not found", False),
    ("some unrelated kernel message", False),
])
def test_amp_log_is_error(line, is_error):
    assert amps._amp_log_is_error(line) is is_error


def test_scan_amp_log_filters_and_flags_errors():
    log = ("kernel: cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw\n"
           "kernel: random unrelated line\n"
           "kernel: cs35l56 sdw:0:1: Firmware boot timed out(3): HALO_STATE=0x2\n")
    assert amps.scan_amp_log(log, ["cs35l", "cirrus"]) == [
        (False, "kernel: cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw"),
        (True, "kernel: cs35l56 sdw:0:1: Firmware boot timed out(3): HALO_STATE=0x2"),
    ]
    assert amps.scan_amp_log(log, []) == []


def test_list_firmware_files(tmp_path):
    (tmp_path / "cirrus").mkdir()
    (tmp_path / "cirrus" / "cs35l56-b0-dsp1-misc-aabb-amp1.bin").write_text("x")
    (tmp_path / "cirrus" / "other.bin").write_text("x")
    found = amps._list_firmware_files(["cirrus/cs35l*"], roots=[tmp_path])
    assert found == ["cirrus/cs35l56-b0-dsp1-misc-aabb-amp1.bin"]


# --- Merged "Speaker amplifier status" section: terse, expand on problems ----

def _astat(node, driver="cs35l56", bound=True, channels=1):
    return speakers.AmpStatus(node=node, driver=driver, bound=bound, channels=channels)


def test_amp_status_lines_healthy_is_terse():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat(f"sdw:{i}") for i in range(6)]
    info.amp_firmware = ["cirrus/cs35l56-amp1.bin"]
    info.amp_log = [(False, "DSP1: cirrus/cs35l56.wmfw")]
    lines = report_speaker._amp_status_lines(info)
    assert lines[0] == "  6 amplifier(s) bound (cs35l56); 1ch"
    assert not any("⚠" in l for l in lines)


def test_amp_status_lines_unbound_is_neutral():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0", bound=False, channels=0), _astat("sdw:1")]
    lines = report_speaker._amp_status_lines(info)
    assert any("no driver bound" in l and "sdw:0" in l for l in lines)
    assert not any("⚠" in l for l in lines)  # neutral — not a "silent speaker" alarm


def test_amp_status_lines_includes_firmware_gate_off():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.firmware_gates = [speakers.FirmwareGate("0", "sofhdadsp", "3", "CARD",
                                          "Speaker Force Firmware Load", on=False)]
    lines = report_speaker._amp_status_lines(info)
    assert any("Force Firmware Load" in l and "OFF" in l for l in lines)
    # This section is where --speaker-info and --doctor both end, so it is the
    # only place either can hand over the fix — flagging without it leaves the
    # reader with a diagnosis and no command.
    fix = [l for l in lines if "amixer" in l]
    assert fix and "iface=CARD" in fix[0] and fix[0].rstrip().endswith(" on")
    assert fix[0].strip() == f"turn it on:  {speakers.amixer_enable_cmd(info.firmware_gates[0])}"


def test_amp_status_lines_gate_on_offers_no_fix():
    info = speakers.SpeakerInfo()
    info.firmware_gates = [speakers.FirmwareGate("0", "sofhdadsp", "3", "CARD",
                                          "Speaker Force Firmware Load", on=True)]
    lines = report_speaker._amp_status_lines(info)
    assert not any("amixer" in l for l in lines)
    assert not any("⚠" in l for l in lines)


def test_amp_status_lines_flags_log_error_and_missing_firmware():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_firmware = []
    info.amp_firmware_missing = True
    info.amp_log = [(True, "Firmware boot timed out(3): HALO_STATE=0x2")]
    lines = report_speaker._amp_status_lines(info)
    assert any("amp firmware/init error" in l for l in lines)
    assert any("Firmware boot timed out" in l for l in lines)
    assert any("none found under /lib/firmware" in l for l in lines)
    # The error case must also hand over the command to read the full log.
    assert any("see full log" in l and "journalctl" in l for l in lines)


def test_amp_status_lines_log_error_truncation_is_surfaced():
    # >3 errors: show 3 and say how many were dropped (never a silent cap).
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log = [(True, f"cs35l56 sdw:0:{i}: FIRMWARE_MISSING") for i in range(6)]
    lines = report_speaker._amp_status_lines(info)
    assert sum("FIRMWARE_MISSING" in l for l in lines) == 3
    assert any("+3 more" in l for l in lines)


def test_amp_status_lines_no_ok_verdict_when_log_clean():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log = [(False, "cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw")]
    lines = report_speaker._amp_status_lines(info)
    joined = "\n".join(lines).lower()
    # Points at the raw log AND tells the reader our scan isn't authoritative.
    assert "no known failure marker" in joined
    assert "read them yourself" in joined
    # No positive health verdict in any wording — broader than one literal.
    assert not any(w in joined for w in
                   ("loaded ok", "firmware ok", "amp ok", "healthy", "all good", "✓"))


def test_amp_status_lines_grep_hint_uses_scanned_keywords():
    # The printed self-check command must match what the report actually scanned.
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0", driver="max98373")]
    info.amp_log_grep = "max98"
    info.amp_log = [(False, "max98373 ...: some line")]
    lines = report_speaker._amp_status_lines(info)
    assert any("grep -iE 'max98'" in l for l in lines)
    assert not any("cs35l|tas2|cirrus" in l for l in lines)


def test_amp_status_lines_missing_firmware_clean_log_still_points_at_log():
    # Regression: firmware missing + readable-but-empty log must not dangle the
    # "see the kernel log" reference with nothing below it.
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_firmware_missing = True
    info.amp_log = []  # readable, but no amp lines this boot
    lines = report_speaker._amp_status_lines(info)
    assert any("inspect" in l and "journalctl" in l for l in lines)


def test_amp_status_lines_log_inaccessible():
    info = speakers.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log_available = False
    assert any("not accessible" in l for l in report_speaker._amp_status_lines(info))


def test_amp_status_lines_empty():
    assert report_speaker._amp_status_lines(speakers.SpeakerInfo()) == ["  (no smart amplifier detected)"]


@pytest.mark.parametrize("mode,check", [
    ("ok", lambda L: any("6 amplifier(s) bound" in l for l in L)
                     and not any("⚠" in l for l in L)),
    ("unbound", lambda L: any("no driver bound" in l for l in L)),
    ("fail", lambda L: any("amp firmware/init error" in l for l in L)),
])
def test_demo_amp_status_env(monkeypatch, mode, check):
    monkeypatch.setenv("ATMOS_DEMO_AMP_STATUS", mode)
    info = speakers.SpeakerInfo()
    assert speakers._maybe_demo_amp_status(info) is True
    assert check(report_speaker._amp_status_lines(info))


@pytest.mark.parametrize("value", ["", "faill", "true", "1"])
def test_demo_amp_status_unknown_value_is_no_demo(monkeypatch, value):
    # An unset or typo'd value must NOT silently fake a healthy report.
    monkeypatch.setenv("ATMOS_DEMO_AMP_STATUS", value)
    info = speakers.SpeakerInfo()
    assert speakers._maybe_demo_amp_status(info) is False
    assert info.amp_status == []


# --- A woofer pin the BIOS hides (issue #53) --------------------------------
#
# On some laptops the firmware reports the pin complex driving the woofers as
# unconnected, so the kernel configures only the tweeter pin and the preset
# shapes half the speaker set. The codec dumps below are the two states of the
# development machine (ThinkPad X1 Yoga Gen 7, ALC287 17AA:22E6), whose PSREF
# entry lists "2W x2 woofers and 0.8W x2 tweeters".
#
# CODEC_TWO_PINS is captured verbatim from its /proc/asound/card0/codec#0.
# CODEC_ONE_PIN is that same dump with pin 0x17's default config rewritten to
# the unconnected value 0x411111f0 — synthesized, not captured, because no
# report we hold contains an affected machine's dump. It reproduces exactly
# what upstream commit b70f007a9fc6 describes for the issue #53 machine.
#
# Both keep pins 0x1b and 0x1e, which are output-capable with no default
# config and are *genuinely spare*. They are why the warning can't be inferred
# from the pins alone: they look identical to a hidden woofer.

def _codec_dump(ssid="0x17aa22e6", bass_pin_default="0x90170111",
                bass_control=None):
    hidden = bass_pin_default == "0x411111f0"
    # The driver builds a mixer control only for a pin it drives, so the
    # firmware-hidden woofer has none — until a fixup overrides the pin, when
    # the control appears while /proc goes on printing 0x411111f0 (issue #53).
    # Hence the override: that third state is neither of the other two.
    if bass_control is None:
        bass_control = not hidden
    bass_render = ("[N/A] Speaker at Ext Rear" if hidden
                   else "[Fixed] Speaker at Int N/A")
    bass_ctl = ('\n  Control: name="Bass Speaker Playback Switch", '
                "index=0, device=0" if bass_control else "")
    return f"""\
Codec: Realtek ALC287
Address: 0
Vendor Id: 0x10ec0287
Subsystem Id: {ssid}
Node 0x14 [Pin Complex] wcaps 0x40058d: Stereo Amp-Out
  Pincap 0x0001003c: OUT EAPD Detect
  Pin Default 0x90170110: [Fixed] Speaker at Int N/A
    Conn = Analog, Color = Unknown
  Control: name="Speaker Playback Switch", index=0, device=0
Node 0x17 [Pin Complex] wcaps 0x40058d: Stereo Amp-Out
  Pincap 0x0001003c: OUT HP Detect
  Pin Default {bass_pin_default}: {bass_render}
    Conn = Analog, Color = Unknown{bass_ctl}
Node 0x18 [Pin Complex] wcaps 0x40048b: Stereo Amp-In
  Pincap 0x00003724: IN Detect
  Pin Default 0x411111f0: [N/A] Speaker at Ext Rear
Node 0x1b [Pin Complex] wcaps 0x40058f: Stereo Amp-In Amp-Out
  Pincap 0x0001373c: IN OUT EAPD Detect
  Pin Default 0x411111f0: [N/A] Speaker at Ext Rear
Node 0x1e [Pin Complex] wcaps 0x400501: Stereo
  Pincap 0x00000010: OUT
  Pin Default 0x411111f0: [N/A] Speaker at Ext Rear
Node 0x21 [Pin Complex] wcaps 0x40058d: Stereo Amp-Out
  Pincap 0x0001001c: OUT HP EAPD Detect
  Pin Default 0x03211020: [Jack] HP Out at Ext Left
  Control: name="Headphone Playback Switch", index=0, device=0
"""


CODEC_TWO_PINS = _codec_dump()
CODEC_ONE_PIN = _codec_dump(bass_pin_default="0x411111f0")

# An HDMI codec as it appears beside the analog one in real reports. Its SSID
# must never be tested against the quirk table.
CODEC_HDMI = """\
Codec: Intel Alderlake-P HDMI
Address: 2
Vendor Id: 0x8086281c
Subsystem Id: 0x80860101
Node 0x05 [Pin Complex] wcaps 0x400781: Digital
  Pincap 0x09000094: OUT Detect HDMI DP
  Pin Default 0x18560010: [Jack] Digital Out at Int HDMI
"""


def test_parse_codec_pins_two_speakers():
    ssid, pins, unconfigured = speakers.parse_hda_codec_pins(CODEC_TWO_PINS)
    assert ssid == "17AA22E6"
    assert [(s.node, s.role, s.channels) for s in pins] == [
        ("0x14", "tweeter", 2), ("0x17", "woofer", 2)]
    assert all(s.codec == "17AA22E6" for s in pins)


def test_parse_codec_pins_hidden_woofer():
    """With 0x17 marked unconnected the woofer stops being a speaker pin and
    becomes indistinguishable from the spare ones."""
    _, pins, unconfigured = speakers.parse_hda_codec_pins(CODEC_ONE_PIN)
    assert [s.node for s in pins] == ["0x14"]
    assert "0x17" in [p.node for p in unconfigured]


def test_parse_codec_pins_reports_spare_output_pins():
    """0x1b and 0x1e are output-capable with no default config; 0x18 is an
    input pin and 0x21 is a configured jack, so neither may be listed."""
    _, _, unconfigured = speakers.parse_hda_codec_pins(CODEC_TWO_PINS)
    assert [p.node for p in unconfigured] == ["0x1b", "0x1e"]
    assert all(p.pin_default == "0x411111f0" for p in unconfigured)
    assert all(p.codec == "17AA22E6" for p in unconfigured)


def test_parse_codec_pins_hdmi_has_no_speakers():
    ssid, pins, unconfigured = speakers.parse_hda_codec_pins(CODEC_HDMI)
    assert ssid == "80860101"
    assert pins == [] and unconfigured == []


@pytest.mark.parametrize("cfg,speaker,unconnected", [
    (0x90170110, True, False),   # "[Fixed] Speaker at Int N/A"
    (0x90170121, True, False),   # what the issue #53 fixup writes for 0x17
    (0x411111f0, False, True),   # "[N/A] Speaker at Ext Rear"
    (0x03211020, False, False),  # "[Jack] HP Out at Ext Left"
    (0x18560010, False, False),  # "[Jack] Digital Out at Int HDMI"
    (0x03a11030, False, False),  # "[Jack] Mic at Ext Left"
])
def test_pin_config_fields_decode_as_the_kernel_renders_them(
        cfg, speaker, unconnected):
    """The classifier reads the raw 32-bit config now, because an override
    arrives as a number with no rendered line to match against. Each value
    here is one a real dump carries, paired with how /proc renders it."""
    assert speakers._pin_is_internal_speaker(cfg) is speaker
    assert speakers._pin_is_unconnected(cfg) is unconnected


def test_parse_pin_config_overrides():
    assert speakers.parse_pin_config_overrides(
        "0x17 0x90170121\n0x1b 0x411111f0\n") == {
            "0x17": 0x90170121, "0x1b": 0x411111f0}
    # An empty file is what a codec with no override reports, and the header
    # -less format means a malformed line can only be skipped.
    assert speakers.parse_pin_config_overrides("") == {}
    assert speakers.parse_pin_config_overrides("garbage\n0xzz 0x1\n") == {}


def test_read_pin_config_overrides_lines_up_proc_and_sysfs(tmp_path):
    """card index and codec address name both views, so nothing needs parsing
    to pair them: /proc/asound/card0/codec#0 ↔ /sys/class/sound/hwC0D0."""
    proc = tmp_path / "proc/asound/card0"
    proc.mkdir(parents=True)
    codec_path = proc / "codec#0"
    codec_path.write_text(CODEC_ONE_PIN)
    sysfs = tmp_path / "sys/class/sound"
    (sysfs / "hwC0D0").mkdir(parents=True)
    (sysfs / "hwC0D0/driver_pin_configs").write_text("0x17 0x90170121\n")

    assert speakers.read_pin_config_overrides(codec_path, sysfs) == {
        "0x17": speakers.PinOverride(0x90170121, "kernel fixup")}
    # user_pin_configs is the only one of the two gated behind
    # CONFIG_SND_HDA_RECONFIG, and it outranks the driver's where both exist.
    (sysfs / "hwC0D0/user_pin_configs").write_text("0x17 0x90170110\n")
    assert speakers.read_pin_config_overrides(codec_path, sysfs) == {
        "0x17": speakers.PinOverride(0x90170110, "manual pincfg")}
    # A codec with neither file reads as a machine with nothing overridden.
    assert speakers.read_pin_config_overrides(proc / "codec#1", sysfs) == {}


def _info(codec_dumps, cards=("0 [PCH ]: HDA-Intel - HDA Intel PCH",),
          pci=None, overrides=None):
    """A SpeakerInfo as _gather_speaker_info would build it from *codec_dumps*."""
    info = speakers.SpeakerInfo(sound_cards=list(cards), pci_subsystem=pci)
    for dump in codec_dumps:
        ssid, pins, unconfigured = speakers.parse_hda_codec_pins(dump, overrides)
        info.speakers.extend(pins)
        info.unconfigured_pins.extend(unconfigured)
        info.hda_codecs.append(("10EC0287", ssid, "Realtek ALC287"))
    return info


# 17AA:386A is the issue #53 machine: in the table, HDA_CODEC_QUIRK-keyed, and
# its quirk (b70f007a9fc6) is merged for 7.2 but in no released kernel yet.
ISSUE_53_SSID = "0x17aa386a"

# The state that used to be unreadable: the fixup is applied, so the driver
# drives 0x17 and builds its control, while /proc goes on printing the
# firmware's 0x411111f0 for it — the register a fixup never writes.
CODEC_FIXUP_APPLIED = _codec_dump(ssid=ISSUE_53_SSID,
                                  bass_pin_default="0x411111f0",
                                  bass_control=True)
FIXUP_OVERRIDE = {"0x17": speakers.PinOverride(0x90170121, "kernel fixup")}


def test_override_reveals_the_pin_proc_still_calls_unconnected():
    _, pins, unconfigured = speakers.parse_hda_codec_pins(
        CODEC_FIXUP_APPLIED, FIXUP_OVERRIDE)
    assert [(s.node, s.role, s.override) for s in pins] == [
        ("0x14", "tweeter", ""), ("0x17", "woofer", "kernel fixup")]
    assert "0x17" not in [p.node for p in unconfigured]


def test_warning_clears_once_the_fixup_is_applied():
    """The regression that mattered: reading /proc alone, a user who applied
    the modprobe fix was told to apply it again, on every run, forever — and
    step 2 of that procedure asked them to confirm something that could never
    happen."""
    assert speakers.find_hidden_speaker_pin(_info([CODEC_FIXUP_APPLIED])) is not None
    assert speakers.find_hidden_speaker_pin(
        _info([CODEC_FIXUP_APPLIED], overrides=FIXUP_OVERRIDE)) is None


def test_detect_hda_speakers_reads_the_override(tmp_path):
    """Wiring guard: the parser honouring an override is worth nothing if the
    gatherer never reads one."""
    proc = tmp_path / "proc/asound/card0"
    proc.mkdir(parents=True)
    (proc / "codec#0").write_text(CODEC_FIXUP_APPLIED)
    sysfs = tmp_path / "sys/class/sound"
    (sysfs / "hwC0D0").mkdir(parents=True)
    (sysfs / "hwC0D0/driver_pin_configs").write_text("0x17 0x90170121\n")

    info = speakers.SpeakerInfo()
    speakers._detect_hda_speakers(info, tmp_path / "proc/asound", sysfs)
    assert [(s.node, s.override) for s in info.speakers] == [
        ("0x14", ""), ("0x17", "kernel fixup")]
    assert [p.node for p in info.unconfigured_pins] == ["0x1b", "0x1e"]


def test_speaker_info_tags_an_overridden_pin(capsys):
    """Without the tag, an applied fixup and a BIOS-declared speaker render
    identically — leaving the user's verification step nothing to look at."""
    info = _info([CODEC_FIXUP_APPLIED], overrides=FIXUP_OVERRIDE)
    report_speaker._print_speaker_info(info)
    out = capsys.readouterr().out
    assert "0x17: Bass Speaker Playback Switch (woofer, stereo) [kernel fixup]" in out
    assert "0x14: Speaker Playback Switch (tweeter, stereo)\n" in out



def test_hidden_pin_detected_on_listed_machine():
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    found = speakers.find_hidden_speaker_pin(info)
    assert found is not None
    quirk, codec_ssid, missing = found
    assert codec_ssid == "17AA386A"
    assert quirk.model == "alc287-yoga9-bass-spk-pin"
    # Only the pin that is actually absent — 0x14 is configured and must not
    # be reported as something the kernel isn't driving.
    assert missing == ["0x17"]


def test_no_warning_when_both_pins_present():
    """The same listed machine, once the quirk is applied, must go silent —
    otherwise the warning would never stop firing after the user fixed it."""
    info = _info([_codec_dump(ssid=ISSUE_53_SSID)])
    assert speakers.find_hidden_speaker_pin(info) is None


def test_no_warning_for_unlisted_machine():
    """One pin on a machine upstream has no fixup for is simply a 2-driver
    laptop — the case that covers most reports we hold (#33, #36, #44, #46,
    #50, all "Stereo speakers, 2W x2" per the manufacturer)."""
    info = _info([_codec_dump(ssid="0x17aa38dc",
                              bass_pin_default="0x411111f0")])
    assert speakers.find_hidden_speaker_pin(info) is None


def test_hdmi_codec_ssid_never_matches():
    """A listed id appearing as an HDMI codec's SSID must not fire: pins are
    counted per codec, and an HDMI codec has none to be short of."""
    hdmi = CODEC_HDMI.replace("0x80860101", ISSUE_53_SSID)
    info = _info([_codec_dump(ssid="0x17aa38dc"), hdmi])
    assert speakers.find_hidden_speaker_pin(info) is None


def test_pci_keyed_quirk_matches_off_sof():
    """17AA:3801 is SND_PCI_QUIRK-keyed, so on a legacy HDA card it may match
    the PCI subsystem id even though the codec's own id differs."""
    info = _info([_codec_dump(ssid="0x17aa9999",
                              bass_pin_default="0x411111f0")],
                 pci=("17AA", "3801"))
    assert speakers.find_hidden_speaker_pin(info) is not None


def test_pci_keyed_quirk_ignored_on_sof():
    """On SOF the kernel sees a zeroed PCI subsystem id and can only match on
    the codec's, so claiming a PCI match there would be a match the kernel
    never makes (upstream commit b70f007a9fc6)."""
    info = _info([_codec_dump(ssid="0x17aa9999",
                              bass_pin_default="0x411111f0")],
                 cards=("0 [sofhdadsp ]: sof-hda-dsp - sof-hda-dsp",),
                 pci=("17AA", "3801"))
    assert speakers.find_hidden_speaker_pin(info) is None


def test_codec_keyed_quirk_never_matches_pci_id():
    """17AA:386A is HDA_CODEC_QUIRK-keyed: upstream matches it against the
    codec's subsystem id only, so a PCI-id match must not be claimed."""
    info = _info([_codec_dump(ssid="0x17aa9999",
                              bass_pin_default="0x411111f0")],
                 pci=("17AA", "386A"))
    assert speakers.find_hidden_speaker_pin(info) is None


def test_soundwire_machine_is_never_checked():
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    info.soundwire_devices = [("01FA", "3556")]  # flips bus_type to soundwire
    assert speakers.find_hidden_speaker_pin(info) is None


def test_warning_offers_no_modprobe_line_without_a_forcible_name(capsys):
    """17AA:38CF's fixup has no name in the kernel's models table, so there is
    nothing to force — printing a command that can't work would be worse than
    saying an upgrade is the only route."""
    info = _info([_codec_dump(ssid="0x17aa38cf",
                              bass_pin_default="0x411111f0")])
    assert report_speaker.warn_hidden_speaker_pin(
        speakers.find_hidden_speaker_pin(info), info) is not None
    out = capsys.readouterr().out
    assert "sudo tee" not in out and "hda_model" not in out
    assert "can't be forced by hand" in out


def test_hidden_pin_warning_copy(capsys):
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    finding = report_speaker.warn_hidden_speaker_pin(speakers.find_hidden_speaker_pin(info), info)
    out = capsys.readouterr().out
    assert finding is not None and finding.kind == "hint"
    # The fix, its verification, and its undo must all be present: a modprobe
    # line with no way back is not a safe thing to print.
    assert "alc287-yoga9-bass-spk-pin" in out
    assert "--speaker-info" in out
    assert f"rm {report_speaker._MODPROBE_CONF}" in out
    # 386A's quirk is mainline-only, so "upgrade your kernel" would be a dead
    # end and must not be what the user is told to do.
    assert "not in any released kernel yet" in out


def test_hidden_pin_warning_silent_without_match(capsys):
    assert report_speaker.warn_hidden_speaker_pin(None, speakers.SpeakerInfo()) is None
    assert capsys.readouterr().out == ""


def test_speaker_pin_doctor_check():
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    check = report_speaker.speaker_pin_status(info)
    assert check is not None and check.status == d.DOCTOR_WARN
    assert report_speaker.speaker_pin_status(_info([_codec_dump(ssid=ISSUE_53_SSID)])) is None


def test_hda_model_module_falls_back_to_legacy(tmp_path):
    """No hda_model parameter anywhere (legacy snd-hda-intel, or nothing
    loaded) must still yield a usable module/parameter pair."""
    assert speakers.hda_model_module(True, tmp_path) == ("snd_hda_intel", "model")


def test_hda_model_module_finds_whichever_driver_exposes_it(tmp_path):
    """The parameter moved between SOF modules across kernels, so it is found
    by scanning rather than by name."""
    params = tmp_path / "snd_sof_intel_hda_generic" / "parameters"
    params.mkdir(parents=True)
    (params / "hda_model").write_text("\n")
    assert speakers.hda_model_module(True, tmp_path) == (
        "snd_sof_intel_hda_generic", "hda_model")


# `since` exists so the three upgrade situations can be told apart. Getting
# this wrong sends a reader after a kernel they already have, or after one
# that doesn't carry the fix at all.

def _quirk(since, model="alc287-yoga9-bass-spk-pin"):
    return speaker_pin_quirks.PinQuirk(model, pins="0x17", since=since,
                                       codec_only=True)


def test_upgrade_prospect_not_in_any_release():
    text = report_speaker.upgrade_prospect(_quirk(""), release="7.1.5-amd64")
    assert "not in any released kernel yet" in text
    assert "upgrad" in text  # says so explicitly rather than staying silent


def test_upgrade_prospect_user_is_behind():
    text = report_speaker.upgrade_prospect(_quirk("7.2"), release="7.0.0-1009-oem")
    assert "Linux 7.2 and newer" in text
    assert "already" not in text


def test_upgrade_prospect_user_is_already_past_it():
    """The case a boolean could not express: their kernel carries the fix and
    the pin is still missing, so telling them to upgrade is telling them to go
    and get what they have."""
    text = report_speaker.upgrade_prospect(_quirk("6.15"), release="7.1.5+deb14-amd64")
    assert "should already be" in text
    assert "7.1" in text and "6.15" in text


def test_upgrade_prospect_unparseable_kernel_falls_back_to_upgrade_advice():
    text = report_speaker.upgrade_prospect(_quirk("7.2"), release="not-a-version")
    assert "Linux 7.2 and newer" in text


# The table covers fixups that declare a machine's *only* speaker pin (HP
# Spectre x360, ASUS ROG), not just a second one. Two things follow, and both
# were wrong under the first pin-counting predicate.

def test_fires_when_the_declared_pin_is_missing_but_another_speaker_exists():
    """17AA:390D declares 0x17. A machine showing only 0x14 is short of it,
    even though it has a speaker pin — so "has speakers" can't be the test."""
    info = _info([_codec_dump(ssid="0x17aa390d",
                              bass_pin_default="0x411111f0")])
    assert speakers.find_hidden_speaker_pin(info) is not None


def test_fires_when_the_codec_has_no_speaker_pins_at_all():
    """103C:8519's fixup declares 0x14 — the only speaker. Before it applies
    the codec has no speaker pins, and keying off speakers alone would skip
    exactly the machine that needs this."""
    dump = _codec_dump(ssid="0x103c8519", bass_pin_default="0x411111f0")
    dump = dump.replace("Pin Default 0x90170110: [Fixed] Speaker at Int N/A",
                        "Pin Default 0x411111f0: [N/A] Speaker at Ext Rear")
    info = _info([dump])
    assert info.speakers == []
    found = speakers.find_hidden_speaker_pin(info)
    assert found is not None and found[0].pins == "0x14"
    assert found[2] == ["0x14"]


def test_silent_once_every_declared_pin_is_present():
    """The regression the pin-counting predicate would have shipped: a fixup
    declaring one pin, on a machine that now has it, must stop warning."""
    info = _info([_codec_dump(ssid="0x17aa390d")])
    assert speakers.find_hidden_speaker_pin(info) is None


# The negative signal (issue #53): the table only knows machines upstream has
# been told about, so a hidden woofer nobody has reported yet is invisible
# here. Its owner can read the spec sheet in seconds; we can't.

def test_asks_about_speaker_count_when_nothing_matched():
    info = _info([_codec_dump(ssid="0x17aa9999",
                              bass_pin_default="0x411111f0")])
    finding = report_speaker.unlisted_speaker_pin_finding(info)
    assert finding is not None and finding.kind == "ask"
    assert "more speakers" in finding.ask
    # Names the pins, since nothing is printed above it in a normal run.
    assert "0x14" in finding.detail and "0x17" in finding.detail


def test_no_speaker_count_ask_when_the_pair_is_complete():
    """Two speaker pins and spare output pins is the development machine —
    nothing ambiguous, so nothing to ask."""
    assert report_speaker.unlisted_speaker_pin_finding(_info([_codec_dump()])) is None


def test_no_speaker_count_ask_when_a_quirk_already_matched():
    """That run has a real fix to offer; a question would compete with it."""
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    assert report_speaker.unlisted_speaker_pin_finding(info) is None


def test_no_speaker_count_ask_without_spare_output_pins():
    """No unconfigured output pin means there is nowhere for a hidden speaker
    to be, so a single pair is simply a single pair."""
    dump = _codec_dump(ssid="0x17aa9999", bass_pin_default="0x411111f0")
    info = _info([dump])
    info.unconfigured_pins.clear()
    assert report_speaker.unlisted_speaker_pin_finding(info) is None


# --- Regressions caught in review of the issue #53 work ---------------------

def test_other_codecs_cannot_borrow_the_machine_pci_id():
    """A PCI-keyed quirk describes the machine, not a codec. Lending that id to
    an HDMI codec that merely has a spare output pin produced a warning naming
    the wrong codec that no user action could ever clear."""
    analog = _codec_dump(ssid="0x17aa9999")          # both pins configured
    hdmi = CODEC_HDMI.replace(
        "Pin Default 0x18560010: [Jack] Digital Out at Int HDMI",
        "Pin Default 0x411111f0: [N/A] Speaker at Ext Rear")
    # 1028:0B37 is PCI-keyed and declares 0x14 0x17.
    info = _info([analog, hdmi], pci=("1028", "0B37"))
    assert speakers.find_hidden_speaker_pin(info) is None


def test_declared_pins_must_exist_on_the_matching_codec():
    """A codec that has neither pin the fixup names is not the codec the fixup
    is about, whatever id matched."""
    hdmi = CODEC_HDMI.replace("0x80860101", "0x10280B37")
    info = _info([hdmi])
    assert speakers.find_hidden_speaker_pin(info) is None


def test_microsoft_usb_device_does_not_read_as_a_sof_machine():
    """"microsoft" contains "sof". An unanchored substring test over the cards
    file let a plugged-in webcam silently disable the PCI-keyed arm, so the
    same machine reported differently depending on what was plugged in."""
    cards = ("0 [PCH            ]: HDA-Intel - HDA Intel PCH",
             "1 [Studio         ]: USB-Audio - Microsoft LifeCam Studio")
    assert speakers._card_uses_sof(list(cards)) is False
    info = _info([_codec_dump(ssid="0x17aa9999",
                              bass_pin_default="0x411111f0")],
                 cards=cards, pci=("17AA", "3801"))
    assert speakers.find_hidden_speaker_pin(info) is not None


@pytest.mark.parametrize("line,expected", [
    ("0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp", True),
    ("0 [PCH            ]: HDA-Intel - HDA Intel PCH", False),
    ("1 [Studio         ]: USB-Audio - Microsoft LifeCam Studio", False),
    ("2 [Headset        ]: USB-Audio - Microsoft Modern USB Headset", False),
    ("0 [Generic        ]: HDA-Intel - HD-Audio Generic", False),
])
def test_card_uses_sof_reads_the_driver_field(line, expected):
    assert speakers._card_uses_sof([line]) is expected


def test_modprobe_line_names_the_driver_that_owns_the_codec(tmp_path):
    """SOF modules are routinely loaded beside snd_hda_intel, so "whichever
    module exposes hda_model" wrote the option to a module driving nothing."""
    params = tmp_path / "snd_sof_intel_hda_generic" / "parameters"
    params.mkdir(parents=True)
    (params / "hda_model").write_text("\n")
    assert speakers.hda_model_module(False, tmp_path) == ("snd_hda_intel", "model")
    assert speakers.hda_model_module(True, tmp_path) == (
        "snd_sof_intel_hda_generic", "hda_model")


def test_warning_names_only_the_missing_pin(capsys):
    """0x14 is configured on this machine; saying the kernel isn't driving it
    would send the reader after a pin that works."""
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    report_speaker.warn_hidden_speaker_pin(speakers.find_hidden_speaker_pin(info), info)
    out = capsys.readouterr().out
    assert "pin 0x17" in out and "0x14" not in out


def test_copy_never_asserts_a_pin_count_it_cannot_know(capsys):
    """The detector fires with zero configured pins too, where "one pin
    configured ... a second one — the woofers" is false twice over."""
    dump = _codec_dump(ssid="0x103c8519", bass_pin_default="0x411111f0")
    dump = dump.replace("Pin Default 0x90170110: [Fixed] Speaker at Int N/A",
                        "Pin Default 0x411111f0: [N/A] Speaker at Ext Rear")
    info = _info([dump])
    found = speakers.find_hidden_speaker_pin(info)
    report_speaker.warn_hidden_speaker_pin(found, info)
    out = capsys.readouterr().out
    check = report_speaker.speaker_pin_status(info)
    assert "one internal speaker pin" not in out + check.detail
    assert "second one" not in out + check.detail
    assert "pin 0x14" in out and "pin 0x14" in check.detail
    # No speaker pin survives here, so there is no "rest" to shape.
    assert "shapes the rest alone" not in out


def test_doctor_carries_the_fix_only_when_there_is_one():
    """--doctor used to send every reader elsewhere for the modprobe line, and
    the 30 model-less rows have none to send them to."""
    listed = _info([_codec_dump(ssid=ISSUE_53_SSID,
                                bass_pin_default="0x411111f0")])
    check = report_speaker.speaker_pin_status(listed)
    assert any("| sudo tee /etc/modprobe.d/speaker-pin-fix.conf" in text
               for _, text in check.steps)
    # Nothing may send the reader off to another invocation for the fix.
    assert "--doctor" not in check.detail

    nameless = _info([_codec_dump(ssid="0x17aa38cf",
                                  bass_pin_default="0x411111f0")])
    nameless_check = report_speaker.speaker_pin_status(nameless)
    assert nameless_check.steps == ()
    assert "modprobe" not in nameless_check.detail


def test_doctor_and_the_end_of_run_block_print_one_procedure(capsys):
    """TRAP: the two surfaces used to hold separate copies of the fix. Same
    builder now, so a command edited on one side can't go stale on the other."""
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    report_speaker.warn_hidden_speaker_pin(speakers.find_hidden_speaker_pin(info), info)
    printed = capsys.readouterr().out
    commands = [t for style, t in report_speaker.speaker_pin_status(info).steps
                if style == "cta"]
    assert commands
    for text in commands:
        assert text in printed


def test_pin_fix_names_the_module_that_owns_the_codec():
    """Legacy HDA takes `model=`; the SOF parameter is scanned from /sys/module
    and covered by test_modprobe_line_names_the_driver_that_owns_the_codec."""
    quirk = speaker_pin_quirks._SPEAKER_PIN_QUIRKS[(0x17AA, 0x386A)]
    legacy = [t for _, t in report_speaker.speaker_pin_fix_steps(quirk, ["0x17"], False, 90)]
    assert any("options snd_hda_intel model=alc287-yoga9-bass-spk-pin" in t
               for t in legacy)


def test_pin_fix_wraps_prose_but_never_a_command():
    """A command folded by textwrap doesn't run; the terminal soft-wrapping a
    long one is fine. So width may only reflow the prose."""
    quirk = speaker_pin_quirks._SPEAKER_PIN_QUIRKS[(0x17AA, 0x386A)]
    narrow = report_speaker.speaker_pin_fix_steps(quirk, ["0x17"], True, 40)
    commands = [t for style, t in narrow if style == "cta"]
    assert any(len(t) > 40 for t in commands)          # left intact
    assert all(len(t) <= 40 for style, t in narrow if style == "dim")


def test_report_never_calls_a_flagged_pin_an_ordinary_spare(capsys):
    """TRAP: --doctor prints the fix and then, 40 lines down, this section. A
    blanket "spare pins are normal" and a flat speaker count under a warning
    that says one is missing read as the bottom line, and talk the reader out
    of the fix they were just handed."""
    info = _info([_codec_dump(ssid=ISSUE_53_SSID,
                              bass_pin_default="0x411111f0")])
    report_speaker._print_speaker_info(info)
    out = capsys.readouterr().out

    assert "0x17: pincap OUT HP Detect" in out
    flagged = next(l for l in out.splitlines() if l.strip().startswith("0x17:"))
    assert "⚠" in flagged and "kernel fix" in flagged
    assert "0x1b" in out and "⚠" not in next(
        l for l in out.splitlines() if l.strip().startswith("0x1b:"))
    assert "(spare pins are normal" not in out
    assert "the flagged pin above would add more" in out


def test_ordinary_machine_keeps_the_plain_spare_pin_note(capsys):
    """The development machine's 0x1b/0x1e really are spare, and nothing here
    may imply otherwise — most reports come from machines like this one."""
    report_speaker._print_speaker_info(_info([_codec_dump()]))
    out = capsys.readouterr().out
    assert "(spare pins are normal" in out
    assert "⚠" not in out and "flagged pin" not in out


def test_verification_step_points_at_the_surface_the_reader_is_on():
    """A --doctor reader has the hardware section on screen already; telling
    them to re-run with --speaker-info reads as a third command to type."""
    quirk = speaker_pin_quirks._SPEAKER_PIN_QUIRKS[(0x17AA, 0x386A)]
    run = " ".join(t for _, t in report_speaker.speaker_pin_fix_steps(quirk, ["0x17"], True, 90))
    doctor = " ".join(t for _, t in report_speaker.speaker_pin_fix_steps(
        quirk, ["0x17"], True, 90, speaker_info_below=True))

    assert "re-run with --speaker-info" in run
    assert "re-run with --speaker-info" not in doctor
    assert "section below" in doctor
    assert "[kernel fixup]" in run and "[kernel fixup]" in doctor
    # Only the pointer may differ — the commands are the shared part.
    assert ([t for s, t in report_speaker.speaker_pin_fix_steps(quirk, ["0x17"], True, 90)
             if s == "cta"]
            == [t for s, t in report_speaker.speaker_pin_fix_steps(
                quirk, ["0x17"], True, 90, speaker_info_below=True)
                if s == "cta"])


def test_fix_says_the_quirk_name_is_not_a_model_name():
    """`alc287-yoga9-bass-spk-pin` on a Yoga 7 reads like someone else's fix
    in a line the reader is about to run with sudo — upstream's own entry for
    this codec id is named "Lenovo Yoga 7 16IAP7"."""
    quirk = speaker_pin_quirks._SPEAKER_PIN_QUIRKS[(0x17AA, 0x386A)]
    prose = " ".join(t for s, t in report_speaker.speaker_pin_fix_steps(
        quirk, ["0x17"], True, 90) if s != "cta")
    assert "not your model" in prose and "hardware id" in prose


def test_pin_fix_is_empty_without_a_forcible_name():
    nameless = speaker_pin_quirks.PinQuirk("", pins="0x17", since="6.10",
                                           codec_only=True)
    assert report_speaker.speaker_pin_fix_steps(nameless, ["0x17"], True, 90) == ()


def test_no_ask_when_the_run_printed_no_procedure():
    """user-messages.md: a finding the user cannot act on carries no ask."""
    nameless = speaker_pin_quirks.PinQuirk("", pins="0x17", since="6.10",
                                           codec_only=True)
    assert report_speaker._hidden_pin_finding(nameless, ["0x17"]).ask == ""
    forcible = nameless._replace(model="alc287-yoga9-bass-spk-pin")
    assert report_speaker._hidden_pin_finding(forcible, ["0x17"]).ask


def test_default_run_gatherer_skips_the_amp_evidence_sweep(monkeypatch):
    """_gather_speaker_info shells out to journalctl/dmesg and globs
    /lib/firmware for a report a default run never prints."""
    def boom(*a, **k):
        raise AssertionError("default run must not gather amp evidence")

    monkeypatch.setattr(amps, "_gather_amp_evidence", boom)
    monkeypatch.setattr(speakers, "detect_speaker_firmware_gates", boom)
    info = report_speaker._gather_speaker_pins()
    assert isinstance(info, speakers.SpeakerInfo)
