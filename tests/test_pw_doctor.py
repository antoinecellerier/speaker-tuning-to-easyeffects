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
from lib import console, packages
from lib.pipewire import checks, conf, session


@pytest.fixture(autouse=True)
def _no_live_pipewire_window(monkeypatch):
    """The report's `=== PipeWire ===` section reads pw-top over a five-second
    window; no test here should pay that, nor depend on this machine's
    graph. The parser tests below stub `session._run` themselves."""
    monkeypatch.setattr(checks, "_probe_pipewire", lambda chains, default: (
        session.ClockSettings(reason="pw-metadata not found"),
        session.Dropouts(reason="pw-top not found"), None))
# Bound before the autouse `no_live_easyeffects_probe` fixture patches the
# module attribute, so the probe itself stays testable.
from lib import ee_socket
from lib.ee_socket import easyeffects_running as unpatched_ee_probe
from lib.doctor import DOCTOR_FAIL, DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN
from lib.report import doctor_layout
from lib.report import findings as report_findings
from tests.conftest import assert_rows_line_up

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


def test_default_sinks_tolerates_no_daemon(monkeypatch):
    """None (nothing answered) carries the why; [] is a daemon answering
    with an empty graph — "none", not "not read"."""
    monkeypatch.setattr(checks.shutil, "which", lambda name: "/usr/bin/" + name)
    assert checks.default_sinks(None) == checks.DefaultSink(
        reason=checks.NO_DUMP_REASON)
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    assert checks.default_sinks(None).reason == "pw-dump not found"
    assert checks.default_sinks([]) == checks.DefaultSink()


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


def test_unreadable_confs_are_not_reported_as_all_present(tmp_path):
    """TRAP: the all-clear was being given about files nothing had read.

    `check_confs_loaded` looks for each conf's node in the graph, and a conf
    whose contents could not be parsed has no node name to look for — so with
    `spa-json-dump` absent every conf fell out of the filter, `missing` came
    back empty, and the check reported "N conf(s), all present in the graph".
    That is the loudest reassurance this report can give, over the one state
    where it knows least. A machine can reach it by installing PipeWire
    without its command-line tools, which Fedora, openSUSE and Alpine all
    allow.
    """
    unreadable = checks.InstalledConf(path=tmp_path / "Dolby.conf",
                                      readable=False,
                                      unreadable=checks.NO_SPA_JSON_DUMP)
    result = checks.check_confs_loaded([unreadable], [], dump=[])
    assert result.status == DOCTOR_UNKNOWN, result
    assert "could be read" in result.detail

    # And a readable one beside it is still judged, with the unread one
    # counted rather than quietly folded into the total.
    readable = _conf(tmp_path, "Live", node_name="effect_input.Live")
    result = checks.check_confs_loaded(
        [readable, unreadable], [checks.LiveChain(name="Live")], dump=[])
    assert result.status == DOCTOR_PASS, result
    assert "1 conf(s), all present" in result.detail
    assert "1 more couldn't be read" in result.detail


def _installed(tmp_path, name):
    """A conf of ours on disk, as `installed_confs` would have read it back."""
    return checks.InstalledConf(path=tmp_path / f"{name}.conf",
                                node_name=f"effect_input.{name}",
                                smart=True, target=SPEAKER)


def test_stacked_chains_point_at_the_block_that_exists(tmp_path):
    """The Environment block moved *above* the checks when inventory-leads
    landed (`.claude/rules/user-messages.md`), and this detail still sent the
    reader to "the block below" — where the report's closing advice is, not
    the conf paths it means."""
    dump = [_speaker_sink(), *_smart_chain("A"), *_smart_chain("B")]
    result = checks.check_stacked_chains(
        checks.live_chains(dump),
        [_installed(tmp_path, "A"), _installed(tmp_path, "B")])
    assert "block below" not in result.detail, result.detail
    assert "Environment block above" in result.detail


def test_delete_the_others_never_names_a_filter_we_did_not_install(tmp_path):
    """`live_chains` sees every effect_input.* node, which is what
    module-filter-chain calls any chain — so a smart filter from elsewhere on
    the same speakers lands in this group. It used to be handed to the reader
    as "<node>.conf", a filename nothing on disk answers to."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("someone_elses_eq")]
    result = checks.check_stacked_chains(
        checks.live_chains(dump), [_installed(tmp_path, "Dolby_Balanced")])
    assert result.status == DOCTOR_FAIL
    assert "someone_elses_eq.conf" not in result.detail
    assert "Keep one of Dolby_Balanced.conf" in result.detail
    assert "other smart filter(s) on the same speakers" in result.detail
    assert "someone_elses_eq" in result.detail


def test_a_stack_of_nothing_of_ours_offers_no_file_to_delete():
    """Two foreign smart filters on one sink is still the fault, but there is
    no conf of ours to name — and inventing one is what this replaced."""
    dump = [_speaker_sink(), *_smart_chain("theirs_a"), *_smart_chain("theirs_b")]
    result = checks.check_stacked_chains(checks.live_chains(dump), [])
    assert result.status == DOCTOR_FAIL
    assert "Keep one of" not in result.detail
    assert "nothing of ours to delete" in result.detail
    assert ".conf" not in result.detail


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


# --- The plugin remedy: inventory above, the fix in the check ---------------

def _probe(**present) -> checks.PluginProbe:
    """A probe answering `present` per label, everything else installed.

    Built from the module's own URI table rather than a copy, so a plugin
    added there is answered here too instead of quietly dropping out of the
    fixture.
    """
    return checks.PluginProbe(
        entries=tuple((label, uri, present.get(label, True))
                      for label, uri in checks._PLUGIN_URIS))


def _conf_using(*labels, path=Path("/tmp/Dolby.conf")):
    """A conf naming the plugins behind `labels`, or all of them if none given.

    The check judges what the machine's own confs ask for, so a fixture that
    supplies no conf is asking about nothing — which is a real answer (None)
    and not the one most of these tests are about.
    """
    by_label = dict(checks._PLUGIN_URIS)
    wanted = labels or tuple(by_label)
    return checks.InstalledConf(path=path, node_name="effect_input.Dolby",
                                plugins=[by_label[l] for l in wanted])


def test_a_missing_calf_plugin_names_calfs_package_and_not_lsps(monkeypatch):
    """The reader installs a package, so the FAIL has to name the right one.
    Mapping the URI namespace to a vendor is what makes that possible — a
    check that named every missing URI instead would send someone whose Calf
    is missing looking through eight strings for the one that matters, and a
    check that named both vendors would have them install LSP twice."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    result = checks.check_plugins_present(
        _probe(**{"Calf bass enhancer": False, "Calf stereo tools": False}),
        [_conf_using()])

    assert result.status == DOCTOR_FAIL
    assert "Calf" in result.detail
    assert "LSP" not in result.detail
    steps = "\n".join(text for _style, text in result.steps)
    assert "calf-plugins" in steps
    assert "lsp-plugins-lv2" not in steps


