"""ee_to_pipewire.py --doctor: the checks, against synthetic graphs.

Probing and judging are separate on purpose, so every state a user can land in
is testable here without a PipeWire daemon — including the ones this developer
machine cannot produce (an old WirePlumber, a sink that vanished, a conf that
never loaded). The states themselves come from measurement: see
docs/ee-to-pipewire.md "One smart filter per target sink".
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import ee_to_pipewire
from lib import console
from lib.pipewire import checks, conf
# Bound before the autouse `no_live_easyeffects_probe` fixture patches the
# module attribute, so the probe itself stays testable.
from lib.pipewire.checks import easyeffects_running as unpatched_ee_probe
from lib.doctor import DOCTOR_FAIL, DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN
from lib.report import findings as report_findings

SPEAKER = "alsa_output.pci-0000_00_1f.3.HiFi__Speaker__sink"


def _node(name, **props):
    return {"type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name, **props}}}


def _smart_chain(name, target=SPEAKER):
    return [
        _node(f"effect_input.{name}", **{
            "media.class": "Audio/Sink",
            "node.link-group": f"{name}_smart_filter",
            "filter.smart": True,
            # The live-graph shape: pw-dump reports the property verbatim, so
            # the object the conf declares comes back as SPA-JSON *text*.
            "filter.smart.target": f'{{ node.name = "{target}" }}',
        }),
        _node(f"effect_output.{name}", **{"node.passive": True}),
    ]


def _virtual_chain(name, pinned=""):
    playback = {"node.passive": True}
    if pinned:
        playback["target.object"] = pinned
    return [
        _node(f"effect_input.{name}", **{"media.class": "Audio/Sink"}),
        _node(f"effect_output.{name}", **playback),
    ]


def _speaker_sink(name=SPEAKER):
    return _node(name, **{"media.class": "Audio/Sink"})


def _default_metadata(effective="", configured=""):
    """The pw-dump Metadata object WirePlumber keeps the default sink in.

    `default.audio.sink` is where streams go now; `default.configured.audio.sink`
    is the pick WirePlumber stores in ~/.local/state/wireplumber/default-nodes
    and re-applies whenever a node of that name exists — the state no
    sound-settings UI shows, and the one that outlives the sink it names.

    Unlike a Node, a Metadata object carries its props at the top level, which
    is the shape the reader has to cope with.
    """
    entries = []
    if effective:
        entries.append({"subject": 0, "key": "default.audio.sink",
                        "type": "Spa:String:JSON", "value": {"name": effective}})
    if configured:
        entries.append({"subject": 0, "key": "default.configured.audio.sink",
                        "type": "Spa:String:JSON", "value": {"name": configured}})
    return {"type": "PipeWire:Interface:Metadata",
            "props": {"metadata.name": "default"}, "metadata": entries}


# --- Reading the graph ------------------------------------------------------

def test_live_chains_joins_both_halves():
    dump = [*_smart_chain("Dolby_Balanced"), *_virtual_chain("Other", "sink.x")]
    chains = {c.name: c for c in checks.live_chains(dump)}
    assert chains["Dolby_Balanced"].smart is True
    assert chains["Dolby_Balanced"].target == SPEAKER
    assert chains["Other"].smart is False
    assert chains["Other"].pinned == "sink.x"


@pytest.mark.parametrize("raw,expected", [
    ({"node.name": SPEAKER}, SPEAKER),                      # spa-json-dump
    (f'{{ node.name = "{SPEAKER}" }}', SPEAKER),            # pw-dump verbatim
    ('{\n  node.name = "x"\n}', "x"),                       # multi-line
    (None, ""),
])
def test_target_node_name_handles_both_shapes(raw, expected):
    """TRAP: taking pw-dump's string form as the name made every live smart
    filter look like it pointed at a sink that doesn't exist — a FAIL on a
    perfectly healthy machine. Only real pw-dump output showed it."""
    assert checks._target_node_name(raw) == expected


def test_live_chains_ignores_everything_else():
    dump = [_speaker_sink(), _node("Firefox"), {"type": "PipeWire:Interface:Link"}]
    assert checks.live_chains(dump) == []


def test_sink_names_only_audio_sinks():
    dump = [_speaker_sink(), _node("Firefox", **{"media.class": "Stream/Output/Audio"})]
    assert checks.sink_names(dump) == {SPEAKER}


def test_graph_readers_tolerate_no_daemon():
    assert checks.live_chains(None) == []
    assert checks.sink_names(None) == set()


# --- Stacked chains: the failure this doctor exists for ---------------------

def test_two_chains_on_one_target_is_a_failure():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Dolby_Warm")]
    result = checks.check_stacked_chains(checks.live_chains(dump), [])
    assert result.status == DOCTOR_FAIL
    assert "Dolby_Balanced" in result.detail and "Dolby_Warm" in result.detail


def test_one_chain_per_target_is_silent():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Other", target="alsa_output.hdmi")]
    assert checks.check_stacked_chains(checks.live_chains(dump), []) is None


def test_virtual_sinks_do_not_count_as_stacked():
    """Without filter.smart, WirePlumber's chaining logic never sees them —
    that is the whole reason --variant all requires --target-sink ''."""
    dump = [_speaker_sink(), *_virtual_chain("A", "s"), *_virtual_chain("B", "s")]
    assert checks.check_stacked_chains(checks.live_chains(dump), []) is None


@pytest.mark.parametrize("chains,flagged", [
    ([("A", ""), ("B", "")], True),            # both follow the default sink
    ([("A", SPEAKER), ("B", SPEAKER)], False),  # both pinned
    ([("A", ""), ("B", SPEAKER)], False),       # only one is loose
    ([("A", "")], False),                       # nothing to chain into
])
def test_unpinned_siblings(chains, flagged):
    dump = [_speaker_sink()]
    for name, pin in chains:
        dump += _virtual_chain(name, pin)
    result = checks.check_unpinned_siblings(checks.live_chains(dump))
    assert (result is not None) is flagged
    if flagged:
        assert result.status == DOCTOR_WARN


# --- Default output: which sink is selected --------------------------------
#
# Measured on this hardware (issue #63): selecting the chain does NOT process
# twice — the graph is identical either way — but it puts two sinks in series,
# each with its own volume, and the chain's control lands *ahead of* the filter
# graph (indistinguishable from scaling the source content: S/R 730 dB) while
# the speaker's hardware control lands after everything PipeWire does.

def _defaults(effective="", configured=""):
    return checks.DefaultSink(effective=effective, configured=configured)


def test_default_sinks_reads_both_keys():
    dump = [_default_metadata(effective=SPEAKER, configured="effect_input.X")]
    got = checks.default_sinks(dump)
    assert got.effective == SPEAKER
    assert got.configured == "effect_input.X"


@pytest.mark.parametrize("raw,expected", [
    ({"name": SPEAKER}, SPEAKER),           # pw-dump's parsed shape
    (f'{{"name": "{SPEAKER}"}}', SPEAKER),  # raw Spa:String:JSON text
    ("bare-name", "bare-name"),             # not JSON at all
    (None, ""),
    ([], ""),
])
def test_metadata_node_name_handles_every_shape(raw, expected):
    """Same trap as _target_node_name: guess the shape wrong and every branch
    below reads an empty default and goes quiet — indistinguishable from
    "nothing to report"."""
    assert checks._metadata_node_name(raw) == expected


def test_default_sinks_ignores_other_metadata_objects():
    dump = [{"type": "PipeWire:Interface:Metadata",
             "props": {"metadata.name": "route-settings"},
             "metadata": [{"key": "default.audio.sink",
                           "value": {"name": "nope"}}]},
            _default_metadata(effective=SPEAKER)]
    assert checks.default_sinks(dump).effective == SPEAKER


def test_default_sinks_tolerates_no_daemon():
    assert checks.default_sinks(None) == checks.DefaultSink()


def test_selected_smart_filter_warns_about_the_second_volume():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(dump), dump)
    assert result.status == DOCTOR_WARN
    assert result.label == "Default output"
    assert "effect_input.Dolby_Balanced" in result.detail
    # The target sink belongs in the steps, which print verbatim — a 70-char
    # node name inside the wrapped detail folds across two lines mid-name.
    assert SPEAKER not in result.detail
    assert any("set-default-sink" in text and SPEAKER in text
               for _, text in result.steps)


def test_selected_smart_filter_names_the_remembered_pick_only_when_it_is_one():
    """The predicate has to match the sentence. WirePlumber restores the
    *current* configured sink, so promising it comes back after every restart
    is false for a chain that merely won the automatic pick."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    chains, sinks = checks.live_chains(dump), checks.sink_names(dump)
    chain = "effect_input.Dolby_Balanced"

    picked = checks.check_default_sink(
        chains, [], _defaults(effective=chain, configured=chain), sinks, dump)
    assert "picked last" in picked.detail

    auto = checks.check_default_sink(
        chains, [], _defaults(effective=chain, configured=SPEAKER), sinks, dump)
    assert "picked last" not in auto.detail


