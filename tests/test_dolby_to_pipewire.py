"""dolby_to_pipewire.py orchestration coverage.

The wrapper contains no per-XML logic — it forwards to the two converters,
which have their own unit and corpus coverage — so the corpus tier is
deliberately not extended for it. What needs locking down here is the
orchestration contract: which flags reach which step (routing units against
recorders), that the shared-builder argv rebuild round-trips, and that a
full run leaves nothing behind except the conf + .irs (end-to-end against
the synthetic tuning XML, always with --no-activate so pytest never
restarts the developer's PipeWire).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import dolby_to_easyeffects
import dolby_to_pipewire
import ee_to_pipewire
from lib.pipewire import conf as pw_conf
from lib.pipewire import install
from lib import version
from dolby_to_pipewire import main as wrapper_main
from tests.conftest import write_synthetic_tuning_xml
from lib.report import findings as report_findings
from lib.report import messages

SCRIPT = Path(__file__).resolve().parent.parent / "dolby_to_pipewire.py"


# ---------------------------------------------------------------------------
# Argparse smoke tests (subprocess)
# ---------------------------------------------------------------------------

def _run_script(*args, env=None, timeout=120):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_help_exits_cleanly():
    """`--help` smokes the composed parser: shared groups imported from both
    converters plus the wrapper-own variant/activation groups render without
    duplicate-flag errors."""
    result = _run_script("--no-color", "--help")
    assert result.returncode == 0
    for group in ("tuning input", "inspection", "profile selection",
                  "variant", "filter tweaks", "routing", "output",
                  "activation", "general"):
        assert f"{group}:" in result.stdout


def test_bad_variant_is_a_usage_error():
    result = _run_script("--variant", "loud")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert "--help" in result.stderr  # _HelpHintParser nudge


def test_xml_and_windows_are_mutually_exclusive(tmp_path):
    """The wrapper pre-checks the generator's one cross-flag rule so the
    error is framed before any staging happens."""
    fake_xml = tmp_path / "fake.xml"
    fake_xml.write_text("<root/>")
    fake_dir = tmp_path / "winroot"
    fake_dir.mkdir()
    result = _run_script(str(fake_xml), "--windows", str(fake_dir))
    assert result.returncode == 2
    assert "not both" in result.stderr
    assert "--help" in result.stderr


# ---------------------------------------------------------------------------
# rebuild_argv: the generic forwarding built on the shared builders
# ---------------------------------------------------------------------------

def test_rebuild_argv_round_trips_through_the_generator_parser():
    """Wrapper argv → parse → rebuild_argv → parse with the generator's own
    parser must preserve every forwarded value (positional, store, append,
    store_true) while leaving unset flags on the child's defaults."""
    parser, step1_actions, _ = dolby_to_pipewire._compose_parser([])
    args = parser.parse_args([
        "some.xml", "--best-guess", "--endpoint", "headphone",
        "--disable", "mbc", "--disable", "volmax",
        "--enable", "autogain", "--enable", "virtual-bass", "--prefix", "X",
    ])
    child_argv = dolby_to_pipewire.rebuild_argv(step1_actions, args)
    child = dolby_to_easyeffects.build_parser([]).parse_args(child_argv)
    assert child.xml_file == Path("some.xml")
    assert child.best_guess is True
    assert child.endpoint == "headphone"
    assert child.disable == ["mbc", "volmax"]
    assert child.enable == ["autogain", "virtual-bass"]
    assert child.prefix == "X"
    # Untouched flags stay on the generator's defaults, not the wrapper's.
    assert child.mode == "normal"
    assert child.profile is None
    assert child.volmax_slot == "input-gain"


def test_rebuild_argv_forwards_empty_target_sink():
    """`--target-sink ''` (explicit smart-filter opt-out) must survive the
    rebuild — an empty string is not a default and not skippable."""
    parser, _, step2_actions = dolby_to_pipewire._compose_parser([])
    args = parser.parse_args(["--target-sink", ""])
    child_argv = dolby_to_pipewire.rebuild_argv(step2_actions, args)
    assert child_argv == ["--target-sink", ""]