def test_a_plugin_no_conf_asks_for_is_not_a_fault(monkeypatch):
    """TRAP: the check scored the catalogue, not the machine.

    It probed all eight URIs the converter is *able* to emit and FAILed on any
    miss, so an LSP-only chain that loads and plays perfectly reported a FAIL
    for want of Calf — and `print_verdict` closed on "Fix the FAIL lines above
    first". openSUSE makes it permanent: Calf is not in its repositories, this
    project's own README says to install LSP alone there, and the FAIL's steps
    render as a note about Packman with no command under them. A reader who
    followed our instructions exactly would have met a failure that was ours.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.SUSE)
    lsp_only = _conf_using("LSP PEQ", "LSP MBC", "LSP limiter")
    assert checks.check_plugins_present(
        _probe(**{"Calf bass enhancer": False, "Calf stereo tools": False,
                  "Calf saturator (virtual-bass)": False}),
        [lsp_only]).status == DOCTOR_PASS

    # And a plugin the conf *does* name still fails, or the narrowing would
    # have thrown the check away rather than aimed it.
    assert checks.check_plugins_present(
        _probe(**{"LSP MBC": False}), [lsp_only]).status == DOCTOR_FAIL


def test_nothing_to_judge_is_not_a_verdict(monkeypatch):
    """No conf, an unreadable one, or a chain of builtins only.

    Each leaves the check with no plugin anyone has asked for. A PASS would be
    an all-clear over an empty set and a FAIL a fault nobody owns, so it says
    nothing at all — the Environment block still carries what was found, which
    is what a pasted report needs.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    probe = _probe(**{"Calf bass enhancer": False})
    assert checks.check_plugins_present(probe, []) is None
    unreadable = checks.InstalledConf(path=Path("/tmp/x.conf"), readable=False,
                                      unreadable=checks.NO_SPA_JSON_DUMP)
    assert checks.check_plugins_present(probe, [unreadable]) is None
    builtin_only = checks.InstalledConf(path=Path("/tmp/y.conf"),
                                        node_name="effect_input.y")
    assert checks.check_plugins_present(probe, [builtin_only]) is None


def test_missing_plugins_from_both_vendors_name_both(monkeypatch):
    """One conf can need both, and a command that installs half of what is
    missing loads no more of the chain than none of it."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    result = checks.check_plugins_present(
        _probe(**{"LSP PEQ": False, "Calf saturator (virtual-bass)": False}),
        [_conf_using()])

    assert result.status == DOCTOR_FAIL
    assert "LSP and Calf" in result.detail
    steps = "\n".join(text for _style, text in result.steps)
    assert "lsp-plugins-lv2" in steps and "calf-plugins" in steps


def test_no_lv2info_is_unknown_and_offers_the_package(monkeypatch):
    """Not a FAIL: nothing was checked, so nothing may be reported as missing
    — and not a silent skip either, because this is the check that would have
    named the commonest reason a conf loads nothing."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    result = checks.check_plugins_present(checks.PluginProbe(has_lv2info=False),
                                        [_conf_using()])

    assert result.status == DOCTOR_UNKNOWN
    assert "lv2info" in result.detail
    assert any("lilv-utils" in text for _style, text in result.steps)