def test_smart_filter_left_unselected_is_silent():
    """The state the tool aims for: speaker selected, chain inserted into it."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    assert checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective=SPEAKER, configured=SPEAKER),
        checks.sink_names(dump), dump) is None


def test_virtual_chain_nobody_selected_warns_that_it_does_nothing():
    dump = [_speaker_sink(), *_virtual_chain("Dolby_Balanced")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [], _defaults(effective=SPEAKER),
        checks.sink_names(dump), dump)
    assert result.status == DOCTOR_WARN
    assert "nothing is going through it" in result.detail
    assert "--target-sink ''" in result.detail


def test_selected_virtual_chain_is_silent():
    """That mode working as intended — the user picked it, which is the point."""
    dump = [_speaker_sink(), *_virtual_chain("Dolby_Balanced")]
    assert checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(dump), dump) is None


def test_several_virtual_chains_report_once_without_the_smart_filter_advice():
    """--variant all installs several by design and only one can be selected,
    so this must not fire per chain — and must not offer smart-filter routing,
    which the converter refuses for multi-chain installs."""
    dump = [_speaker_sink(), *_virtual_chain("A"), *_virtual_chain("B"),
            *_virtual_chain("C")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [], _defaults(effective=SPEAKER),
        checks.sink_names(dump), dump)
    assert result.status == DOCTOR_WARN
    assert "--target-sink ''" not in result.detail
    assert sum("set-default-sink" in text for _, text in result.steps) == 3


def test_stale_remembered_chain_warns():
    """Measured: with this name at the head of WirePlumber's stack, installing
    a chain under it takes the default output back on the next restart."""
    dump = [_speaker_sink(), _default_metadata(effective=SPEAKER,
                                               configured="effect_input.Gone")]
    result = checks.check_default_sink(
        [], [], checks.default_sinks(dump), checks.sink_names(dump), dump)
    assert result.status == DOCTOR_WARN
    assert "effect_input.Gone" in result.detail
    assert any(SPEAKER in text for _, text in result.steps)


def test_stale_remembered_chain_defers_to_the_load_failure(tmp_path):
    """A conf of that name on disk means the chain failed to load, which
    check_confs_loaded already FAILs on. Two remedies for one file reads as two
    problems."""
    conf = checks.InstalledConf(path=tmp_path / "Gone.conf",
                                node_name="effect_input.Gone")
    dump = [_speaker_sink()]
    assert checks.check_default_sink(
        [], [conf], _defaults(effective=SPEAKER, configured="effect_input.Gone"),
        checks.sink_names(dump), dump) is None


def test_selected_smart_filter_outranks_an_unselected_virtual_one():
    dump = [_speaker_sink(), *_smart_chain("Smart"), *_virtual_chain("Loose")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [], _defaults(effective="effect_input.Smart"),
        checks.sink_names(dump), dump)
    assert "effect_input.Smart" in result.detail
    assert "Loose" not in result.detail


def test_default_sink_check_is_silent_without_a_daemon():
    assert checks.check_default_sink([], [], _defaults(), set(), None) is None


# --- The level you cannot see from the slider you are moving ----------------
#
# Two sinks in series means the one underneath is subtracted from everything,
# and nothing in a desktop's sound settings shows it while the chain is
# selected. This dev machine sits at 0.064548 — 40 %, -23.8 dB — under a chain
# reading 100 %.

def _sink_with_volume(name, linear):
    node = _node(name, **{"media.class": "Audio/Sink"})
    node["info"]["params"] = {"Props": [{"channelVolumes": [linear, linear]}]}
    return node


def _link(dump, src, dst):
    """A Link joining two nodes already in `dump`, by the ids they were given."""
    ids = {o["info"]["props"]["node.name"]: o["id"] for o in dump if "id" in o}
    return {"type": "PipeWire:Interface:Link",
            "info": {"output-node-id": ids[src], "input-node-id": ids[dst]}}


def _with_ids(objs):
    for i, obj in enumerate(objs, start=100):
        obj["id"] = i
    return objs


def test_sink_volumes_reads_channel_volumes():
    dump = [_sink_with_volume(SPEAKER, 0.064548), _node("Firefox")]
    assert checks.sink_volumes(dump) == {SPEAKER: 0.064548}


@pytest.mark.parametrize("linear,expected", [
    (0.064548, "40 % (-23.8 dB)"),   # this machine, as its settings show it
    (1.0, "100 % (0.0 dB)"),
    (0.0, "0 % (silent)"),
])
def test_volume_reading_speaks_the_readers_units(linear, expected):
    """The percentage is what they recognise from their own settings; the dB is
    how big the problem is. PulseAudio cubes the percentage, so neither can be
    derived from the other by eye."""
    assert checks._volume_reading(linear) == expected


def test_downstream_sink_follows_the_link_not_the_conf():
    """An unpinned v1 chain has no target.object — WirePlumber picks its
    downstream at link time, so the graph is the only place it is written."""
    dump = _with_ids([*_virtual_chain("Dolby_Balanced"),
                      _sink_with_volume(SPEAKER, 0.5)])
    dump.append(_link(dump, "effect_output.Dolby_Balanced", SPEAKER))
    assert checks.downstream_sink(dump, "Dolby_Balanced") == SPEAKER
    assert checks.downstream_sink(dump, "Nobody") == ""


def test_selected_smart_filter_names_the_level_underneath():
    dump = [_sink_with_volume(SPEAKER, 0.064548), *_smart_chain("Dolby_Balanced")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(dump), dump)
    assert "40 % (-23.8 dB)" in result.detail


def test_selected_v1_chain_warns_only_when_the_sink_below_is_turned_down():
    """Selecting a v1 chain is correct usage, so it stays silent — the fault is
    the invisible level underneath, not the selection."""
    quiet = _with_ids([*_virtual_chain("Dolby_Balanced"),
                       _sink_with_volume(SPEAKER, 0.064548)])
    quiet.append(_link(quiet, "effect_output.Dolby_Balanced", SPEAKER))
    result = checks.check_default_sink(
        checks.live_chains(quiet), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(quiet), quiet)
    assert result.status == DOCTOR_WARN
    assert "40 % (-23.8 dB)" in result.detail
    assert any(f"set-sink-volume {SPEAKER} 100%" in text
               for _, text in result.steps)

    loud = _with_ids([*_virtual_chain("Dolby_Balanced"),
                      _sink_with_volume(SPEAKER, 1.0)])
    loud.append(_link(loud, "effect_output.Dolby_Balanced", SPEAKER))
    assert checks.check_default_sink(
        checks.live_chains(loud), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(loud), loud) is None


# --- A chain turned down on its own slider ----------------------------------
#
# Measured: with the speaker selected and the chain at 0.125, output came back
# 7.9x down. The chain's volume applies in smart-filter mode too — nothing ever
# puts a reader on that slider, so it is the attenuation that survives getting
# the selected output right.

def test_chain_turned_down_is_reported_even_when_not_selected():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    dump[1]["info"]["params"] = {"Props": [{"channelVolumes": [0.125, 0.125]}]}
    result = checks.check_chain_volume(checks.live_chains(dump), dump)
    assert result.status == DOCTOR_WARN
    assert result.label == "Chain volume"
    assert "50 % (-18.1 dB)" in result.detail
    assert any("set-sink-volume effect_input.Dolby_Balanced 100%" in text
               for _, text in result.steps)


def test_chain_at_full_volume_is_silent():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    dump[1]["info"]["params"] = {"Props": [{"channelVolumes": [1.0, 1.0]}]}
    assert checks.check_chain_volume(checks.live_chains(dump), dump) is None


def test_chain_volume_says_how_many_others_without_listing_them():
    dump = [_speaker_sink(), *_smart_chain("A"), *_smart_chain("B")]
    for node in (dump[1], dump[3]):
        node["info"]["params"] = {"Props": [{"channelVolumes": [0.5, 0.5]}]}
    result = checks.check_chain_volume(checks.live_chains(dump), dump)
    assert "1 more like it" in result.detail


def test_chain_volume_is_silent_without_volume_data_or_daemon():
    """No Props in the dump is "we don't know", not "it is turned down"."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    assert checks.check_chain_volume(checks.live_chains(dump), dump) is None
    assert checks.check_chain_volume([], None) is None


