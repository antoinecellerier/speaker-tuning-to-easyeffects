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


# --- Smart-amp firmware-load gate (issue #17) -------------------------------
#
# Some laptops (e.g. Yoga Pro 9i, TI TAS2563/2781 smart amps) leave their
# woofers muted until an ALSA control is switched on. The parser turns
# ``amixer -c N contents`` text into gate records; the detector wraps it with
# the card scan; the warning is what the user actually sees.

# A realistic `amixer -c N contents` excerpt with the gate among other controls.
SAMPLE_AMIXER_CONTENTS = """\
numid=1,iface=MIXER,name='Master Playback Volume'
  ; type=INTEGER,access=rw---R--,values=1,min=0,max=87,step=0
  : values=87
numid=3,iface=MIXER,name='Speaker Force Firmware Load'
  ; type=BOOLEAN,access=rw------,values=1
  : values=off
numid=4,iface=MIXER,name='Headphone Playback Switch'
  ; type=BOOLEAN,access=rw------,values=2
  : values=on,on
"""


def test_parse_firmware_gate_among_other_controls():
    assert d.parse_firmware_gate_controls(SAMPLE_AMIXER_CONTENTS) == [
        ("3", "Speaker Force Firmware Load", False)
    ]


@pytest.mark.parametrize("value,expected_on", [
    ("off", False), ("on", True), ("0", False), ("1", True),
])
def test_parse_firmware_gate_value(value, expected_on):
    text = (
        "numid=3,iface=MIXER,name='Speaker Force Firmware Load'\n"
        "  ; type=BOOLEAN,access=rw------,values=1\n"
        f"  : values={value}\n"
    )
    assert d.parse_firmware_gate_controls(text) == [
        ("3", "Speaker Force Firmware Load", expected_on)
    ]


@pytest.mark.parametrize("text", [
    "",
    "garbage\nlines with no controls\n",
    # A real control block, but not a firmware gate.
    "numid=5,iface=MIXER,name='Master Playback Volume'\n  : values=50\n",
])
def test_parse_firmware_gate_absent_or_malformed(text):
    assert d.parse_firmware_gate_controls(text) == []


def test_detect_firmware_gates_no_amixer(monkeypatch):
    """A missing `amixer` binary must yield [] rather than raising."""
    def fake_run(*a, **k):
        raise FileNotFoundError("amixer")
    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d.detect_speaker_firmware_gates() == []


@pytest.mark.parametrize("value,expected_on", [
    ("off", False), ("0", False), ("on", True), ("1", True),
])
def test_detect_firmware_gates_demo_env(monkeypatch, value, expected_on):
    """DEMO_FIRMWARE_GATE injects a synthetic gate (state = the value)."""
    monkeypatch.setenv("DEMO_FIRMWARE_GATE", value)
    gates = d.detect_speaker_firmware_gates()
    assert len(gates) == 1 and gates[0].on is expected_on


def _gate(on):
    return d.FirmwareGate(
        card_index="0", card_id="sofhdadsp", numid="3",
        name="Speaker Force Firmware Load", on=on,
    )


def test_warn_firmware_gate_off_prints_fix(monkeypatch, capsys):
    monkeypatch.setattr(d, "_CONSOLE", None)  # plain print → no rich wrapping
    d.warn_speaker_firmware_gate([_gate(on=False)])
    out = capsys.readouterr().out
    assert "amixer -c sofhdadsp cset name='Speaker Force Firmware Load' on" in out
    assert "sudo alsactl store" in out  # one-liner persistence
    # self-check commands: control state, kernel log, firmware blob presence
    assert "cget name='Speaker Force Firmware Load'" in out
    assert "journalctl -k" in out
    assert "/lib/firmware/TAS2" in out
    assert "TAS2" in out          # names the amp / firmware blob
    assert "#17" in out           # firmware-specific feedback ask (dim, not a CTA)