def test_no_lv2info_and_no_conf_is_not_a_question_about_nothing(monkeypatch):
    """The UNKNOWN is about "the plugins a conf names", so it needs a conf.
    Printed on a machine with an empty directory it offered a package to a
    reader whose own report says there is nothing installed to check."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    no_lv2info = checks.PluginProbe(has_lv2info=False)
    assert checks.check_plugins_present(no_lv2info, []) is None
    unreadable = checks.InstalledConf(path=Path("/tmp/x.conf"), readable=False,
                                      unreadable=checks.NO_SPA_JSON_DUMP)
    assert checks.check_plugins_present(no_lv2info, [unreadable]) is None


def test_a_full_plugin_house_passes_with_nothing_to_do(monkeypatch):
    """The one PASS worth printing for something that is *there*: it rules out
    the commonest cause, so a reader whose chain still doesn't load knows to
    stop looking at packages. Nothing to do, so no steps to print.

    That elimination reaches the failing reader through the chains-loaded
    FAIL's cross-reference to this check — not spelled into the PASS as a
    hypothetical, which green reports (#78) read as a live failure path."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    result = checks.check_plugins_present(_probe(), [_conf_using()])

    assert result.status == DOCTOR_PASS
    assert result.steps == ()


def test_an_empty_probe_is_not_an_all_clear():
    """"Nothing was asked" is not "all present". The stubbed shape every
    report test uses, and a PASS built from it would be an all-clear over an
    empty set."""
    assert checks.check_plugins_present(checks.PluginProbe(),
                                       [_conf_using()]) is None


def test_the_plugin_remedy_lands_in_steps_not_in_the_detail(monkeypatch):
    """`emit_check` reflows the detail to the terminal and prints the steps
    verbatim, because a command folded across two lines is not runnable. A
    remedy written into the prose is a command that stops working on a narrow
    window."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.DEBIAN)
    for result in (checks.check_plugins_present(_probe(**{"LSP PEQ": False}),
                                               [_conf_using()]),
                   checks.check_plugins_present(
                       checks.PluginProbe(has_lv2info=False),
                       [_conf_using()])):
        assert "sudo " not in result.detail, result.detail
        assert any(text.startswith("sudo ") for _style, text in result.steps)


def test_the_environment_lines_are_unchanged_by_the_split():
    """The inventory did not become a diagnosis. `_plugin_presence` renders
    the same fact lines it always did — now off a probe the check shares — and
    the block above the checks stays a listing a reader cross-references."""
    probe = _probe(**{"Calf stereo tools": False})
    assert checks._plugin_presence(probe) == [
        "LSP PEQ: present", "LSP MBC: present", "LSP limiter: present",
        "LSP autogain: present", "Calf bass enhancer: present",
        "Calf stereo tools: MISSING", "LSP filter (virtual-bass): present",
        "Calf saturator (virtual-bass): present"]
    assert checks._plugin_presence(checks.PluginProbe(has_lv2info=False)) == [
        "lv2info not installed — LV2 plugin presence unknown"]
    # No remedy in the inventory: the block is printed verbatim, and a package
    # name here would be a fix the reader meets before the diagnosis.
    assert not any("install" in line.lower()
                   for line in checks._plugin_presence(probe))


def test_the_plugin_list_says_it_is_the_catalogue_not_the_confs_needs(monkeypatch):
    """Unlabelled, the eight `present` rows read as what a conf needs, and the
    LV2 check's "all 3 your conf(s) name" below then looks like a count that
    lost five — both #78-round reviewers called the mismatch a fault. The
    label makes the two numbers answer different questions on the page."""
    monkeypatch.setenv("COLUMNS", "80")
    facts = {"version": "v", "sinks": [],
             "plugins": _probe(**{"Calf stereo tools": False})}
    lines = checks._environment_lines([], [], facts)
    assert ("  Plugins:         every LV2 plugin this tool can use; "
            "a conf may need fewer") in lines
    assert "                   Calf stereo tools: MISSING" in lines
    # An empty probe hangs no rows, so it earns no header either.
    facts["plugins"] = checks.PluginProbe()
    assert not any("Plugins:" in l
                   for l in checks._environment_lines([], [], facts))


def test_the_report_probes_lv2info_once(tmp_path, monkeypatch):
    """Two readers of one answer, not two spawns: the Environment listing and
    the LV2 plugins check. Probing per reader is eight subprocesses paid
    twice, and — worse — two blocks that can disagree."""
    calls = []
    monkeypatch.setattr(checks, "_probe_plugins",
                        lambda: (calls.append(1), _probe())[1])
    monkeypatch.setattr(checks, "_pw_dump", lambda: [])
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")

    _results, _confs, _chains, facts = checks.gather_pw_doctor()
    assert len(calls) == 1
    # ...and the same answer is what the Environment block renders.
    lines = checks._environment_lines([], [], facts)
    assert any("LSP PEQ: present" in line for line in lines)
    assert len(calls) == 1


def test_environment_block_prints_no_bluetooth_address():
    """The `Sinks:` list is every sink in the graph, and the closing line asks
    the reader to paste everything above it into an issue."""
    facts = {"version": "v-test",
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


def test_a_dropped_conf_points_at_the_check_not_at_the_readme(tmp_path):
    """The reader is in a terminal, mid-report, and the fix is now four lines
    below: the LV2 plugins check both names which vendor is missing and prints
    the install command. Sending them to the README instead was a detour away
    from the answer — and both causes have to survive it, because this check
    fires just as often for a PipeWire that hasn't been restarted."""
    result = checks.check_confs_loaded([_conf(tmp_path, "Dolby_Balanced")],
                                       [], dump=[])
    assert "README" not in result.detail
    assert "LV2 plugins and impulse response checks" in result.detail
    # Not a diagnosis of a missing plugin — both causes stay on the line, and
    # neither is ranked: nothing here has counted how often either happens.
    assert "stops the whole conf loading" in result.detail
    assert "restarted" in result.detail
    assert "Usually" not in result.detail
    # The restart is the FAIL's own fix, not just a cause it names — before it
    # carried steps, the only command in a failing report was the closing
    # block's "To remove a chain", and a #78-round reviewer read that as the
    # remedy. Command in steps, never in the wrapping prose.
    assert "systemctl" not in result.detail
    assert any(style == "cta" and text.strip() == conf.PIPEWIRE_RESTART_CMD
               for style, text in result.steps)


def test_the_lv2_check_is_printed_under_the_one_that_names_it(tmp_path,
                                                              monkeypatch):
    """"the LV2 plugins and impulse response checks below" is a direction,
    and the order of the check list is what makes it true — for both names
    the chains-loaded FAIL sends its reader to."""
    monkeypatch.setattr(checks, "_pw_dump", lambda: [])
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))
    monkeypatch.setattr(checks, "_probe_plugins",
                        lambda: checks.PluginProbe(
                            entries=(("LSP PEQ", "http://lsp-plug.in/x", True),)))
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    # A conf on disk that names that plugin: "Chains loaded" needs one to have
    # anything to report, and the LV2 check now judges what the confs ask for,
    # so a conf naming nothing would leave neither in the list this direction
    # is about. Injected rather than written, so the test doesn't depend on
    # spa-json-dump being installed wherever the suite runs.
    conf = checks.InstalledConf(path=tmp_path / "Dolby_Balanced.conf",
                                node_name="effect_input.Dolby_Balanced",
                                plugins=["http://lsp-plug.in/x"],
                                irs=[tmp_path / "Dolby_Balanced.irs"])
    monkeypatch.setattr(checks, "installed_confs", lambda *a, **k: [conf])

    labels = [c.label for c in checks.gather_pw_doctor()[0]]
    assert (labels.index("Chains loaded")
            < labels.index("LV2 plugins")
            < labels.index("Impulse responses"))


# --- A conf we could not read says why --------------------------------------

def test_an_unreadable_conf_says_why_it_could_not_be_read(tmp_path,
                                                          monkeypatch):
    """"unreadable" with no cause sends the reader looking for a damaged file,
    when the usual answer is a tool this machine hasn't got — the same tool
    the converter's own path names and offers a package for. Two answers on
    one machine, and the doctor gave the useless one."""
    path = tmp_path / "Dolby_Balanced.conf"
    path.write_text(conf.CONF_HEADER_MARK + " — see\n# version: vtest\n"
                    "context.modules = []\n")
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)

    parsed = checks.parse_conf(path)
    assert not parsed.readable
    assert parsed.unreadable == checks.NO_SPA_JSON_DUMP
    # The header is read before the tool is needed, so the version survives.
    assert parsed.version == "vtest"

    facts = {"version": "v-test", "sinks": []}
    line = [l for l in checks._environment_lines([parsed], [], facts)
            if "Dolby_Balanced.conf" in l]
    assert line and "unreadable (spa-json-dump not installed)" in line[0]