# --- Addresses stay out of a block written to be pasted ---------------------

BT_SINK = "bluez_output.80_99_E7_E0_8A_23.1"


def test_plugin_presence_covers_every_uri_the_converter_can_emit(monkeypatch):
    """The doctor's plugin inventory has to match what a run can actually put
    in a conf, or a full house means nothing.

    Autogain and stereo tools were missing from the table. Autogain is the one
    that mattered: it is on by default on SoundWire devices, so the doctor
    could report every plugin present on the exact machine whose chain would
    not load. Asserted against the converter's URI constants rather than a
    copy of them, so a plugin added there without a row here fails.
    """
    from lib.pipewire import plugins, vbe
    asked = []

    def fake_run(cmd, **kwargs):
        asked.append(cmd[1])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(checks.subprocess, "run", fake_run)

    lines = checks._plugin_presence()
    expected = {plugins.LSP_PEQ_URI, plugins.LSP_MBC_URI, plugins.LSP_LIM_URI,
                plugins.LSP_AUTOGAIN_URI, plugins.CALF_BE_URI,
                plugins.CALF_ST_URI, vbe.LSP_FILTER_URI,
                vbe.CALF_SATURATOR_URI}
    assert set(asked) == expected, (
        f"not probed: {sorted(expected - set(asked))}")
    assert all(line.endswith(": present") for line in lines), lines


