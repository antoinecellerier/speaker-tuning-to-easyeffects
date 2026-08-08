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

import ee_to_pipewire
from lib import console
from lib.pipewire import checks, conf
from lib.doctor import DOCTOR_FAIL, DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN

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
    assert "paste this into your issue" in out
    # ...and `--doctor > report.txt` has to capture all of it. The console and
    # the bare prints here both target stdout, so the report arrives whole with
    # no mechanism holding the two together.
    assert captured.err == ""


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


def test_both_doctors_render_a_check_identically(tmp_path, monkeypatch,
                                                 silence_console, capsys):
    """The two reports must read as one tool, and a check that wraps at one
    measure here and another there is the visible half of that. Same
    CheckResult, same bytes."""
    from lib.doctor import CheckResult
    from lib.report import environment

    check = CheckResult(DOCTOR_WARN, "Gate", "word " * 40,
                        steps=(("dim", "Switch it on:"), ("", ""),
                               ("cta", "systemctl --user restart pipewire")))
    monkeypatch.setattr(console, "_wrap_width", lambda: 72)
    silence_console(console)

    environment.emit_check(check)
    ee_lines = capsys.readouterr().out.splitlines()

    assert _pw_check_block(check, monkeypatch, tmp_path, capsys) == ee_lines