def test_a_conf_that_could_not_be_parsed_is_not_the_missing_tool(tmp_path,
                                                                 monkeypatch):
    """The two causes take different remedies: one is a package away, the
    other is a conf nothing can make sense of and no install fixes. Reported
    as one string, the doctor would offer a package for a damaged file."""
    path = tmp_path / "Dolby_Balanced.conf"
    path.write_text(conf.CONF_HEADER_MARK + " — see\ncontext.modules = []\n")
    monkeypatch.setattr(checks.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(checks.subprocess, "run",
                        lambda cmd, **kw: SimpleNamespace(stdout="not json"))

    parsed = checks.parse_conf(path)
    assert not parsed.readable
    assert parsed.unreadable and parsed.unreadable != checks.NO_SPA_JSON_DUMP
    # No package for this one, so no check offering one.
    assert checks.check_conf_contents([parsed]) is None

    # And a file that cannot even be opened is a third cause, not either.
    missing = checks.parse_conf(tmp_path / "gone.conf")
    assert not missing.readable
    assert missing.unreadable not in ("", checks.NO_SPA_JSON_DUMP)


def test_confs_nothing_could_read_offer_the_spa_tools_package(tmp_path,
                                                              monkeypatch):
    """A conf whose contents were never read leaves every check that rests on
    them judging what it could see, so the report has to say so rather than
    read as clean."""
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.SUSE)
    blind = _conf(tmp_path, "Dolby_Balanced", readable=False,
                  unreadable=checks.NO_SPA_JSON_DUMP)
    result = checks.check_conf_contents([blind])

    assert result.status == DOCTOR_UNKNOWN
    assert "spa-json-dump" in result.detail
    # openSUSE splits `spa-json-dump` out of the pw-* tools, so this is also
    # the family that catches a remedy built from the wrong package key.
    assert any("pipewire-spa-tools" in text for _style, text in result.steps)
    assert "sudo " not in result.detail

    assert checks.check_conf_contents([]) is None
    assert checks.check_conf_contents([_conf(tmp_path, "OK")]) is None


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
    # Healthy prints: the chains-loaded FAIL sends its reader here to rule
    # this cause out, and until 2026-08 a satisfied check said nothing — the
    # #78-round reviewer concluded impulse files were "never checked".
    result = checks.check_irs_present([_conf(tmp_path, "A", irs=[present])])
    assert result.status == DOCTOR_PASS
    assert result.label == "Impulse responses"
    assert "all 1 impulse file" in result.detail
    result = checks.check_irs_present(
        [_conf(tmp_path, "B", irs=[present, absent])])
    assert result.status == DOCTOR_FAIL
    assert "gone.irs" in result.detail


def test_no_named_irs_is_not_a_verdict(tmp_path):
    """A convolver-less conf (or no conf) leaves nothing to check — a PASS
    would say "all present" about an empty set."""
    assert checks.check_irs_present([]) is None
    assert checks.check_irs_present([_conf(tmp_path, "A", irs=[])]) is None


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


def test_several_missing_targets_are_not_described_as_one_chain(tmp_path):
    """A conf per voicing copied from another machine names several sinks that
    are all gone, and the singular read as one chain with a list of targets."""
    dump = [_speaker_sink(),
            *_smart_chain("A", target="alsa_output.gone_one"),
            *_smart_chain("B", target="alsa_output.gone_two")]
    result = checks.check_targets_exist(checks.live_chains(dump),
                                        checks.sink_names(dump), dump)
    assert result.status == DOCTOR_FAIL
    assert "chains are attached to" in result.detail
    assert "the chain is" not in result.detail
    assert "they never join" in result.detail


# --- Environment checks -----------------------------------------------------

@pytest.mark.parametrize("version,status", [
    (session.Version(reason="wireplumber not found"),      DOCTOR_UNKNOWN),
    (session.Version(text="0.4", parts=(0, 4)),            DOCTOR_FAIL),
    (session.Version(text="0.4.15", parts=(0, 4, 15)),     DOCTOR_FAIL),
    (session.Version(text="0.5", parts=(0, 5)),            DOCTOR_PASS),
    (session.Version(text="0.5.15", parts=(0, 5, 15)),     DOCTOR_PASS),
    (session.Version(text="1.0", parts=(1, 0)),            DOCTOR_PASS),
])
def test_wireplumber_version(version, status):
    assert checks.check_wireplumber(version).status == status
    if not version.ok:
        # The UNKNOWN names the probe's own reason, not a fixed claim about
        # a command that may not even exist on the machine.
        assert checks.check_wireplumber(version).detail.startswith(
            "wireplumber not found, so its version")


@pytest.mark.parametrize("answer,parts,reason", [
    ("wireplumber 0.5.15\n", (0, 5, 15), ""),
    ("wireplumber 0.4\n", (0, 4), ""),
    ("no idea\n", (), "no answer from wireplumber --version"),
])
def test_wireplumber_version_keeps_the_patch_level(monkeypatch, answer,
                                                   parts, reason):
    """The version is pasted into issues as well as compared, and 0.5 reads
    as 0.5.0 — a different build from the 0.5.15 that answered."""
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=answer))
    v = session.wireplumber_version()
    assert (v.parts, v.reason) == (parts, reason)
    monkeypatch.setattr(session.shutil, "which", lambda name: None)
    assert session.wireplumber_version().reason == "wireplumber not found"


def test_pipewire_version_reads_the_running_daemons_core(monkeypatch):
    """`pw-cli info 0` answers for the daemon that is actually running —
    `pipewire --version` is the installed binary's number, the wrong one
    after an upgrade nobody restarted."""
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    core = 'type: PipeWire:Interface:Core/4\n\tversion: "1.6.8"\n\tname: "pipewire-0"\n'
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: core)
    v = session.pipewire_version()
    assert (v.text, v.parts) == ("1.6.8", (1, 6, 8))
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: None)
    assert session.pipewire_version().reason == "no answer from pw-cli"
    monkeypatch.setattr(session.shutil, "which", lambda name: None)
    assert session.pipewire_version().reason == "pw-cli not found"