def test_shared_choices_cannot_drift():
    """The wrapper reuses the generator's builders, so its --disable/--enable
    choices are the generator's lists by construction."""
    wrapper_parser = dolby_to_pipewire.build_parser([])
    by_dest = {a.dest: a for g in wrapper_parser._action_groups
               for a in g._group_actions}
    assert list(by_dest["disable"].choices) == \
        list(messages.DISABLEABLE_FILTERS)
    assert list(by_dest["enable"].choices) == \
        list(messages.ENABLEABLE_FILTERS)


# ---------------------------------------------------------------------------
# Flag routing units (recorders in place of the two converters)
# ---------------------------------------------------------------------------

@pytest.fixture
def recorders(monkeypatch):
    """Replace the generator, the converter, and subprocess with recorders.
    The fake generator writes the three variant stubs into the --output-dir
    it was handed, like the real one; the fake pw-cli lists every node."""
    # Warm the git-describe version cache before the recorder hooks in:
    # whichever test runs first in a fresh worker would otherwise record
    # the one-time version lookup and fail the no-subprocess assertions.
    version.get_version()
    calls = SimpleNamespace(step1=[], step2=[], commands=[])

    def fake_run_cli(argv, closing=None, troubleshooting=None, resolved=None,
                     staged=False):
        calls.step1.append(list(argv))
        if resolved is not None:
            # Only the generator resolves the XML (auto-discovery), and the
            # wrapper prints the closing block that names it.
            resolved["xml_path"] = "/tmp/tuning.xml"
        if closing is not None:
            # The real generator hands its findings back for the wrapper to
            # render at the end; one is enough to exercise that path.
            closing.append(dolby_to_easyeffects.Finding(
                slug="unconfirmed-by-ear", kind="ask", detail="x",
                ask="Does the treble sound right?"))
        if troubleshooting is not None:
            # Same for the fix-flags menu: the generator stashes its inputs
            # and the wrapper renders it after [3/3].
            troubleshooting.update(
                findings=list(closing or []),
                filters_by_profile={"volmax": {"dynamic"}},
                enabled_by_flag=frozenset())
        if "--output-dir" in argv:
            out = Path(argv[argv.index("--output-dir") + 1])
            for stem in ("Dolby-Balanced", "Dolby-Detailed", "Dolby-Warm"):
                (out / f"{stem}.json").write_text("{}")
                (out / f"{stem}.irs").write_bytes(b"")
        return 0

    def fake_ee_main(argv, wrapped=False):
        calls.step2.append(list(argv))
        return 0

    def fake_subprocess_run(cmd, **kwargs):
        calls.commands.append(list(cmd))
        return SimpleNamespace(
            returncode=0,
            stdout="Dolby_Balanced Dolby_Detailed Dolby_Warm")

    monkeypatch.setattr(dolby_to_easyeffects, "run_cli", fake_run_cli)
    monkeypatch.setattr(ee_to_pipewire, "main", fake_ee_main)
    # The multi-sink modes resolve a playback pin through this, which reaches
    # the real pw-dump. Stub it so these stay hermetic.
    monkeypatch.setattr(install, "_autodetect_speaker_sink",
                        lambda: ("alsa_output.stub_Speaker__sink", []))
    monkeypatch.setattr(install.subprocess, "run",
                        fake_subprocess_run)
    monkeypatch.setattr(install.shutil, "which",
                        lambda name: f"/usr/bin/{name}")
    return calls