def test_plugin_presence_says_missing_when_lv2info_says_no(monkeypatch):
    """A non-zero exit is the whole point of the probe, so it has to survive
    the exception path too: `lv2info` that cannot run at all must not read as
    a plugin that is there."""
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(checks.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(returncode=255))
    assert all(line.endswith(": MISSING")
               for line in checks._plugin_presence())

    monkeypatch.setattr(checks.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError("boom")))
    assert all(line.endswith(": MISSING")
               for line in checks._plugin_presence())


def test_environment_block_prints_no_bluetooth_address():
    """The `Sinks:` list is every sink in the graph, and the closing line asks
    the reader to paste everything above it into an issue."""
    facts = {"version": "v-test", "wireplumber": (0, 5),
             "sinks": [SPEAKER, BT_SINK],
             "default": checks.DefaultSink(BT_SINK, BT_SINK)}
    lines = "\n".join(checks._environment_lines([], [], facts))
    assert "80_99_E7_E0_8A_23" not in lines
    # Presence survives: which sink is selected is the fact triage reads.
    assert "bluez_output.<mac>.1" in lines
    assert SPEAKER in lines


def test_a_command_is_dropped_rather_than_printed_with_an_address():
    """The one place redaction and runnability collide. A redacted name is not
    runnable, and printing the real one would put the address back into the
    same block the redaction just cleaned — so the command goes and the
    sound-settings route stays."""
    dump = [_speaker_sink(BT_SINK), *_smart_chain("Dolby_Balanced", target=BT_SINK)]
    result = checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(dump), dump)
    rendered = result.detail + "".join(text for _, text in result.steps)
    assert "80_99_E7_E0_8A_23" not in rendered
    assert not any("pactl" in text for _, text in result.steps)
    assert any("sound settings" in text for _, text in result.steps)
    # And the detail must not promise a pointer the dropped step took with it.
    assert "named in the fix below" not in result.detail