def test_the_versions_row_leads_the_pipewire_section(monkeypatch):
    """Which server the section describes comes before what it is doing —
    and it moved out of the setup block, where it sat as a WirePlumber row
    only the filter-chain doctor printed."""
    monkeypatch.setenv("COLUMNS", "80")
    lines = checks._pipewire_lines(
        checks.DefaultSink(effective="alsa_output.spk"),
        session.ClockSettings(reason="pw-metadata not found"),
        session.Dropouts(reason="pw-top not found"), None,
        label="",
        pipewire=session.Version(text="1.6.8", parts=(1, 6, 8)),
        wireplumber=session.Version(text="0.5.15", parts=(0, 5, 15)))
    assert lines[0] == ("  Versions:        PipeWire 1.6.8 (running), "
                        "WirePlumber 0.5.15 (installed)")
    assert not any("WirePlumber" in ln
                   for ln in checks._environment_lines([], [], {"sinks": []}))


def test_the_versions_row_says_why_a_version_is_missing(monkeypatch):
    """An unread version prints its bare reason — an absent row and a zero
    look alike in a paste; the daemon question belongs to the PipeWire
    check, asked once."""
    monkeypatch.setenv("COLUMNS", "80")
    def text(pw, wp):
        return " ".join(ln.strip() for ln in doctor_layout.version_rows(
            pw, wp, doctor_layout.GUTTER))
    assert ("PipeWire not read (no answer from pw-cli), WirePlumber 0.5.15 "
            "(installed)") in text(
        session.Version(reason="no answer from pw-cli"),
        session.Version(text="0.5.15", parts=(0, 5, 15)))
    assert ("PipeWire not read (pw-cli not found), WirePlumber not read "
            "(wireplumber not found)") in text(
        session.Version(reason="pw-cli not found"),
        session.Version(reason="wireplumber not found"))


def test_the_remembered_line_prints_only_for_a_name_the_graph_lacks():
    """The remembered pick is worth a line only as a forward-looking fact — a
    dead or absent name WirePlumber will re-apply if it returns. Printed
    whenever it merely differed from the effective default, it sat under the
    ← default arrow contradicting it with no cue that it was a memory: the
    #78-round reviewer read it as the tool rerouting audio to HDMI."""
    hdmi = "alsa_output.hdmi"

    def env(configured):
        facts = {"wireplumber": (0, 5), "version": "0.0.0",
                 "sinks": [SPEAKER, hdmi], "plugins": checks.PluginProbe(),
                 "default": checks.DefaultSink(effective=SPEAKER,
                                               configured=configured)}
        return checks._environment_lines([], [], facts)

    assert not any("Remembered:" in l for l in env(""))
    assert not any("Remembered:" in l for l in env(SPEAKER))
    # In the graph but not effective: anomalous, predicts nothing — no line.
    assert not any("Remembered:" in l for l in env(hdmi))
    line = [l for l in env("effect_input.Gone") if "Remembered:" in l]
    assert line == [
        "  Remembered:      effect_input.Gone (not in the graph)"]


def test_the_setup_block_keeps_the_gutter(tmp_path, monkeypatch):
    """The PW block's mirror of the EE doctor's gutter trap: both print on
    `doctor_layout.GUTTER`, so the two reports' rows sit in one column."""
    monkeypatch.setenv("COLUMNS", "80")
    facts = {"version": "v",
             "sinks": [SPEAKER, "alsa_output.hdmi"],
             "plugins": _probe(**{"Calf stereo tools": False}),
             "default": checks.DefaultSink(effective=SPEAKER,
                                           configured="effect_input.Gone")}
    lines = checks._environment_lines(
        [_conf(tmp_path, "A", version="v1")], [], facts)
    assert_rows_line_up(lines, doctor_layout.GUTTER)


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
    assert result.detail.startswith("1 of 1 conf was written by v1 and "
                                    "this is v2.")
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
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))
    # The probe rather than the renderer: one stub covers both readers
    # of it, and an empty probe is the "nothing was asked" shape — which
    # the LV2 plugins check must not turn into an all-clear.
    monkeypatch.setattr(checks, "_probe_plugins", checks.PluginProbe)
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
    assert "=== PipeWire filter-chain setup ===" in out
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
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))
    monkeypatch.setattr(checks, "_probe_plugins", checks.PluginProbe)
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
    # The setup block sits directly above the checks because the check details
    # name those confs and sinks.
    assert (out.index("=== HARDWARE STUB ===")
            < out.index("=== PipeWire ===")
            < out.index("=== PipeWire filter-chain setup ===")
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
    monkeypatch.setattr(
        session, "wireplumber_version",
        lambda: session.Version(reason="no answer from wireplumber --version"))
    monkeypatch.setattr(
        session, "pipewire_version",
        lambda: session.Version(reason="no answer from pw-cli"))
    monkeypatch.setattr(checks, "_probe_plugins", checks.PluginProbe)
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
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))

    _checks, confs, _chains, _facts = checks.gather_pw_doctor()
    assert [c.path.parent for c in confs] == [scanned]
    # And the stray still gets reported, by the check that owns it.
    assert checks.check_conf_directory().status == DOCTOR_WARN


def test_wireplumber_running_version_comes_off_the_dump():
    """The daemon's own number outranks the installed binary's: after an
    upgrade nobody restarted, the binary PASSes 0.5 while a 0.4 daemon
    ignores filter.smart — the exact failure the check's FAIL describes."""
    daemon = {"type": "PipeWire:Interface:Client",
              "info": {"props": {"application.name": "WirePlumber",
                                 "wireplumber.daemon": True,
                                 "application.version": "0.5.15"}}}
    script = {"type": "PipeWire:Interface:Client",
              "info": {"props": {"application.name": "WirePlumber",
                                 "application.version": "9.9"}}}
    v = checks.wireplumber_running_version([script, daemon])
    assert (v.text, v.parts, v.claim) == ("0.5.15", (0, 5, 15), "running")
    assert checks.wireplumber_running_version([script]) is None
    assert checks.wireplumber_running_version([]) is None
    assert checks.wireplumber_running_version(None) is None