def test_routing_staging_and_skip_flags(recorders):
    """The no-artifacts mechanics: both step-1 dirs point into the same
    staging tempdir (gone once main returns), --skip-ee-check /
    --skip-next-steps are always passed, and the poisoned flags
    (--no-copy-irs, --autoload*) never reach a child."""
    assert wrapper_main(["--no-activate"]) == 0
    (step1,) = recorders.step1
    assert "--skip-ee-check" in step1
    staging = Path(step1[step1.index("--output-dir") + 1])
    assert step1[step1.index("--irs-dir") + 1] == str(staging)
    assert not staging.exists(), "staging tempdir must not outlive main()"

    (step2,) = recorders.step2
    assert Path(step2[0]) == staging / "Dolby-Balanced.json"
    assert step2[step2.index("--irs-dir") + 1] == str(staging)
    assert "--skip-next-steps" in step2
    for argv in recorders.step1 + recorders.step2:
        assert "--no-copy-irs" not in argv
        assert not any(a.startswith("--autoload") for a in argv)


def test_routing_variant_selects_stems(recorders):
    assert wrapper_main(["--variant", "detailed", "--no-activate"]) == 0
    (step2,) = recorders.step2
    assert Path(step2[0]).name == "Dolby-Detailed.json"

    recorders.step2.clear()
    assert wrapper_main(["--variant", "all", "--target-sink", "",
                         "--no-activate"]) == 0
    stems = [Path(argv[0]).name for argv in recorders.step2]
    assert stems == ["Dolby-Balanced.json", "Dolby-Detailed.json",
                     "Dolby-Warm.json"]


# --- One chain per target ---------------------------------------------------
#
# Chains sharing a filter.smart.target are run in SERIES by WirePlumber, not
# offered as alternatives (measured on 0.5.15). So installing more than one
# is only allowed with smart-filter routing off, and then only with the
# playback side pinned — an unpinned virtual sink follows the default sink,
# so choosing one of them routes the others through it.