def test_an_ordinary_target_still_gets_its_command():
    """The fallback above must not cost every other reader their one
    copy-pasteable line."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced")]
    result = checks.check_default_sink(
        checks.live_chains(dump), [],
        _defaults(effective="effect_input.Dolby_Balanced"),
        checks.sink_names(dump), dump)
    assert any(f"pactl set-default-sink {SPEAKER}" in text
               for _, text in result.steps)


# --- Conf-vs-graph checks ---------------------------------------------------

def _conf(tmp_path, name, **kw):
    kw.setdefault("node_name", f"effect_input.{name}")
    return checks.InstalledConf(path=tmp_path / f"{name}.conf", **kw)


def test_conf_with_no_node_is_a_failure(tmp_path):
    """A missing LSP/Calf plugin makes PipeWire drop the whole file, so the
    conf looks installed and nothing is in the graph."""
    confs = [_conf(tmp_path, "Dolby_Balanced")]
    result = checks.check_confs_loaded(confs, [], dump=[])
    assert result.status == DOCTOR_FAIL
    assert "Dolby_Balanced.conf" in result.detail
    assert "plugin" in result.detail


def test_conf_present_in_graph_passes(tmp_path):
    dump = [*_smart_chain("Dolby_Balanced")]
    result = checks.check_confs_loaded([_conf(tmp_path, "Dolby_Balanced")],
                                       checks.live_chains(dump), dump=dump)
    assert result.status == DOCTOR_PASS


def test_conf_load_is_unknown_without_a_daemon(tmp_path):
    result = checks.check_confs_loaded([_conf(tmp_path, "X")], [], dump=None)
    assert result.status == DOCTOR_UNKNOWN


def test_no_confs_is_not_a_load_failure():
    assert checks.check_confs_loaded([], [], dump=[]) is None


def test_missing_irs_is_reported_once_per_file(tmp_path):
    """The stereo convolver is two nodes reading one file, so a missing IRS
    would otherwise be counted once per channel."""
    absent = tmp_path / "gone.irs"
    result = checks.check_irs_present(
        [_conf(tmp_path, "A", irs=[absent, absent])])
    assert result.detail.count("gone.irs") == 1
    assert result.detail.startswith("1 impulse file")


def test_missing_irs_is_a_failure(tmp_path):
    present, absent = tmp_path / "here.irs", tmp_path / "gone.irs"
    present.write_bytes(b"")
    assert checks.check_irs_present(
        [_conf(tmp_path, "A", irs=[present])]) is None
    result = checks.check_irs_present(
        [_conf(tmp_path, "B", irs=[present, absent])])
    assert result.status == DOCTOR_FAIL
    assert "gone.irs" in result.detail


def test_target_sink_that_no_longer_exists(tmp_path):
    dump = [_speaker_sink(), *_smart_chain("A", target="alsa_output.vanished")]
    chains = checks.live_chains(dump)
    result = checks.check_targets_exist(chains, checks.sink_names(dump), dump)
    assert result.status == DOCTOR_FAIL
    assert "alsa_output.vanished" in result.detail
    # And is silent when the target is really there.
    dump = [_speaker_sink(), *_smart_chain("A")]
    assert checks.check_targets_exist(checks.live_chains(dump),
                                      checks.sink_names(dump), dump) is None


# --- Environment checks -----------------------------------------------------

@pytest.mark.parametrize("version,status", [
    (None,   DOCTOR_UNKNOWN),
    ((0, 4), DOCTOR_FAIL),      # no smart-filter support at all
    ((0, 5), DOCTOR_PASS),
    ((1, 0), DOCTOR_PASS),
])
def test_wireplumber_version(version, status):
    assert checks.check_wireplumber(version).status == status


def test_easyeffects_conflict():
    live = checks.live_chains(_smart_chain("Dolby_Balanced"))
    assert checks.check_easyeffects_conflict({SPEAKER}, live, dump=[]) is None
    result = checks.check_easyeffects_conflict(
        {SPEAKER, "easyeffects_sink"}, live, dump=[])
    assert result.status == DOCTOR_WARN
    assert "twice" in result.detail
    # No graph, no claim.
    assert checks.check_easyeffects_conflict(set(), live, dump=None) is None


def test_easyeffects_alone_is_not_a_conflict():
    """Nothing of ours is installed, so "processed twice" would be false —
    EasyEffects running by itself is just EasyEffects running."""
    assert checks.check_easyeffects_conflict(
        {SPEAKER, "easyeffects_sink"}, [], dump=[]) is None


def test_conf_version_drift(tmp_path):
    same = [_conf(tmp_path, "A", version="v1")]
    assert checks.check_conf_versions(same, "v1") is None
    result = checks.check_conf_versions(same, "v2")
    assert result.status == DOCTOR_WARN and "v1" in result.detail
    # An unreadable header is not drift.
    assert checks.check_conf_versions([_conf(tmp_path, "A")], "v2") is None


def test_conf_in_unread_directory(tmp_path, monkeypatch):
    """filter-chain.conf.d/ is for `pipewire -c`; the running daemon only
    auto-includes pipewire.conf.d/, so a conf there loads for nobody."""
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path)
    assert checks.check_conf_directory() is None
    (tmp_path / "Dolby_Balanced.conf").write_text(
        conf.CONF_HEADER_MARK + " — see\ncontext.modules = []\n")
    result = checks.check_conf_directory()
    assert result.status == DOCTOR_WARN
    assert "pipewire.conf.d" in result.detail


# --- Reading our own confs back --------------------------------------------

def test_installed_confs_ignores_foreign_files(tmp_path):
    (tmp_path / "someone_elses.conf").write_text("context.modules = []\n")
    assert checks.installed_confs(tmp_path) == []


# --- The report as a whole --------------------------------------------------

def test_doctor_reports_a_stacked_pair(tmp_path, monkeypatch, silence_console,
                                       capsys):
    """End to end over the state that prompted this: two chains on one sink.
    The verdict must not come out clean, and the report has to say how to
    clear it — deleting a file is the whole remedy, and the one step a reader
    can't derive."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Dolby_Warm")]
    monkeypatch.setattr(checks, "_pw_dump", lambda: dump)
    monkeypatch.setattr(checks, "_wireplumber_version", lambda: (0, 5))
    monkeypatch.setattr(checks, "_plugin_presence", lambda: [])
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    silence_console(console)

    assert checks.report_pw_doctor() == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "Stacked filter chains" in out
    assert "No blocking problems detected." not in out
    assert "systemctl --user restart pipewire" in out
    # The paste block is the point of running this before filing an issue.
    assert "=== Environment ===" in out
    assert "Paste everything above into an issue" in out
    # ...and `--doctor > report.txt` has to capture all of it. The console and
    # the bare prints here both target stdout, so the report arrives whole with
    # no mechanism holding the two together.
    assert captured.err == ""