def test_the_versions_are_probed_once_per_run(tmp_path, monkeypatch):
    """"Probe everything once, then judge" — each version probe is a
    subprocess, and the check and the facts dict are two readers of one
    answer, not two spawns. A reason is an answer too: a version the binary
    won't give is not a reason to ask it again."""
    def _run(answer):
        wp_calls, pw_calls = [], []
        monkeypatch.setattr(session, "wireplumber_version",
                            lambda: (wp_calls.append(answer), answer)[1])
        monkeypatch.setattr(
            session, "pipewire_version",
            lambda: (pw_calls.append(1),
                     session.Version(text="1.0", parts=(1, 0)))[1])
        monkeypatch.setattr(checks, "_pw_dump", lambda: [])
        monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
        monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
        results, _confs, _chains, facts = checks.gather_pw_doctor()
        wireplumber = [c for c in results if c.label == "WirePlumber"]
        return wp_calls, pw_calls, wireplumber, facts

    old = session.Version(text="0.4", parts=(0, 4))
    wp_calls, pw_calls, wireplumber, facts = _run(old)
    assert len(wp_calls) == 1 and len(pw_calls) == 1
    # ...and that one answer reaches both consumers, unchanged.
    assert facts["wireplumber_version"] == old
    assert facts["pipewire_version"].text == "1.0"
    assert [c.status for c in wireplumber] == [DOCTOR_FAIL]

    unread = session.Version(reason="wireplumber not found")
    wp_calls, _pw, wireplumber, facts = _run(unread)
    assert len(wp_calls) == 1
    assert facts["wireplumber_version"] == unread
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
    monkeypatch.setattr(ee_socket.subprocess, "run", _no_pgrep)
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
                                 {"version": "0.0-test",
                                  "sinks": []}))
    monkeypatch.setattr(checks, "_probe_plugins", checks.PluginProbe)
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


# --- PipeWire clock / dropouts (lib.pipewire.session) ------------------------
#
# Issue #84: a crackling report whose pasted --doctor output could not say
# what quantum the chain ran at or whether the graph dropped buffers. The
# parsers are exercised on text captured from the real tools (PipeWire 1.6.8),
# preamble and all, so a format drift lands here and not in a user's paste.

PW_METADATA_SETTINGS = """\
Found "settings" metadata 32
update: id:0 key:'log.level' value:'2' type:''
update: id:0 key:'clock.rate' value:'48000' type:''
update: id:0 key:'clock.allowed-rates' value:'[ 48000 ]' type:''
update: id:0 key:'clock.quantum' value:'1024' type:''
update: id:0 key:'clock.min-quantum' value:'32' type:''
update: id:0 key:'clock.max-quantum' value:'2048' type:''
update: id:0 key:'clock.force-quantum' value:'0' type:''
update: id:0 key:'clock.force-rate' value:'0' type:''
"""

# Three batch snapshots. The first is pw-top's pre-info placeholder — every
# state `C`, every ERR 0 (seen live) — and must not be the window's baseline.
# Never-run nodes print `---` for their times, drivers carry a three-token
# FORMAT column, followers a `+` before the name, and the last snapshot's
# easyeffects_sink count has moved on by one.
PW_TOP_BATCH = """\
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
C   91      0      0    ---     ---   ---   ---     0                  alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink
C  119      0      0    ---     ---   ---   ---     0                  easyeffects_sink
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
S  113      0      0    ---     ---   ---   ---     0                  libcamera_input.__SB_.PC00.LNK1
R  160   2048  48000   2.5ms   1.2us  0.06  0.00    8    S24LE 2 48000 alsa_input.usb-R__DE_Microphones_R__DE_VideoMic_NTG_D9D6D10C-00.analog-stereo
R   91      0      0 134.4us 124.4us  0.00  0.00   42    S32LE 2 48000  + alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink
R  119      0      0  81.3us  13.8us  0.00  0.00   26     F32P 2 48000  + easyeffects_sink
R  140      0      0   2.0us 912.6us  0.00  0.02   14                   + ee_soe_multiband_compressor
R  331      0      0  83.9us  56.8us  0.00  0.00    0                   + ee_soe_convolver
R  231   3600  48000 247.3us  18.5us  0.01  0.00    0    F32LE 2 48000  + Firefox
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R  160   2048  48000   2.5ms   1.2us  0.06  0.00    8    S24LE 2 48000 alsa_input.usb-R__DE_Microphones_R__DE_VideoMic_NTG_D9D6D10C-00.analog-stereo
R   91      0      0 134.4us 124.4us  0.00  0.00   42    S32LE 2 48000  + alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Speaker__sink
R  119      0      0  81.3us  13.8us  0.00  0.00   27     F32P 2 48000  + easyeffects_sink
R  140      0      0   2.0us 912.6us  0.00  0.02   14                   + ee_soe_multiband_compressor
"""

_SPEAKER_NODE = ("alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic"
                 ".HiFi__Speaker__sink")


def test_parse_settings_reads_the_clock_keys_past_the_preamble():
    values = session.parse_settings(PW_METADATA_SETTINGS)
    assert values["clock.rate"] == "48000"
    assert values["clock.quantum"] == "1024"
    assert values["clock.min-quantum"] == "32"
    assert values["clock.max-quantum"] == "2048"
    assert values["clock.force-quantum"] == "0"
    assert values["clock.force-rate"] == "0"
    assert session.parse_settings("") == {}
    assert session.parse_settings("Found \"settings\" metadata 32\n") == {}


