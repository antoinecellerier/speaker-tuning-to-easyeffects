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

import pytest

import ee_to_pipewire as e
from _doctor import DOCTOR_FAIL, DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN

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


# --- Reading the graph ------------------------------------------------------

def test_live_chains_joins_both_halves():
    dump = [*_smart_chain("Dolby_Balanced"), *_virtual_chain("Other", "sink.x")]
    chains = {c.name: c for c in e.live_chains(dump)}
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
    assert e._target_node_name(raw) == expected


def test_live_chains_ignores_everything_else():
    dump = [_speaker_sink(), _node("Firefox"), {"type": "PipeWire:Interface:Link"}]
    assert e.live_chains(dump) == []


def test_sink_names_only_audio_sinks():
    dump = [_speaker_sink(), _node("Firefox", **{"media.class": "Stream/Output/Audio"})]
    assert e.sink_names(dump) == {SPEAKER}


def test_graph_readers_tolerate_no_daemon():
    assert e.live_chains(None) == []
    assert e.sink_names(None) == set()


# --- Stacked chains: the failure this doctor exists for ---------------------

def test_two_chains_on_one_target_is_a_failure():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Dolby_Warm")]
    result = e.check_stacked_chains(e.live_chains(dump), [])
    assert result.status == DOCTOR_FAIL
    assert "Dolby_Balanced" in result.detail and "Dolby_Warm" in result.detail


def test_one_chain_per_target_is_silent():
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Other", target="alsa_output.hdmi")]
    assert e.check_stacked_chains(e.live_chains(dump), []) is None


def test_virtual_sinks_do_not_count_as_stacked():
    """Without filter.smart, WirePlumber's chaining logic never sees them —
    that is the whole reason --variant all requires --target-sink ''."""
    dump = [_speaker_sink(), *_virtual_chain("A", "s"), *_virtual_chain("B", "s")]
    assert e.check_stacked_chains(e.live_chains(dump), []) is None


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
    result = e.check_unpinned_siblings(e.live_chains(dump))
    assert (result is not None) is flagged
    if flagged:
        assert result.status == DOCTOR_WARN


# --- Conf-vs-graph checks ---------------------------------------------------

def _conf(tmp_path, name, **kw):
    kw.setdefault("node_name", f"effect_input.{name}")
    return e.InstalledConf(path=tmp_path / f"{name}.conf", **kw)


def test_conf_with_no_node_is_a_failure(tmp_path):
    """A missing LSP/Calf plugin makes PipeWire drop the whole file, so the
    conf looks installed and nothing is in the graph."""
    confs = [_conf(tmp_path, "Dolby_Balanced")]
    result = e.check_confs_loaded(confs, [], dump=[])
    assert result.status == DOCTOR_FAIL
    assert "Dolby_Balanced.conf" in result.detail
    assert "plugin" in result.detail


def test_conf_present_in_graph_passes(tmp_path):
    dump = [*_smart_chain("Dolby_Balanced")]
    result = e.check_confs_loaded([_conf(tmp_path, "Dolby_Balanced")],
                                  e.live_chains(dump), dump=dump)
    assert result.status == DOCTOR_PASS


def test_conf_load_is_unknown_without_a_daemon(tmp_path):
    result = e.check_confs_loaded([_conf(tmp_path, "X")], [], dump=None)
    assert result.status == DOCTOR_UNKNOWN


def test_no_confs_is_not_a_load_failure():
    assert e.check_confs_loaded([], [], dump=[]) is None


def test_missing_irs_is_reported_once_per_file(tmp_path):
    """The stereo convolver is two nodes reading one file, so a missing IRS
    would otherwise be counted once per channel."""
    absent = tmp_path / "gone.irs"
    result = e.check_irs_present([_conf(tmp_path, "A", irs=[absent, absent])])
    assert result.detail.count("gone.irs") == 1
    assert result.detail.startswith("1 impulse file")


def test_missing_irs_is_a_failure(tmp_path):
    present, absent = tmp_path / "here.irs", tmp_path / "gone.irs"
    present.write_bytes(b"")
    assert e.check_irs_present([_conf(tmp_path, "A", irs=[present])]) is None
    result = e.check_irs_present([_conf(tmp_path, "B", irs=[present, absent])])
    assert result.status == DOCTOR_FAIL
    assert "gone.irs" in result.detail


