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
from lib.pipewire import checks
from lib.pipewire import conf as pw_conf
from lib.pipewire import install
from lib import tool_env, version
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


def test_inherited_help_describes_this_surface():
    """The shared builders word their help for the script that owns them.
    --doctor runs the PipeWire doctor here, --all-profiles carries a rule only
    this wrapper enforces, and nothing this run produces is an EasyEffects
    preset — so the borrowed sentences must not say otherwise."""
    by_dest = {a.dest: a for g in dolby_to_pipewire.build_parser([])._action_groups
               for a in g._group_actions}
    doctor_help = by_dest["doctor"].help
    assert "PipeWire filter chain" in doctor_help
    assert "EasyEffects" not in doctor_help
    assert "--target-sink ''" in by_dest["all_profiles"].help
    # The phrase-patching rules silently no-op if the owner rewords the
    # sentence they match, so the rendered result is what is trapped.
    for dest in ("all_profiles", "prefix", "disable", "enable"):
        assert "preset" not in by_dest[dest].help, dest


# ---------------------------------------------------------------------------
# Flag routing units (recorders in place of the two converters)
# ---------------------------------------------------------------------------

@pytest.fixture
def recorders(monkeypatch, tmp_path):
    """Replace the generator, the converter, and subprocess with recorders.
    The fake generator writes the three variant stubs into the --output-dir
    it was handed, like the real one; the fake pw-cli lists every node.
    The install directory is a tmp dir: it is computed at import, so a run
    without --output-dir would otherwise read the real one, and the confs
    on the machine running the suite would decide what these tests see."""
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path / "conf.d")
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
            resolved["tuning_file"] = "/tmp/tuning.xml"
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
    monkeypatch.setattr(tool_env, "run",
                        fake_subprocess_run)
    monkeypatch.setattr(tool_env, "which",
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


def test_the_wrapper_path_can_never_reload_easyeffects(tmp_path, monkeypatch):
    """The generator's end-of-run load into a running EasyEffects must not
    fire for staged presets (deleted when the wrapper returns) — proven on
    the real generator, since the recorder above stubs it out. Even with
    EasyEffects' own dirs pointed at the staging dir, `staged` alone gates
    it; the wrapper's custom dirs gate it a second time."""
    from lib import ee_paths, ee_socket
    from lib.hardware import sinks
    sent = []

    class FakeSock:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def settimeout(self, _t): pass
        def connect(self, _p): pass
        def sendall(self, d): sent.append(d)
        def recv(self, _n): return b"Podcast\n"

    monkeypatch.setattr(ee_socket, "_socket_path", lambda: tmp_path / "sock")
    monkeypatch.setattr(ee_socket.socket, "socket", lambda *a, **k: FakeSock())
    monkeypatch.setattr(ee_paths, "DEFAULT_OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(ee_paths, "DEFAULT_IRS_DIR", tmp_path / "irs")
    monkeypatch.setattr(sinks, "live_default_sink", lambda: "")
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert not dolby_to_easyeffects.main(
        [str(xml), "--skip-ee-check", "--skip-closing", "--no-color"], staged=True)
    assert sent == []


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


def test_all_profiles_still_names_the_voicings_it_dropped(recorders, capsys):
    """--all-profiles is the run that stages every profile of every voicing
    and converts one voicing's worth, so the two it left behind are the least
    visible thing on screen — and were the only run that never said so."""
    assert wrapper_main(["--all-profiles", "--target-sink", "",
                         "--no-activate"]) == 0
    out = capsys.readouterr().out
    assert "The other voicings are not converted for any profile:" in out
    assert "Detailed" in out and "Warm" in out


def test_a_tuning_without_voicings_marks_the_presets_nobody_heard(
        recorders, monkeypatch, capsys):
    """The generator hands back which presets nobody has listened to, and
    the wrapper marks those where it lists what it converts."""
    names = ["Sonitude-Music", "Sonitude-Movie", "Sonitude-Voice"]

    def fake_run_cli(argv, closing=None, troubleshooting=None, resolved=None,
                     staged=False):
        out = Path(argv[argv.index("--output-dir") + 1])
        for stem in names:
            (out / f"{stem}.json").write_text("{}")
            (out / f"{stem}.irs").write_bytes(b"")
        resolved.update(tuning_file="/tmp/SEAPODAT.zip", voicings=False,
                        presets=names, experimental=names[1:])
        if troubleshooting is not None:
            troubleshooting.update(findings=[], filters_by_profile={},
                                   enabled_by_flag=frozenset())
        return 0

    monkeypatch.setattr(dolby_to_easyeffects, "run_cli", fake_run_cli)
    assert wrapper_main(["--all-profiles", "--target-sink", "",
                         "--no-activate"]) == 0
    converting = capsys.readouterr().out.split("[2/3]")[1].split("\n")
    assert [line.strip() for line in converting[1:4]] == [
        "Sonitude-Music",
        "Sonitude-Movie (experimental: nobody has listened to it yet)",
        "Sonitude-Voice (experimental: nobody has listened to it yet)"]
    assert [Path(a[0]).stem for a in recorders.step2] == names


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


# Several virtual-sink chains each cost their full DSP whenever they run, and
# enough of them at once got PipeWire killed (issue #144). The warning is the
# wrapper's to print, once, after its last conf lands: the children each see
# only the confs written before them.

def _fake_v1_confs(monkeypatch, names):
    """Our v1 confs already on disk, as virtual_sink_confs would find them."""
    def found(directory, exclude=()):
        skip = {Path(p).resolve() for p in exclude}
        return [p for p in (Path(directory) / n for n in names)
                if p.resolve() not in skip]
    monkeypatch.setattr(checks, "virtual_sink_confs", found)


def test_multi_chain_run_warns_once_with_its_count(recorders, capsys):
    assert wrapper_main(["--variant", "all", "--target-sink", "",
                         "--no-activate"]) == 0
    out = capsys.readouterr().out
    assert out.count("chains installed as separate outputs") == 1
    assert "makes 3 chains" in out


def test_single_v1_run_counts_the_chains_already_there(recorders, monkeypatch,
                                                       tmp_path, capsys):
    """The reporter's path: a voicing set first, then a profile on its own."""
    _fake_v1_confs(monkeypatch, ["Dolby_Detailed.conf", "Dolby_Warm.conf"])
    assert wrapper_main(["--profile", "music", "--target-sink", "",
                         "--output-dir", str(tmp_path), "--no-activate"]) == 0
    assert "makes 3 chains" in capsys.readouterr().out


def test_rewriting_a_lone_chain_never_counts_itself(recorders, monkeypatch,
                                                    tmp_path, capsys):
    _fake_v1_confs(monkeypatch, ["Dolby_Balanced.conf"])
    assert wrapper_main(["--target-sink", "", "--output-dir", str(tmp_path),
                         "--force", "--no-activate"]) == 0
    assert "separate outputs" not in capsys.readouterr().out


def test_smart_filter_runs_never_count_chains(recorders, monkeypatch, tmp_path,
                                              capsys):
    _fake_v1_confs(monkeypatch, ["Dolby_Warm.conf", "Dolby_Detailed.conf"])
    assert wrapper_main(["--output-dir", str(tmp_path), "--no-activate"]) == 0
    assert "separate outputs" not in capsys.readouterr().out


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
    monkeypatch.setattr(tool_env, "run", raise_missing)
    assert wrapper_main([]) == 0
    out = capsys.readouterr().out
    assert "systemctl not found" in out
    assert "systemctl --user restart pipewire pipewire-pulse" in out


def test_routing_restart_failure_is_an_error(recorders, monkeypatch, capsys):
    monkeypatch.setattr(
        tool_env, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    assert wrapper_main([]) == 1
    assert "restart failed" in capsys.readouterr().out


def _instant_polling(monkeypatch):
    """Let the post-restart verify window run out at once. A no-op sleep alone
    left the loop polling against the real clock for the full six seconds —
    each of these tests burned 6 s of CPU, the fast tier's longest two."""
    now = [0.0]
    monkeypatch.setattr(install.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(install.time, "sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))


def test_a_missing_sink_on_a_pipewire_without_lv2_names_the_loader(
        recorders, monkeypatch, capsys):
    """Issue #123: offering LSP and Calf to a machine whose PipeWire can't
    load LV2 at all is a loop: the reporter installed them, and the chain
    stayed dead. PipeWire's answer comes first when it has one."""
    from lib.pipewire import session
    monkeypatch.setattr(
        tool_env, "run",
        lambda cmd, **kwargs: SimpleNamespace(
            returncode=0, stdout="id 33, type PipeWire:Interface:Node/3\n"))
    line = ("spa.filter-graph: can't load plugin type 'lv2': No such file or "
            "directory")
    monkeypatch.setattr(session, "lv2_loader",
                        lambda: session.Lv2Loader(present=False, line=line))
    _instant_polling(monkeypatch)
    assert wrapper_main([]) == 1
    out = capsys.readouterr().out
    assert "did not appear" in out
    assert "can't load LV2 plugins" in out and line in out
    assert "lsp-plugins-lv2" not in out


def test_routing_missing_sink_after_restart_is_an_error(recorders,
                                                        monkeypatch, capsys):
    """Restart succeeds, pw-cli answers, and our node isn't in the graph — the
    classic missing-LV2-plugin case. Surface it instead of reporting success.

    The listing has to be non-empty: an empty one is the *other* failure (see
    below), and stubbing it empty here let this test pass while the diagnosis
    it checks was being printed on a state that cannot support it.
    """
    monkeypatch.setattr(
        tool_env, "run",
        lambda cmd, **kwargs: SimpleNamespace(
            returncode=0, stdout="id 33, type PipeWire:Interface:Node/3\n"))
    _instant_polling(monkeypatch)
    assert wrapper_main([]) == 1
    out = capsys.readouterr().out
    assert "did not appear" in out
    # The remedy has to be pasteable, not a pointer to a doc section: looking
    # up two package names was the one step in this block a reader could not
    # do from the terminal.
    assert "install lsp-plugins-lv2" in out
    # Issue #71: the conf carries `nofail`, so a chain PipeWire cannot load is
    # skipped rather than fatal. The reader's question at this point is what
    # the absent sink means for them, and until the flag shipped the honest
    # answer could be "no audio at all" — so the message could not say it.
    assert "isn't running" in out, (
        "the failure block never says the generated chain isn't in the path"
    )
    assert "speakers still work" in out, (
        "the failure block never says the machine still plays audio"
    )


def test_undo_is_a_footnote_when_it_worked_and_a_step_when_it_did_not(
        tmp_path, monkeypatch):
    """Same words, two jobs. On a run that worked the way back is a footnote —
    nothing went wrong, and dim is what a footnote looks like here. On a run
    that failed it is the next thing the reader does, and the failure block
    above it is already the loudest thing on screen; leaving the remedy dim
    puts the least-visible styling on the most useful line.
    """
    conf = tmp_path / "Chain.conf"
    conf.write_text("")
    styles: list[tuple[str, str]] = []
    monkeypatch.setattr(install.console, "cprint",
                        lambda style, text="", **k: styles.append((style, text)))

    install._print_undo([conf])
    install._print_undo([conf], style="cta")

    undo = [s for s, t in styles if "To undo: rm" in t]
    assert undo == ["dim", "cta"], undo
    assert str(conf) in "".join(t for _, t in styles)


def test_routing_unreadable_graph_is_not_diagnosed_as_a_dead_chain(
        recorders, monkeypatch, capsys):
    """`pw-cli` timing out marks every node missing, exactly like a chain that
    didn't load — so the confident diagnosis must not fire on it.

    Otherwise a loaded machine that lost one `pw-cli` call gets told its chain
    isn't running and handed a two-package install for a transient read.

    Nor the per-sink "did not appear", which used to print above that
    disclaimer and assert as fact the thing it then said was unknown; nor a
    non-zero exit, which the wrapper renders by promoting "To undo: rm …" to
    the loudest line on screen — over a chain that is probably running.
    """
    monkeypatch.setattr(
        tool_env, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=""))
    _instant_polling(monkeypatch)
    assert wrapper_main([]) == 0
    out = capsys.readouterr().out
    assert "couldn't be checked" in out
    assert "isn't running" not in out, "diagnosed a chain we never looked at"
    assert "did not appear" not in out, "asserted an absence it hadn't read"
    assert "install lsp-plugins-lv2" not in out


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
        env={**{k: v for k, v in os.environ.items()
                if k not in ("XDG_DATA_HOME", "XDG_CONFIG_HOME")},
             # Not inherited: with either set, the defaults leave `home` and
             # the no-EE-artifacts assertion below would be checking an empty
             # directory the run never had a reason to touch.
             "HOME": str(home), **(env or {})},
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


def test_a_chain_without_lv2_is_not_blamed_on_the_lv2_loader(monkeypatch,
                                                              capsys, tmp_path):
    """A convolver-only conf has no LV2 node, so a missing loader can't be why
    its sink is absent; naming it would send the reader to a package that
    changes nothing."""
    from lib.pipewire import session
    conf_path = tmp_path / "Conv.conf"
    conf_path.write_text('nodes = [ { type = "builtin" label = "convolver" } ]')
    assert install._confs_use_lv2([conf_path]) is False
    monkeypatch.setattr(tool_env, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        tool_env, "run",
        lambda cmd, **kwargs: SimpleNamespace(
            returncode=0, stdout="id 33, type PipeWire:Interface:Node/3\n"))
    monkeypatch.setattr(session, "lv2_loader",
                        lambda: pytest.fail("asked about LV2 for no LV2 node"))
    _instant_polling(monkeypatch)
    assert install._verify_sinks(["Conv"], uses_lv2=False) == 1
    assert "can't load LV2 plugins" not in capsys.readouterr().out


def test_the_lv2_marker_matches_what_the_conf_writer_emits():
    """`_confs_use_lv2` reads the confs this run wrote by their text, so it
    must match the writer's own rendering of an LV2 node."""
    from lib.pipewire.plugins import Stage
    node = {"type": "lv2", "name": "peq",
            "plugin": "http://lsp-plug.in/plugins/lv2/para_equalizer_x16_lr"}
    stage = Stage(nodes=[node], in_l=("peq", "in_l"), in_r=("peq", "in_r"),
                  out_l=("peq", "out_l"), out_r=("peq", "out_r"))
    text = pw_conf.format_conf([stage], [], "X", "X")
    assert install._LV2_NODE in text


def _install_graph(levels, linked=True):
    """A graph after install: each chain's sink at its (linear, mute), smart
    chains attached to the speaker, linked once WirePlumber has handled them."""
    dump = [{"id": 1, "type": "PipeWire:Interface:Node", "info": {
        "props": {"node.name": "alsa_output.spk", "media.class": "Audio/Sink"}}}]
    for i, (name, (linear, mute, smart)) in enumerate(levels.items()):
        capture = {"node.name": f"effect_input.{name}",
                   "media.class": "Audio/Sink"}
        if smart:
            capture.update({"filter.smart": True,
                            "filter.smart.target": '{ node.name = "alsa_output.spk" }'})
        out_id = 100 + i
        dump += [
            {"id": 200 + i, "type": "PipeWire:Interface:Node", "info": {
                "props": capture,
                "params": {"Props": [{"channelVolumes": [linear, linear],
                                      "mute": mute}]}}},
            {"id": out_id, "type": "PipeWire:Interface:Node", "info": {
                "props": {"node.name": f"effect_output.{name}"}}}]
        if linked:
            dump.append({"type": "PipeWire:Interface:Link",
                         "info": {"output-node-id": out_id, "input-node-id": 1}})
    return dump


def test_install_says_when_a_chain_comes_up_muted(monkeypatch, capsys):
    """Issue #152: "Sink loaded", then silence — the new sink had come up
    muted, which a sink takes from whatever WirePlumber restores for its
    name. The run that installed it is the one place sure to be read, and
    the symptom leads."""
    monkeypatch.setattr(checks, "_pw_dump", lambda: _install_graph({
        "Dolby_Balanced": (1.0, True, True),
        "Dolby_Music": (1.0, False, True)}))
    install._warn_chain_levels(["Dolby_Balanced", "Dolby_Music"], timeout=0)
    out = " ".join(capsys.readouterr().out.split())
    assert out.startswith("Your speakers will stay silent: "
                          "effect_input.Dolby_Balanced")
    assert "pactl set-sink-mute effect_input.Dolby_Balanced 0" in out
    assert "set-sink-volume" not in out
    assert "Dolby_Music" not in out


def test_install_leaves_a_v1_chains_level_alone(monkeypatch, capsys):
    """A v1 chain is the reader's own volume control: restored at 40 % is how
    loud they had it, as --doctor treats it. Its mute is still worth saying."""
    monkeypatch.setattr(checks, "_pw_dump", lambda: _install_graph({
        "Quiet": (0.064, False, False), "Muted": (1.0, True, False)}))
    install._warn_chain_levels(["Quiet", "Muted"], timeout=0)
    out = " ".join(capsys.readouterr().out.split())
    assert "effect_input.Quiet" not in out
    assert "effect_input.Muted, the output this run installed, started muted" in out


def test_install_reads_the_level_once_wireplumber_has_linked_the_chain(
        monkeypatch, capsys):
    """WirePlumber's restore lands after the node is listed: a read on first
    sight finds the sink not yet muted and says nothing. The link is the sign
    it has handled the node, so the read waits for it."""
    reads = iter([_install_graph({"Dolby_Balanced": (1.0, False, True)}, linked=False),
                  _install_graph({"Dolby_Balanced": (1.0, True, True)}, linked=False),
                  _install_graph({"Dolby_Balanced": (1.0, True, True)})])
    monkeypatch.setattr(checks, "_pw_dump", lambda: next(reads))
    install._warn_chain_levels(["Dolby_Balanced"], interval=0)
    assert "started muted" in capsys.readouterr().out