def test_parse_pwtop_splits_snapshots_and_reads_the_columns_by_position():
    snaps = session.parse_pwtop(PW_TOP_BATCH)
    assert len(snaps) == 3
    assert snaps[0]["easyeffects_sink"][:2] == ("C", 0)            # the placeholder
    assert snaps[1]["easyeffects_sink"][:2] == ("R", 26)
    assert snaps[2]["easyeffects_sink"][:2] == ("R", 27)
    assert snaps[1]["ee_soe_multiband_compressor"][:2] == ("R", 14)
    assert snaps[1][_SPEAKER_NODE][:2] == ("R", 42)               # a FORMAT column shifts nothing
    assert snaps[1]["libcamera_input.__SB_.PC00.LNK1"][:2] == ("S", 0)   # never ran
    assert all("ID" not in s and "NAME" not in s for s in snaps)   # headers split, not kept
    # Drivers carry the clock and are their own driver; followers (`+`) run
    # under the last driver above them, at no clock of their own.
    mic = "alsa_input.usb-R__DE_Microphones_R__DE_VideoMic_NTG_D9D6D10C-00.analog-stereo"
    assert snaps[1][mic].quant == 2048 and snaps[1][mic].rate == 48000
    assert snaps[1][mic].driver == mic
    assert snaps[1][_SPEAKER_NODE].driver == mic and snaps[1][_SPEAKER_NODE].quant == 0
    assert snaps[1]["Firefox"].driver == mic
    assert session.parse_pwtop("") == []


def test_read_xruns_windows_the_counters_from_the_first_real_snapshot(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: PW_TOP_BATCH)
    d = session.read_xruns(sink=_SPEAKER_NODE)
    assert d.ok
    assert (d.sink, d.chain, d.chain_node) == (42, 27, "easyeffects_sink")
    # Growth is measured from the second snapshot: against the placeholder
    # the sink would read as 42 fresh dropouts.
    assert (d.sink_recent, d.chain_recent) == (0, 1)
    assert d.playing                                   # an EasyEffects node ran
    # In this capture the mic drove the clock and the sink followed it, so
    # the sink's ERR is its own and the running clock is the mic's.
    assert not d.sink_is_driver
    assert (d.running_quantum, d.running_rate) == (2048, 48000)
    # With the sink driving (its row unprefixed, carrying the clock), ERR is
    # the whole graph's and the running clock is the sink's own.
    driving = PW_TOP_BATCH.replace("   42    S32LE 2 48000  + alsa_output",
                                   "   42    S32LE 2 48000 alsa_output") \
                          .replace("R   91      0      0", "R   91    256  48000")
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: driving)
    d = session.read_xruns(sink=_SPEAKER_NODE)
    assert d.sink_is_driver and (d.running_quantum, d.running_rate) == (256, 48000)
    # Without the sink named, only EasyEffects' side is reported.
    d = session.read_xruns()
    assert (d.sink, d.sink_recent, d.chain) == (None, None, 27)
    # A filter chain names its nodes exactly instead of by prefix.
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: PW_TOP_BATCH.replace(
        "+ ee_soe_multiband_compressor", "+ effect_output.Dolby_Balanced"))
    d = session.read_xruns(sink=_SPEAKER_NODE, chain_prefixes=(),
                         chain_names=["effect_input.Dolby_Balanced",
                                      "effect_output.Dolby_Balanced"])
    assert (d.chain, d.chain_node) == (14, "effect_output.Dolby_Balanced")
    # Playing is judged on EasyEffects' nodes, not the sink: idle EasyEffects
    # beside a running sink is "nothing was playing".
    idle = PW_TOP_BATCH.replace("R  119", "S  119").replace("R  140", "S  140") \
                       .replace("R  331", "S  331")
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: idle)
    assert not session.read_xruns(sink=_SPEAKER_NODE).playing


def test_read_xruns_says_why_when_it_cannot(monkeypatch):
    """TRAP: an unreadable count must never render as zero — in a pasted
    report the two are indistinguishable, and the reassuring one wins."""
    monkeypatch.setattr(session.shutil, "which", lambda name: None)
    assert session.read_xruns().reason == "pw-top not found"
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: None)
    assert session.read_xruns().reason == "pw-top didn't answer"
    no_ee = "\n".join(
        ln for ln in PW_TOP_BATCH.splitlines()
        if not ln.split()[-1].startswith(session.EASYEFFECTS_NODE_PREFIXES))
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: no_ee)
    assert session.read_xruns().reason == "none of the chain's nodes are in the graph"
    assert session.read_xruns(sink="missing").reason == (
        "neither the output sink nor any of the chain's nodes is in the graph")


def test_process_age_is_read_off_proc_stat_past_the_comm_field():
    # comm may hold spaces and parentheses; field 22 (start time, in clock
    # ticks since boot) is counted from the state field that follows it.
    stat = ("4628 (pipe wire (x)) S 1 4628 4628 0 -1 4194560 12 0 0 0 5 3 0 0 "
            "20 0 3 0 3900 1000000 500 18446744073709551615 1 1 0 0 0 0 0 0 0 "
            "0 0 0 17 3 0 0 0 0 0 0 0 0 0 0 0 0 0")
    assert session.age_from_stat(stat, uptime_s=114442.0, clk_tck=100) == 114403.0
    assert session.age_from_stat("garbage", 10.0, 100) is None
    assert session.age_from_stat(stat, uptime_s=1.0, clk_tck=100) == 0.0   # never negative


def test_format_age_uses_the_two_largest_units():
    assert session.format_age(114403) == "1 d 7 h"
    assert session.format_age(3 * 86400) == "3 d"
    assert session.format_age(2 * 3600 + 5 * 60 + 9) == "2 h 5 min"
    assert session.format_age(3600) == "1 h"
    assert session.format_age(33 * 60 + 40) == "33 min"
    assert session.format_age(48) == "48 s"


def test_read_settings_soft_fails_in_the_reports_own_words(monkeypatch):
    monkeypatch.setattr(session.shutil, "which", lambda name: None)
    assert session.read_settings().reason == "pw-metadata not found"
    monkeypatch.setattr(session.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: "")
    assert session.read_settings().reason == "no answer from pw-metadata"
    monkeypatch.setattr(session, "_run", lambda cmd, timeout=0: PW_METADATA_SETTINGS)
    settings = session.read_settings()
    assert settings.ok
    assert (settings.rate, settings.quantum, settings.min_quantum,
            settings.max_quantum, settings.force_quantum, settings.force_rate
            ) == ("48000", "1024", "32", "2048", "0", "0")