def test_doctor_ends_on_the_diagnosis_not_the_inventory(tmp_path, monkeypatch,
                                                        silence_console, capsys):
    """Inventory leads, diagnosis trails.

    This report is longer than a terminal and the reader is here because
    something is already wrong. Printed inventory-last, the checks, the verdict
    and the restart command scrolled off a 26-line window and a PCI listing was
    the last thing on screen. Same principle as the generator's closing block
    (`.claude/rules/user-messages.md`), which nothing else traps for --doctor.
    """
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Dolby_Warm")]
    monkeypatch.setattr(checks, "_pw_dump", lambda: dump)
    monkeypatch.setattr(checks, "_wireplumber_version", lambda: (0, 5))
    monkeypatch.setattr(checks, "_plugin_presence", lambda: [])
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    # Stubbed rather than probed: the sequence is the assertion here, and the
    # real hardware block differs line for line per machine.
    monkeypatch.setattr(checks.gen, "_gather_speaker_info", lambda: None)
    monkeypatch.setattr(checks.gen, "_print_speaker_info",
                        lambda info: print("=== HARDWARE STUB ==="))
    silence_console(console)

    assert checks.report_pw_doctor() == 0
    out = capsys.readouterr().out

    # Widest context first, then this tool's state, then what is wrong with it.
    # Environment sits directly above the checks because the check details name
    # those confs and sinks.
    assert (out.index("=== HARDWARE STUB ===")
            < out.index("=== Environment ===")
            < out.index("=== PipeWire filter-chain doctor ===")
            < out.index("Stacked filter chains")
            < out.index("Summary:")
            < out.index("systemctl --user restart pipewire")
            < out.index(report_findings._REPORT_FORM_URL))

    # The bottom line has to survive one screen — 26 lines is what
    # tools/user_review_capture.py treats as a terminal's worth.
    tail = "\n".join(out.splitlines()[-26:])
    assert "Summary:" in tail
    assert "systemctl --user restart pipewire" in tail
    # A FAIL suppresses the verdict line (lib/doctor.py), so the summary and the
    # remedy are what carry the bottom line here.
    assert out.rstrip().endswith(report_findings._REPORT_FORM_URL)


def test_doctor_without_a_daemon_says_so(tmp_path, monkeypatch,
                                         silence_console, capsys):
    monkeypatch.setattr(checks, "_pw_dump", lambda: None)
    monkeypatch.setattr(checks, "_wireplumber_version", lambda: None)
    monkeypatch.setattr(checks, "_plugin_presence", lambda: [])
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    silence_console(console)

    assert checks.report_pw_doctor() == 0
    out = capsys.readouterr().out
    assert "pw-dump didn't answer" in out
    assert "No blocking problems detected." not in out


def test_doctor_needs_no_preset(monkeypatch):
    """--doctor inspects what is installed, so the positional is optional —
    but it stays required for every other mode."""
    monkeypatch.setattr(checks, "report_pw_doctor", lambda: 0)
    assert ee_to_pipewire.main(["--doctor", "--no-color"]) == 0
    with pytest.raises(SystemExit) as excinfo:
        ee_to_pipewire.main(["--no-color"])
    assert excinfo.value.code == 2