@pytest.mark.parametrize("gates", [[], [_gate(on=True)]])
def test_warn_firmware_gate_silent_when_not_off(monkeypatch, capsys, gates):
    monkeypatch.setattr(d, "_CONSOLE", None)
    d.warn_speaker_firmware_gate(gates)
    assert capsys.readouterr().out == ""


# --- Old-kernel end-of-run hint (issue #33) ----------------------------------
#
# 6.12 only gets older as wall-clock time passes, so the "old" case is stable;
# the silent case uses a far-future series (assumed recent by design), never a
# real recent one that would age past the cutoff and rot the test.

def test_warn_old_kernel_prints_hint(monkeypatch, capsys):
    monkeypatch.setattr(d, "_CONSOLE", None)  # plain print → no rich wrapping
    d.warn_old_kernel("6.12.74+deb13+1-amd64")
    out = capsys.readouterr().out
    assert "6.12" in out
    assert "2024-11" in out                    # names the release month
    assert "EasyEffects disabled" in out       # the confirm-symptom
    assert "hardware-enablement/HWE" in out    # the remedy, acronym spelt out


@pytest.mark.parametrize("release", ["99.0.0-future", "not-a-kernel"])
def test_warn_old_kernel_silent_when_recent_or_unparseable(monkeypatch, capsys,
                                                           release):
    monkeypatch.setattr(d, "_CONSOLE", None)
    d.warn_old_kernel(release)
    assert capsys.readouterr().out == ""


# --- Amp channel count: probe, don't assume (issue #27) ---------------------
#
# Six mono cs35l56 SoundWire amps were reported as "12 speakers" because each
# enumerated amp defaulted to stereo (×2). The count now sums a *probed* per-amp
# channel count; each SoundWire slave is one amp, default 1.

def test_layout_summary_soundwire_amps_not_doubled():
    info = d.SpeakerInfo()
    info.speakers = [d.SpeakerPin(f"sdw:{i}", "cs35l56", "amplifier", channels=1)
                     for i in range(6)]
    assert info.layout_summary == "6 speakers → multi-way: 6x amplifier"


def test_layout_summary_hda_stereo_pin_unchanged():
    info = d.SpeakerInfo()
    info.speakers = [d.SpeakerPin("0x17", "Speaker", "tweeter", channels=2)]
    assert info.layout_summary == "2 speakers → full-range stereo"


def test_layout_summary_multiway_sums_channels_by_role():
    info = d.SpeakerInfo()
    info.speakers = [
        d.SpeakerPin("0x17", "Speaker", "tweeter", channels=2),
        d.SpeakerPin("0x1d", "Bass Speaker", "woofer", channels=2),
    ]
    assert info.layout_summary == "4 speakers → multi-way: 2x tweeter + 2x woofer"


def test_amp_channels_from_sysfs(tmp_path):
    dev = tmp_path / "sdw:0:1:01fa:3557:01:0"
    sink = dev / "dp1_sink"
    sink.mkdir(parents=True)
    (sink / "max_ch").write_text("1\n")
    assert d._amp_channels_from_sysfs(dev) == 1
    assert d._amp_channels_from_sysfs(tmp_path / "missing") is None  # no DisCo props


def test_read_sysfs_int_tolerates_bad_bytes(tmp_path):
    p = tmp_path / "max_ch"
    p.write_bytes(b"\xff\xfe")  # non-UTF-8 sysfs blob → None, not a traceback
    assert d._read_sysfs_int(p) is None


# --- Smart-amp firmware/log evidence: bus-agnostic, driver-keyed (issue #27) -