def test_pipewire_section_is_worded_for_a_filter_chain():
    """The section both doctors print: on this path the chain lives inside
    PipeWire, so there is no app uptime to bound the counters, its nodes are
    "the chain's", and the default sink is what "the output sink" means."""
    settings = session.ClockSettings(rate="48000", quantum="1024", min_quantum="32",
                                   max_quantum="2048", force_quantum="0",
                                   force_rate="0")
    d = session.Dropouts(sink=3, chain=1, chain_node="effect_output.Dolby_Balanced",
                       sink_recent=0, chain_recent=0, window_s=5.0, playing=False,
                       sink_is_driver=True, running_quantum=1024, running_rate=48000)
    lines = checks._pipewire_lines(checks.DefaultSink(effective="alsa_output.spk"),
                                   settings, d, pw_age=90061.0)
    text = " ".join(ln.strip() for ln in lines)
    assert lines[0] == "  Output sink:     alsa_output.spk"
    assert "during the check: 48000 Hz, 1024-sample cycles (21.3 ms)" in text
    assert ("3 xruns on the output sink (it drives the clock, so any node's "
            "dropout counts there), 1 on the busiest chain node "
            "(effect_output.Dolby_Balanced) — since each node was created, at "
            "most PipeWire's 1 d 1 h uptime during the check: none in 5 s, "
            "nothing was playing into the chain") in text
    assert "EasyEffects" not in text
    # A Bluetooth default is redacted like every node name in the report.
    lines = checks._pipewire_lines(
        checks.DefaultSink(effective="bluez_output.80_99_E7_E0_8A_23.1"),
        settings, session.Dropouts(reason="pw-top not found"), None)
    assert "80_99" not in " ".join(lines)
    assert any("not read (pw-top not found)" in ln for ln in lines)


def test_the_two_doctors_print_one_output_sink_row():
    """TRAP: the filter-chain doctor once printed the bare node name where
    the EasyEffects doctor printed "description — node" — two renderers for
    one row, and a reader comparing reports saw two different facts. Same
    inputs, same value; only the gutter may differ."""
    from lib.report import doctor_run
    label, node = "Built-in Audio Speaker", "alsa_output.spk"
    pw = checks._pipewire_lines(
        checks.DefaultSink(effective=node),
        session.ClockSettings(reason="pw-metadata not found"),
        session.Dropouts(reason="pw-top not found"), None, label=label)
    ee = doctor_run._environment_lines(
        {"ee_running": True, "rc_present": True, "output_device": node,
         "output_device_source": "live", "output_label": label})
    value = lambda lines: next(ln for ln in lines if "Output sink" in ln
                               ).split(":", 1)[1].strip()
    assert value(pw) == value(ee) == f"{label} — {node}"


def test_the_dropouts_row_counts_a_self_sink_once(monkeypatch):
    """TRAP (/user-review 2026-08-30): with EasyEffects' own sink as the
    default output, "4 xruns on the output sink, 4 on the busiest
    EasyEffects node (easyeffects_sink)" hung two labels on one node and
    read as 8 glitches."""
    monkeypatch.setenv("COLUMNS", "80")
    d = session.Dropouts(sink=4, chain=4, chain_node="easyeffects_sink",
                         sink_recent=3, chain_recent=3, window_s=5.0,
                         playing=True, sink_is_driver=True,
                         sink_is_chain_node=True)
    text = " ".join(ln.strip()
                    for ln in doctor_layout.dropouts_rows(d, None, None, 19))
    assert ("4 xruns on the output sink (easyeffects_sink — itself the "
            "busiest of EasyEffects' nodes)") in text
    assert "4 on the busiest" not in text
    assert "3 on the sink in 5 s" in text and "3 on EasyEffects" not in text


def test_both_doctors_say_which_kind_of_no_sink_it_was(monkeypatch):
    """TRAP: both doctors used to drop the row when they had no name, while
    the Dropouts row below kept referring to "the output sink". Now it
    prints, and "couldn't be read" never wears the words for "there is
    genuinely none"."""
    monkeypatch.setenv("COLUMNS", "80")
    settings = session.ClockSettings(reason="pw-metadata not found")
    d = session.Dropouts(reason="pw-top not found")

    def pw(default):
        return " ".join(ln.strip() for ln in checks._pipewire_lines(
            default, settings, d, None))

    assert ("Output sink:     not read (pw-dump didn't answer)") in pw(
        checks.DefaultSink(reason=checks.NO_DUMP_REASON))
    assert ("Output sink:     none — PipeWire has no default output right "
            "now") in pw(checks.DefaultSink())


def test_gather_labels_the_default_sink_off_its_own_dump(tmp_path, monkeypatch):
    """The description comes from the dump the checks already read, by the
    rule the EasyEffects doctor uses — a Bluetooth sink's user-set name
    included, which is never printed."""
    monkeypatch.setattr(session, "wireplumber_version",
                        lambda: session.Version(text="0.5", parts=(0, 5)))
    monkeypatch.setattr(session, "pipewire_version",
                        lambda: session.Version(text="1.0", parts=(1, 0)))
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    monkeypatch.setattr(checks, "_probe_plugins", lambda: {})
    spk = _node(SPEAKER, **{"media.class": "Audio/Sink",
                            "node.description": "Built-in Audio Speaker"})
    bt = _node(BT_SINK, **{"media.class": "Audio/Sink",
                           "node.description": "Someone's AirPods",
                           "device.api": "bluez5"})

    monkeypatch.setattr(checks, "_pw_dump",
                        lambda: [spk, bt, _default_metadata(effective=SPEAKER)])
    assert checks.gather_pw_doctor()[3]["default_label"] == "Built-in Audio Speaker"
    monkeypatch.setattr(checks, "_pw_dump",
                        lambda: [spk, bt, _default_metadata(effective=BT_SINK)])
    assert checks.gather_pw_doctor()[3]["default_label"] == "Bluetooth output"
    # No default, or no graph at all: an empty label, not a crash.
    monkeypatch.setattr(checks, "_pw_dump", lambda: [spk])
    assert checks.gather_pw_doctor()[3]["default_label"] == ""
    monkeypatch.setattr(checks, "_pw_dump", lambda: None)
    assert checks.gather_pw_doctor()[3]["default_label"] == ""