def test_unread_directory_confs_are_not_counted_as_installed(tmp_path,
                                                             monkeypatch):
    """A conf PipeWire never reads is not installed. Counting it inflated the
    conf total and — because a copy shares its node name with the real one —
    let "Chains loaded" pass for a file that had loaded for nobody."""
    scanned, unread = tmp_path / "pipewire.conf.d", tmp_path / "filter-chain.conf.d"
    scanned.mkdir()
    unread.mkdir()
    body = conf.CONF_HEADER_MARK + " — see\ncontext.modules = []\n"
    (scanned / "Dolby_Balanced.conf").write_text(body)
    (unread / "Dolby_Balanced.conf").write_text(body)
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", scanned)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", unread)
    monkeypatch.setattr(checks, "_pw_dump", lambda: [])
    monkeypatch.setattr(checks, "_wireplumber_version", lambda: (0, 5))

    _checks, confs, _chains, _facts = checks.gather_pw_doctor()
    assert [c.path.parent for c in confs] == [scanned]
    # And the stray still gets reported, by the check that owns it.
    assert checks.check_conf_directory().status == DOCTOR_WARN


def test_wireplumber_is_probed_once_per_run(tmp_path, monkeypatch):
    """"Probe everything once, then judge" — `wireplumber --version` is a
    subprocess, and the check and the facts dict are two readers of one answer,
    not two spawns. None is an answer too: a version the binary won't give is
    not a reason to ask it again."""
    def _run(answer):
        calls = []
        monkeypatch.setattr(checks, "_wireplumber_version",
                            lambda: (calls.append(answer), answer)[1])
        monkeypatch.setattr(checks, "_pw_dump", lambda: [])
        monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
        results, _confs, _chains, facts = checks.gather_pw_doctor()
        wireplumber = [c for c in results if c.label == "WirePlumber"]
        return calls, wireplumber, facts

    calls, wireplumber, facts = _run((0, 4))
    assert len(calls) == 1
    # ...and that one answer reaches both consumers, unchanged.
    assert facts["wireplumber"] == (0, 4)
    assert [c.status for c in wireplumber] == [DOCTOR_FAIL]

    calls, wireplumber, facts = _run(None)
    assert len(calls) == 1
    assert facts["wireplumber"] is None
    assert [c.status for c in wireplumber] == [DOCTOR_UNKNOWN]


# --- Warning at the moment it happens ---------------------------------------

def _write_conf(path: Path, target=SPEAKER, node="Dolby_Balanced"):
    path.write_text(
        f'''{conf.CONF_HEADER_MARK} — see
# version: vtest
context.modules = [
    {{
        name = "libpipewire-module-filter-chain"
        args = {{
            capture.props = {{
                node.name = "effect_input.{node}"
                media.class = "Audio/Sink"
                filter.smart = true
                filter.smart.target = {{ node.name = "{target}" }}
            }}
            playback.props = {{ node.name = "effect_output.{node}" }}
        }}
    }}
]
''')


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_second_variant_warns_that_it_stacks(tmp_path, silence_console,
                                             capsys):
    """Trying another voicing is the obvious next step and the run's own copy
    suggests it — but --force only guards one output path, so the second conf
    lands beside the first and WirePlumber runs both in series."""
    silence_console(console)
    first, second = tmp_path / "Dolby_Balanced.conf", tmp_path / "Dolby_Warm.conf"
    _write_conf(first)
    _write_conf(second, node="Dolby_Warm")

    checks.warn_if_stacked(second, SPEAKER)
    out = capsys.readouterr().out
    assert "Dolby_Balanced.conf" in out
    assert "one after another" in out


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_first_conf_and_virtual_sinks_do_not_warn(tmp_path, silence_console,
                                                  capsys):
    silence_console(console)
    only = tmp_path / "Dolby_Balanced.conf"
    _write_conf(only)
    checks.warn_if_stacked(only, SPEAKER)
    assert capsys.readouterr().out == "", "a lone chain stacks with nothing"

    # --target-sink '' emits no smart filter, so several never chain — that is
    # the whole reason --variant all requires it.
    other = tmp_path / "Dolby_Warm.conf"
    _write_conf(other, node="Dolby_Warm")
    checks.warn_if_stacked(other, None)
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_chains_on_different_sinks_do_not_warn(tmp_path, silence_console,
                                               capsys):
    silence_console(console)
    _write_conf(tmp_path / "Dolby_Balanced.conf", target="alsa_output.hdmi")
    second = tmp_path / "Dolby_Warm.conf"
    _write_conf(second, node="Dolby_Warm")
    checks.warn_if_stacked(second, SPEAKER)
    assert capsys.readouterr().out == ""


def test_easyeffects_running_warns_it_processes_twice(silence_console, capsys):
    """The likeliest converter reader is an EE user crossing over for
    --enable virtual-bass — with EasyEffects still running, whose output
    plays into the very sink the smart filter attaches to."""
    silence_console(console)
    checks.warn_if_easyeffects_running(running=True)
    out = capsys.readouterr().out
    assert "EasyEffects is running" in out
    assert "twice" in out
    assert "Quit EasyEffects" in out
    # The switch-back story, delivered at the moment both chains coexist.
    assert "restart PipeWire" in out