@pytest.mark.parametrize("driver,has_globs,kw", [
    ("cs35l56", True, "cs35l"),
    ("snd_soc_cs35l41", True, "cs35l"),   # Cirrus over HDA, not just SoundWire
    ("snd_soc_tas2781", True, "tas2"),    # TI smart amp (issue #17 family)
    ("max98373", False, "max98"),         # Maxim DSM — no separate fw blob
    ("rt1318", False, "rt13"),            # Realtek SoundWire — no separate fw blob
])
def test_amp_firmware_profile_known(driver, has_globs, kw):
    globs, keywords = d._amp_firmware_profile(driver)
    assert bool(globs) is has_globs
    assert kw in keywords


@pytest.mark.parametrize("driver", [
    "snd_hda_codec_realtek",
    "snd_soc_max98090",   # Maxim jack CODEC, not a smart amp
    "snd_soc_max98357a",  # dumb I2S Class-D amp, no DSP firmware
])
def test_amp_firmware_profile_unknown(driver):
    # 'max98' must not be a bare substring match (it would catch these).
    assert d._amp_firmware_profile(driver) is None


def test_amp_families_failure_markers():
    # Markers are co-located per family; every blob-loading family carries a
    # source-verified tell, and Maxim deliberately carries none (its missing DSM
    # param is silent/non-fatal — we must not invent a marker for it).
    markers = {fam[0][0]: fam[3] for fam in d._AMP_FAMILIES}
    assert markers["cs35l"] and markers["tas2"] and markers["rt13"]
    assert markers["max98373"] == ""
    # The compiled union must be exactly the non-empty family markers, OR-joined.
    assert d._AMP_LOG_ERROR_RE.pattern == "|".join(
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
    assert d._amp_log_is_error(line) is is_error


def test_scan_amp_log_filters_and_flags_errors():
    log = ("kernel: cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw\n"
           "kernel: random unrelated line\n"
           "kernel: cs35l56 sdw:0:1: Firmware boot timed out(3): HALO_STATE=0x2\n")
    assert d.scan_amp_log(log, ["cs35l", "cirrus"]) == [
        (False, "kernel: cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw"),
        (True, "kernel: cs35l56 sdw:0:1: Firmware boot timed out(3): HALO_STATE=0x2"),
    ]
    assert d.scan_amp_log(log, []) == []


def test_list_firmware_files(tmp_path):
    (tmp_path / "cirrus").mkdir()
    (tmp_path / "cirrus" / "cs35l56-b0-dsp1-misc-aabb-amp1.bin").write_text("x")
    (tmp_path / "cirrus" / "other.bin").write_text("x")
    found = d._list_firmware_files(["cirrus/cs35l*"], roots=[tmp_path])
    assert found == ["cirrus/cs35l56-b0-dsp1-misc-aabb-amp1.bin"]


# --- Merged "Speaker amplifier status" section: terse, expand on problems ----

def _astat(node, driver="cs35l56", bound=True, channels=1):
    return d.AmpStatus(node=node, driver=driver, bound=bound, channels=channels)


def test_amp_status_lines_healthy_is_terse():
    info = d.SpeakerInfo()
    info.amp_status = [_astat(f"sdw:{i}") for i in range(6)]
    info.amp_firmware = ["cirrus/cs35l56-amp1.bin"]
    info.amp_log = [(False, "DSP1: cirrus/cs35l56.wmfw")]
    lines = d._amp_status_lines(info)
    assert lines[0] == "  6 amplifier(s) bound (cs35l56); 1ch"
    assert not any("⚠" in l for l in lines)


def test_amp_status_lines_unbound_is_neutral():
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0", bound=False, channels=0), _astat("sdw:1")]
    lines = d._amp_status_lines(info)
    assert any("no driver bound" in l and "sdw:0" in l for l in lines)
    assert not any("⚠" in l for l in lines)  # neutral — not a "silent speaker" alarm


def test_amp_status_lines_includes_firmware_gate_off():
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.firmware_gates = [d.FirmwareGate("0", "sofhdadsp", "3",
                                          "Speaker Force Firmware Load", on=False)]
    assert any("Force Firmware Load" in l and "OFF" in l
               for l in d._amp_status_lines(info))


def test_amp_status_lines_flags_log_error_and_missing_firmware():
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_firmware = []
    info.amp_firmware_missing = True
    info.amp_log = [(True, "Firmware boot timed out(3): HALO_STATE=0x2")]
    lines = d._amp_status_lines(info)
    assert any("amp firmware/init error" in l for l in lines)
    assert any("Firmware boot timed out" in l for l in lines)
    assert any("none found under /lib/firmware" in l for l in lines)
    # The error case must also hand over the command to read the full log.
    assert any("see full log" in l and "journalctl" in l for l in lines)


def test_amp_status_lines_log_error_truncation_is_surfaced():
    # >3 errors: show 3 and say how many were dropped (never a silent cap).
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log = [(True, f"cs35l56 sdw:0:{i}: FIRMWARE_MISSING") for i in range(6)]
    lines = d._amp_status_lines(info)
    assert sum("FIRMWARE_MISSING" in l for l in lines) == 3
    assert any("+3 more" in l for l in lines)


def test_amp_status_lines_no_ok_verdict_when_log_clean():
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log = [(False, "cs35l56 sdw:0:1: DSP1: cirrus/cs35l56.wmfw")]
    lines = d._amp_status_lines(info)
    joined = "\n".join(lines).lower()
    # Points at the raw log AND tells the reader our scan isn't authoritative.
    assert "no known failure marker" in joined
    assert "read them yourself" in joined
    # No positive health verdict in any wording — broader than one literal.
    assert not any(w in joined for w in
                   ("loaded ok", "firmware ok", "amp ok", "healthy", "all good", "✓"))


def test_amp_status_lines_grep_hint_uses_scanned_keywords():
    # The printed self-check command must match what the report actually scanned.
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0", driver="max98373")]
    info.amp_log_grep = "max98"
    info.amp_log = [(False, "max98373 ...: some line")]
    lines = d._amp_status_lines(info)
    assert any("grep -iE 'max98'" in l for l in lines)
    assert not any("cs35l|tas2|cirrus" in l for l in lines)


def test_amp_status_lines_missing_firmware_clean_log_still_points_at_log():
    # Regression: firmware missing + readable-but-empty log must not dangle the
    # "see the kernel log" reference with nothing below it.
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_firmware_missing = True
    info.amp_log = []  # readable, but no amp lines this boot
    lines = d._amp_status_lines(info)
    assert any("inspect" in l and "journalctl" in l for l in lines)


def test_amp_status_lines_log_inaccessible():
    info = d.SpeakerInfo()
    info.amp_status = [_astat("sdw:0")]
    info.amp_log_available = False
    assert any("not accessible" in l for l in d._amp_status_lines(info))


def test_amp_status_lines_empty():
    assert d._amp_status_lines(d.SpeakerInfo()) == ["  (no smart amplifier detected)"]


@pytest.mark.parametrize("mode,check", [
    ("ok", lambda L: any("6 amplifier(s) bound" in l for l in L)
                     and not any("⚠" in l for l in L)),
    ("unbound", lambda L: any("no driver bound" in l for l in L)),
    ("fail", lambda L: any("amp firmware/init error" in l for l in L)),
])
def test_demo_amp_status_env(monkeypatch, mode, check):
    monkeypatch.setenv("ATMOS_DEMO_AMP_STATUS", mode)
    info = d.SpeakerInfo()
    assert d._maybe_demo_amp_status(info) is True
    assert check(d._amp_status_lines(info))


@pytest.mark.parametrize("value", ["", "faill", "true", "1"])
def test_demo_amp_status_unknown_value_is_no_demo(monkeypatch, value):
    # An unset or typo'd value must NOT silently fake a healthy report.
    monkeypatch.setenv("ATMOS_DEMO_AMP_STATUS", value)
    info = d.SpeakerInfo()
    assert d._maybe_demo_amp_status(info) is False
    assert info.amp_status == []