def test_target_sink_that_no_longer_exists(tmp_path):
    dump = [_speaker_sink(), *_smart_chain("A", target="alsa_output.vanished")]
    chains = e.live_chains(dump)
    result = e.check_targets_exist(chains, e.sink_names(dump), dump)
    assert result.status == DOCTOR_FAIL
    assert "alsa_output.vanished" in result.detail
    # And is silent when the target is really there.
    dump = [_speaker_sink(), *_smart_chain("A")]
    assert e.check_targets_exist(e.live_chains(dump), e.sink_names(dump),
                                 dump) is None


# --- Environment checks -----------------------------------------------------

@pytest.mark.parametrize("version,status", [
    (None,   DOCTOR_UNKNOWN),
    ((0, 4), DOCTOR_FAIL),      # no smart-filter support at all
    ((0, 5), DOCTOR_PASS),
    ((1, 0), DOCTOR_PASS),
])
def test_wireplumber_version(version, status):
    assert e.check_wireplumber(version).status == status


def test_easyeffects_conflict():
    live = e.live_chains(_smart_chain("Dolby_Balanced"))
    assert e.check_easyeffects_conflict({SPEAKER}, live, dump=[]) is None
    result = e.check_easyeffects_conflict({SPEAKER, "easyeffects_sink"}, live,
                                          dump=[])
    assert result.status == DOCTOR_WARN
    assert "twice" in result.detail
    # No graph, no claim.
    assert e.check_easyeffects_conflict(set(), live, dump=None) is None


def test_easyeffects_alone_is_not_a_conflict():
    """Nothing of ours is installed, so "processed twice" would be false —
    EasyEffects running by itself is just EasyEffects running."""
    assert e.check_easyeffects_conflict({SPEAKER, "easyeffects_sink"}, [],
                                        dump=[]) is None


def test_conf_version_drift(tmp_path):
    same = [_conf(tmp_path, "A", version="v1")]
    assert e.check_conf_versions(same, "v1") is None
    result = e.check_conf_versions(same, "v2")
    assert result.status == DOCTOR_WARN and "v1" in result.detail
    # An unreadable header is not drift.
    assert e.check_conf_versions([_conf(tmp_path, "A")], "v2") is None


def test_conf_in_unread_directory(tmp_path, monkeypatch):
    """filter-chain.conf.d/ is for `pipewire -c`; the running daemon only
    auto-includes pipewire.conf.d/, so a conf there loads for nobody."""
    monkeypatch.setattr(e, "_UNSCANNED_CONF_DIR", tmp_path)
    assert e.check_conf_directory() is None
    (tmp_path / "Dolby_Balanced.conf").write_text(
        e.CONF_HEADER_MARK + " — see\ncontext.modules = []\n")
    result = e.check_conf_directory()
    assert result.status == DOCTOR_WARN
    assert "pipewire.conf.d" in result.detail


# --- Reading our own confs back --------------------------------------------

def test_installed_confs_ignores_foreign_files(tmp_path):
    (tmp_path / "someone_elses.conf").write_text("context.modules = []\n")
    assert e.installed_confs(tmp_path) == []


# --- The report as a whole --------------------------------------------------