def test_no_easyeffects_process_stays_silent(silence_console, capsys,
                                             monkeypatch):
    silence_console(console)
    checks.warn_if_easyeffects_running(running=False)
    assert capsys.readouterr().out == ""

    # Probe unavailable (no pgrep): None, never a crash or a guess — and
    # None reaches the same silent branch as False.
    def _no_pgrep(*_a, **_kw):
        raise FileNotFoundError("pgrep")
    monkeypatch.setattr(checks.subprocess, "run", _no_pgrep)
    assert unpatched_ee_probe() is None
    checks.warn_if_easyeffects_running(running=None)
    assert capsys.readouterr().out == ""


def test_emit_check_wraps_prose_and_leaves_steps_alone(capsys):
    """The printer rule both doctors depend on: a check's prose reflows to the
    terminal, its commands don't. A command folded across two lines is not
    runnable, which is why checks used to point elsewhere for the fix."""
    from lib.doctor import CheckResult, emit_check

    command = "amixer -c sofhdadsp cset \"iface=CARD,name='Speaker Force Firmware Load'\" on"
    emit_check(CheckResult(DOCTOR_WARN, "Gate", "word " * 40,
                           steps=(("dim", "Switch it on:"), ("", ""),
                                  ("cta", command))),
               lambda _style, text: print(text), width=60)
    out = capsys.readouterr().out

    assert f"         {command}" in out.splitlines(), \
        "the command must survive as one line"
    assert len(command) > 60, "…and this one is wider than the terminal"
    assert all(len(l) <= 60 for l in out.splitlines() if l.startswith("         word"))
    assert "" in out.splitlines(), "a blank step prints an unindented blank line"


def test_emit_check_without_steps_is_unchanged(capsys):
    from lib.doctor import CheckResult, emit_check

    emit_check(CheckResult(DOCTOR_PASS, "Presets", "all load."),
               lambda _style, text: print(text), width=80)
    assert capsys.readouterr().out == "  [PASS] Presets\n         all load.\n"


# --- One wrap width, both doctors -------------------------------------------

def _pw_check_block(check, monkeypatch, tmp_path, capsys) -> list[str]:
    """Render one CheckResult through the whole PipeWire report and return the
    lines of its check block — headers, summary and paste block dropped.

    Goes through ``report_pw_doctor`` rather than calling the printer directly
    because the width this file is about is chosen at that call site, and a
    test that passed its own would assert nothing about it.

    COLUMNS pins what ``shutil.get_terminal_size`` answers, so a width read
    from the terminal anywhere on this path lands on the cap (120) instead of
    the runner's own window. Without it the assertions below would pass or
    fail depending on how wide the terminal running the suite happens to be.
    """
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(checks, "gather_pw_doctor",
                        lambda: ([check], [], [],
                                 {"wireplumber": None, "version": "0.0-test",
                                  "sinks": []}))
    monkeypatch.setattr(checks, "_plugin_presence", lambda: [])
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)

    assert checks.report_pw_doctor() == 0
    lines = capsys.readouterr().out.split("Summary:")[0].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("  ["))
    while lines and lines[-1] == "":
        lines.pop()
    return lines[start:]


def test_pw_doctor_wraps_detail_to_the_console_width(tmp_path, monkeypatch,
                                                     silence_console, capsys):
    """Redirected output used to wrap wider here than anywhere else the tool
    prints: this report sized its own prose off a 100-column fallback while
    lib.console fell back to 80, so `--doctor > report.txt` came out at a
    measure no other block used."""
    from lib.doctor import CheckResult

    monkeypatch.setattr(console, "_wrap_width", lambda: 72)
    silence_console(console)

    lines = _pw_check_block(CheckResult(DOCTOR_WARN, "Gate", "word " * 40),
                            monkeypatch, tmp_path, capsys)
    assert len([l for l in lines if l.startswith("         ")]) > 1, \
        "the detail has to be long enough to wrap for this to test anything"
    assert max(len(l) for l in lines) <= 72, \
        "a detail line wider than the console width the rest of the run uses"


def _ee_check_block(check, monkeypatch, capsys) -> list[str]:
    """The same, through the whole EasyEffects report.

    ``DoctorReport.speaker_info`` defaults to None, so the hardware block is
    skipped and nothing here probes the machine.
    """
    from lib.report import doctor_run, environment

    monkeypatch.setenv("COLUMNS", "200")
    doctor_run._print_doctor_report(environment.DoctorReport(checks=[check]))
    lines = capsys.readouterr().out.split("Summary:")[0].splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("  ["))
    while lines and lines[-1] == "":
        lines.pop()
    return lines[start:]


def test_both_doctors_render_a_check_identically(tmp_path, monkeypatch,
                                                 silence_console, capsys):
    """The two reports must read as one tool, and a check that wraps at one
    measure here and another there is the visible half of that. Same
    CheckResult, same bytes.

    Both sides go through their real report function, not the shared printer
    directly: that the two now call one printer is the thing under test, so a
    test that called it itself would assert nothing about either report.
    """
    from lib.doctor import CheckResult

    check = CheckResult(DOCTOR_WARN, "Gate", "word " * 40,
                        steps=(("dim", "Switch it on:"), ("", ""),
                               ("cta", "systemctl --user restart pipewire")))
    monkeypatch.setattr(console, "_wrap_width", lambda: 72)
    silence_console(console)

    assert (_pw_check_block(check, monkeypatch, tmp_path, capsys)
            == _ee_check_block(check, monkeypatch, capsys))