@pytest.mark.parametrize("flags", [
    ["--variant", "all"],
    ["--all-profiles"],
    ["--variant", "all", "--all-profiles"],
    # An explicit target sink is still smart-filter mode — only '' is opt-out.
    ["--variant", "all", "--target-sink", "alsa_output.whatever"],
])
def test_multi_sink_modes_refuse_smart_filter_routing(recorders, flags, capsys):
    with pytest.raises(SystemExit) as e:
        wrapper_main([*flags, "--no-activate"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "--target-sink ''" in err
    assert "in series" in err, "the error must say what goes wrong, not just no"
    assert recorders.step2 == [], "nothing may be converted after a refusal"


@pytest.mark.parametrize("flags,pinned", [
    (["--variant", "all", "--target-sink", ""], True),
    (["--all-profiles", "--target-sink", ""], True),
    # A single chain needs no pin. Measured on WirePlumber 0.5 (issue #63):
    # unpinned, its playback settled on the speaker sink and stayed there — as
    # the selected output, with the default switched to HDMI, and across a
    # PipeWire restart — identical to the pinned conf in all four states. So it
    # does not "follow the default" at all; there is simply nothing to keep it
    # apart from, which is why pinning was measured and then not shipped.
    ([], False),
    (["--variant", "warm"], False),
])
def test_playback_is_pinned_exactly_when_more_than_one_sink(recorders, flags,
                                                            pinned):
    assert wrapper_main([*flags, "--no-activate"]) == 0
    assert recorders.step2, "expected at least one conversion"
    for argv in recorders.step2:
        assert ("--target-object" in argv) is pinned
        if pinned:
            assert argv[argv.index("--target-object") + 1] == \
                "alsa_output.stub_Speaker__sink"


def test_user_target_object_is_not_doubled(recorders):
    """A user-supplied pin is already in the rebuilt argv; resolving a second
    one would leave the child arguing with itself."""
    assert wrapper_main(["--variant", "all", "--target-sink", "",
                         "--target-object", "mine", "--no-activate"]) == 0
    for argv in recorders.step2:
        assert argv.count("--target-object") == 1
        assert argv[argv.index("--target-object") + 1] == "mine"


def test_undetectable_speaker_sink_fails_before_writing(recorders, monkeypatch):
    monkeypatch.setattr(install, "_autodetect_speaker_sink",
                        lambda: (None, ["two candidates"]))
    assert wrapper_main(["--variant", "all", "--target-sink", "",
                         "--no-activate"]) == 1
    assert recorders.step1 == [], "must fail before generating anything"
    assert recorders.step2 == []


def test_routing_dry_run_reaches_only_step2_and_skips_activation(recorders):
    assert wrapper_main(["--dry-run"]) == 0
    (step1,) = recorders.step1
    assert "--dry-run" not in step1, \
        "the generator's --dry-run writes nothing — the tempdir is the " \
        "no-artifacts mechanism"
    (step2,) = recorders.step2
    assert "--dry-run" in step2
    assert recorders.commands == []


def test_routing_no_activate_suppresses_systemctl(recorders):
    assert wrapper_main(["--no-activate"]) == 0
    assert recorders.commands == []


def test_routing_activation_restarts_once_then_verifies(recorders):
    assert wrapper_main(["--variant", "all", "--target-sink", ""]) == 0
    restarts = [c for c in recorders.commands if c[0] == "systemctl"]
    assert restarts == [["systemctl", "--user", "restart",
                         "pipewire", "pipewire-pulse"]]
    assert any(c[:2] == ["pw-cli", "ls"] for c in recorders.commands)


def test_routing_systemctl_absent_soft_fails(recorders, monkeypatch, capsys):
    """No systemd is a legitimate environment: warn with the manual command
    and exit 0."""
    def raise_missing(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(install.subprocess, "run", raise_missing)
    assert wrapper_main([]) == 0
    out = capsys.readouterr().out
    assert "systemctl not found" in out
    assert "systemctl --user restart pipewire pipewire-pulse" in out


def test_routing_restart_failure_is_an_error(recorders, monkeypatch, capsys):
    monkeypatch.setattr(
        install.subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    assert wrapper_main([]) == 1
    assert "restart failed" in capsys.readouterr().out


def test_routing_missing_sink_after_restart_is_an_error(recorders,
                                                        monkeypatch, capsys):
    """Restart succeeds but the node never appears (classic cause: missing
    LV2 plugins) — surface it instead of reporting success."""
    monkeypatch.setattr(
        install.subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(install.time, "sleep", lambda s: None)
    assert wrapper_main([]) == 1
    out = capsys.readouterr().out
    assert "did not appear" in out
    assert "Plugin dependencies" in out


def test_routing_step2_failure_fails_fast(recorders, monkeypatch):
    monkeypatch.setattr(ee_to_pipewire, "main",
                        lambda argv, wrapped=False:
                            (recorders.step2.append(argv) or 1))
    assert wrapper_main(["--variant", "all", "--target-sink", ""]) == 1
    assert len(recorders.step2) == 1
    assert recorders.commands == []


def test_routing_step1_failure_propagates(recorders, monkeypatch):
    monkeypatch.setattr(dolby_to_easyeffects, "run_cli",
                        lambda argv, closing=None, troubleshooting=None, resolved=None,
                               staged=False: 1)
    assert wrapper_main([]) == 1
    assert recorders.step2 == []


def test_routing_inspection_short_circuits(recorders):
    """--list runs the generator against the real environment (no staging
    dirs) and stops — no conversion, no activation."""
    assert wrapper_main(["--list"]) == 0
    (step1,) = recorders.step1
    assert "--list" in step1
    assert "--output-dir" not in step1
    assert recorders.step2 == []
    assert recorders.commands == []


def test_routing_no_matching_preset_errors(recorders, monkeypatch, capsys):
    """A generator run that emits nothing for the requested variant (curve
    absent from the XML) must fail with a pointer, not convert nothing
    silently."""
    monkeypatch.setattr(dolby_to_easyeffects, "run_cli",
                        lambda argv, closing=None, troubleshooting=None, resolved=None,
                               staged=False: 0)
    assert wrapper_main(["--no-activate"]) == 1
    assert "no Balanced preset was generated" in capsys.readouterr().out
    assert recorders.step2 == []


def test_routing_output_dir_is_forwarded_absolute(recorders):
    """A relative --output-dir must reach the converter absolute, or the
    conf would embed a relative IRS path PipeWire resolves against its own
    CWD."""
    assert wrapper_main(["--output-dir", "rel/confs", "--no-activate"]) == 0
    (step2,) = recorders.step2
    out = Path(step2[step2.index("--output") + 1])
    assert out.is_absolute()
    assert out.name == "Dolby_Balanced.conf"
    assert out.parent.name == "confs"


# ---------------------------------------------------------------------------
# End-to-end (subprocess, synthetic XML, isolated HOME)
# ---------------------------------------------------------------------------

def _run_e2e(tmp_path, *args, env=None):
    """Run the wrapper against the synthetic XML with HOME pointed at an
    empty directory — the no-EE-artifacts claim is asserted against it.
    Always passes --no-activate and --target-sink '' (no pw-dump probe, no
    PipeWire restart on the developer's machine)."""
    home = tmp_path / "home"
    home.mkdir()
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out = tmp_path / "confs"
    result = _run_script(
        str(xml), "--output-dir", str(out), "--target-sink", "",
        "--no-activate", "--no-color", *args,
        env={**os.environ, "HOME": str(home), **(env or {})},
    )
    return result, home, out


def test_e2e_default_writes_only_balanced_pair(tmp_path):
    result, home, out = _run_e2e(tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.iterdir()) == \
        ["Dolby_Balanced.conf", "Dolby_Balanced.irs"]
    conf = (out / "Dolby_Balanced.conf").read_text()
    assert "dolby_to_pipewire-" not in conf, "staging path leaked into conf"
    assert ".local/share/easyeffects" not in conf
    assert str(out / "Dolby_Balanced.irs") in conf
    # The no-EE-artifacts promise: the fake HOME stays untouched.
    assert list(home.iterdir()) == []


def test_e2e_closing_names_the_xml_to_attach(tmp_path):
    """The closing block asks for the tuning XML, and auto-discovery means the
    reader may never have seen its path — so the wrapper has to name the file
    the run actually read, as the generator does on its own."""
    # The attach lines only print when the run raised something worth
    # reporting; the synthetic XML is clean, so force one finding.
    result, _home, _out = _run_e2e(tmp_path,
                                   env={"DEMO_FIRMWARE_GATE": "off"})
    assert result.returncode == 0, result.stderr
    xml = tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml"
    assert f"'{xml.resolve()}'" in result.stdout


def test_e2e_variant_all_writes_three_pairs(tmp_path):
    # --target-object is supplied so the run doesn't probe pw-dump for a
    # speaker sink — there may not be a PipeWire daemon to answer.
    result, _home, out = _run_e2e(tmp_path, "--variant", "all",
                                  "--target-object", "sink.test")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.iterdir()) == [
        "Dolby_Balanced.conf", "Dolby_Balanced.irs",
        "Dolby_Detailed.conf", "Dolby_Detailed.irs",
        "Dolby_Warm.conf", "Dolby_Warm.irs",
    ]
    # The bug this mode exists to avoid: three chains that WirePlumber would
    # run in series. None of them may declare itself a smart filter, and each
    # must name its own playback target rather than following the default.
    for stem in ("Dolby_Balanced", "Dolby_Detailed", "Dolby_Warm"):
        conf = (out / f"{stem}.conf").read_text()
        assert "filter.smart" not in conf
        assert 'target.object = "sink.test"' in conf


def test_e2e_variant_detailed_writes_only_that_pair(tmp_path):
    result, _home, out = _run_e2e(tmp_path, "--variant", "detailed")
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in out.iterdir()) == \
        ["Dolby_Detailed.conf", "Dolby_Detailed.irs"]


def test_e2e_dry_run_writes_nothing_outside_staging(tmp_path):
    """A dry run installs nothing: the --output-dir it was handed is never
    created, HOME stays empty, and no conf is dumped to a stream either
    (--output-dir without --dry-run is how you keep the confs). The run
    still reports itself in full."""
    result, home, out = _run_e2e(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not out.exists()
    assert list(home.iterdir()) == []
    assert pw_conf.CONF_HEADER_MARK not in result.stdout
    assert pw_conf.CONF_HEADER_MARK not in result.stderr
    # All three scripts print through a console on stdout, so a run is one
    # redirectable stream: everything the user reads is on stdout and stderr
    # carries nothing.
    assert "ieq-amount" in result.stdout
    # ...including the closing block we print on the generator's behalf.
    assert report_findings._REPORT_FORM_URL in result.stdout
    assert result.stderr == ""


def test_e2e_next_steps_checklist_is_consolidated(tmp_path):
    """--no-activate prints the wrapper's single manual block; the
    converter's own checklist stays suppressed (--skip-next-steps) so the
    steps appear exactly once."""
    result, _home, _out = _run_e2e(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Restart PipeWire:") == 1
    assert "Next steps:" not in result.stdout


def test_e2e_no_activate_says_how_to_undo(tmp_path):
    """Every other line asks the user to restart their sound server with a
    config they can't read; none said how to get back if it sounds worse or
    PipeWire won't come up."""
    result, _home, out = _run_e2e(tmp_path)
    assert result.returncode == 0, result.stderr
    report = result.stdout
    assert "To undo: rm" in report
    # It must name the file it actually wrote, not a generic path.
    assert str(out) in report
    # ...and the .irs copied beside the conf — undo used to strand one
    # stray file per variant (round-3 review).
    undo_line = next(ln for ln in report.splitlines() if "To undo: rm" in ln)
    assert ".irs'" in undo_line
    assert report.index("To undo: rm") > report.index("Restart PipeWire:")


def test_e2e_report_cta_is_printed_once_and_last(tmp_path):
    """The generator runs at [1/3], so its own closing block would land two
    phases of output above the end. The wrapper suppresses it there and
    prints it after [3/3] instead — exactly once, and last."""
    result, _home, _out = _run_e2e(tmp_path)
    assert result.returncode == 0, result.stderr
    # One stream for both scripts, so "after [3/3]" is an ordering guarantee
    # and not a coincidence of two streams sharing a terminal.
    report = result.stdout
    url = report_findings._REPORT_FORM_URL
    assert report.count(url) == 1
    assert report.index(url) > report.index("[3/3]")


# --- What still has to happen for the chain to be heard ---------------------
#
# With smart-filter routing WirePlumber inserts the chain and nothing is left
# to do. With --target-sink '' the chain is an ordinary output that processes
# nothing until it is selected — a run that stops at "Sink loaded" leaves the
# reader believing it is already working.

def test_smart_filter_run_claims_automatic_pinning(recorders, capsys):
    assert wrapper_main(["--no-activate"]) == 0
    out = capsys.readouterr().out
    assert "pinned to your speakers automatically" in out
    assert "pick it as your output" not in out


def test_virtual_sink_run_says_to_select_it(recorders, capsys):
    assert wrapper_main(["--target-sink", "", "--no-activate"]) == 0
    out = capsys.readouterr().out
    assert "pick it as your output" in out
    assert "pinned to your speakers automatically" not in out, \
        "nothing pins a v1 virtual sink — it is inert until selected"


def test_activation_says_the_restart_stops_easyeffects(recorders, capsys):
    """The restart takes a running EasyEffects down with it, so whatever it
    was applying stops here whether or not the reader acts on the advice."""
    assert wrapper_main([]) == 0
    out = capsys.readouterr().out
    assert "stops it for this session" in out


def test_activated_virtual_sink_run_names_the_sink_to_pick(recorders, capsys):
    assert wrapper_main(["--target-sink", ""]) == 0
    out = capsys.readouterr().out
    assert "pick it as your output" in out
    assert "Dolby_Balanced" in out