def test_doctor_reports_a_stacked_pair(tmp_path, monkeypatch, capsys):
    """End to end over the state that prompted this: two chains on one sink.
    The verdict must not come out clean, and the report has to say how to
    clear it — deleting a file is the whole remedy, and the one step a reader
    can't derive."""
    dump = [_speaker_sink(), *_smart_chain("Dolby_Balanced"),
            *_smart_chain("Dolby_Warm")]
    monkeypatch.setattr(e, "_pw_dump", lambda: dump)
    monkeypatch.setattr(e, "_wireplumber_version", lambda: (0, 5))
    monkeypatch.setattr(e, "_plugin_presence", lambda: [])
    monkeypatch.setattr(e, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(e, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    monkeypatch.setattr(e, "_CONSOLE", None)

    assert e.report_pw_doctor() == 0
    out = capsys.readouterr().out
    assert "Stacked filter chains" in out
    assert "No blocking problems detected." not in out
    assert "systemctl --user restart pipewire" in out
    # The paste block is the point of running this before filing an issue.
    assert "paste this into your issue" in out


def test_doctor_without_a_daemon_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e, "_pw_dump", lambda: None)
    monkeypatch.setattr(e, "_wireplumber_version", lambda: None)
    monkeypatch.setattr(e, "_plugin_presence", lambda: [])
    monkeypatch.setattr(e, "DEFAULT_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(e, "_UNSCANNED_CONF_DIR", tmp_path / "nope")
    monkeypatch.setattr(e, "_CONSOLE", None)

    assert e.report_pw_doctor() == 0
    out = capsys.readouterr().out
    assert "pw-dump didn't answer" in out
    assert "No blocking problems detected." not in out


def test_doctor_needs_no_preset(monkeypatch):
    """--doctor inspects what is installed, so the positional is optional —
    but it stays required for every other mode."""
    monkeypatch.setattr(e, "report_pw_doctor", lambda: 0)
    assert e.main(["--doctor", "--no-color"]) == 0
    with pytest.raises(SystemExit) as excinfo:
        e.main(["--no-color"])
    assert excinfo.value.code == 2


def test_unread_directory_confs_are_not_counted_as_installed(tmp_path,
                                                             monkeypatch):
    """A conf PipeWire never reads is not installed. Counting it inflated the
    conf total and — because a copy shares its node name with the real one —
    let "Chains loaded" pass for a file that had loaded for nobody."""
    scanned, unread = tmp_path / "pipewire.conf.d", tmp_path / "filter-chain.conf.d"
    scanned.mkdir()
    unread.mkdir()
    body = e.CONF_HEADER_MARK + " — see\ncontext.modules = []\n"
    (scanned / "Dolby_Balanced.conf").write_text(body)
    (unread / "Dolby_Balanced.conf").write_text(body)
    monkeypatch.setattr(e, "DEFAULT_OUTPUT_DIR", scanned)
    monkeypatch.setattr(e, "_UNSCANNED_CONF_DIR", unread)
    monkeypatch.setattr(e, "_pw_dump", lambda: [])
    monkeypatch.setattr(e, "_wireplumber_version", lambda: (0, 5))

    _checks, confs, _chains, _facts = e.gather_pw_doctor()
    assert [c.path.parent for c in confs] == [scanned]
    # And the stray still gets reported, by the check that owns it.
    assert e.check_conf_directory().status == DOCTOR_WARN


# --- Warning at the moment it happens ---------------------------------------

def _write_conf(path: Path, target=SPEAKER, node="Dolby_Balanced"):
    path.write_text(
        f'''{e.CONF_HEADER_MARK} — see
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
def test_second_variant_warns_that_it_stacks(tmp_path, monkeypatch, capsys):
    """Trying another voicing is the obvious next step and the run's own copy
    suggests it — but --force only guards one output path, so the second conf
    lands beside the first and WirePlumber runs both in series."""
    monkeypatch.setattr(e, "_CONSOLE", None)
    first, second = tmp_path / "Dolby_Balanced.conf", tmp_path / "Dolby_Warm.conf"
    _write_conf(first)
    _write_conf(second, node="Dolby_Warm")

    e.warn_if_stacked(second, SPEAKER)
    err = capsys.readouterr().err
    assert "Dolby_Balanced.conf" in err
    assert "one after another" in err


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_first_conf_and_virtual_sinks_do_not_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e, "_CONSOLE", None)
    only = tmp_path / "Dolby_Balanced.conf"
    _write_conf(only)
    e.warn_if_stacked(only, SPEAKER)
    assert capsys.readouterr().err == "", "a lone chain stacks with nothing"

    # --target-sink '' emits no smart filter, so several never chain — that is
    # the whole reason --variant all requires it.
    other = tmp_path / "Dolby_Warm.conf"
    _write_conf(other, node="Dolby_Warm")
    e.warn_if_stacked(other, None)
    assert capsys.readouterr().err == ""


@pytest.mark.skipif(shutil.which("spa-json-dump") is None,
                    reason="spa-json-dump not installed")
def test_chains_on_different_sinks_do_not_warn(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(e, "_CONSOLE", None)
    _write_conf(tmp_path / "Dolby_Balanced.conf", target="alsa_output.hdmi")
    second = tmp_path / "Dolby_Warm.conf"
    _write_conf(second, node="Dolby_Warm")
    e.warn_if_stacked(second, SPEAKER)
    assert capsys.readouterr().err == ""


def test_emit_check_wraps_prose_and_leaves_steps_alone(capsys):
    """The printer rule both doctors depend on: a check's prose reflows to the
    terminal, its commands don't. A command folded across two lines is not
    runnable, which is why checks used to point elsewhere for the fix."""
    from _doctor import CheckResult, emit_check

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
    from _doctor import CheckResult, emit_check

    emit_check(CheckResult(DOCTOR_PASS, "Presets", "all load."),
               lambda _style, text: print(text), width=80)
    assert capsys.readouterr().out == "  [PASS] Presets\n         all load.\n"
