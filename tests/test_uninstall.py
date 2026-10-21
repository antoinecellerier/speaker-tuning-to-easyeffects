"""`--uninstall` on both routes: what it removes, and what it must not.

Most of these are survival tests: each names a file a plausible machine has
that looks like ours and is not, or is ours but still in use. `--uninstall`
deletes files, so a test of what it keeps is a test of what it could lose.
"""

import json
import shlex
from pathlib import Path

import pytest

import dolby_to_easyeffects
import dolby_to_pipewire
import ee_to_pipewire
from lib import ee_paths, ee_socket, tool_env, uninstall
from lib.pipewire import checks
from lib.pipewire import conf as pw_conf
from lib.pipewire import uninstall as pw_uninstall
from lib.preset import autoload
from lib.preset import uninstall as ee_uninstall
from tests.conftest import write_synthetic_tuning_xml

STAMP = {"_generator": "dolby_to_easyeffects.py v2026.10"}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A native EasyEffects tree as the default, with no PipeWire confs and
    no Flatpak tree, so every default the uninstall reads is under tmp."""
    base = tmp_path / "ee"
    rc = tmp_path / "config" / "easyeffects" / "db" / "easyeffectsrc"
    for d in ("output", "irs", "autoload/output"):
        (base / d).mkdir(parents=True)
    rc.parent.mkdir(parents=True)
    monkeypatch.setattr(ee_paths, "install_trees",
                        lambda: (("native", base, rc),
                                 ("Flatpak", tmp_path / "nope", tmp_path / "nope.rc")))
    monkeypatch.setattr(ee_paths, "DEFAULT_OUTPUT_DIR", base / "output")
    monkeypatch.setattr(ee_paths, "DEFAULT_IRS_DIR", base / "irs")
    monkeypatch.setattr(ee_paths, "DEFAULT_AUTOLOAD_DIR", base / "autoload" / "output")
    monkeypatch.setattr(ee_paths, "DEFAULT_EASYEFFECTS_RC", rc)
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path / "pw")
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "pw_fc")
    monkeypatch.setattr(uninstall, "trash_available", lambda: False)
    return base


def _preset(tree, name, stamped=True, kernel=None):
    data = dict(STAMP) if stamped else {}
    data["output"] = {"convolver#0": {"kernel-name": kernel or f"{name}-deadbeef"}}
    path = tree / "output" / f"{name}.json"
    path.write_text(json.dumps(data))
    return path


def _irs(tree, stem):
    path = tree / "irs" / f"{stem}.irs"
    path.write_bytes(b"RIFF")
    return path


def _autoload(tree, device, preset):
    path = tree / "autoload" / "output" / f"{device}:route.json"
    path.write_text(json.dumps({"device": device, "device-profile": "route",
                                "preset-name": preset}))
    return path


def _bypass(tree, stamped=True):
    path = tree / "output" / "Nothing.json"
    path.write_text(json.dumps({**(STAMP if stamped else {}),
                                "output": {"plugins_order": []}}))
    return path


def _rc(tree, fallback="Nothing", on=True):
    rc = ee_paths.DEFAULT_EASYEFFECTS_RC
    rc.write_text("[Presets]\nlastLoadedOutputPreset=Other\n\n[Window]\n"
                  f"outputAutoloadingFallbackPreset={fallback}\n"
                  f"outputAutoloadingUsesFallback={'true' if on else 'false'}\n"
                  "someKey=kept\n")
    return rc


def _uninstall(*extra):
    return dolby_to_easyeffects.run_cli(["--uninstall", "--yes", "--no-color", *extra])


def _planned(p):
    return {s.path for s in p.steps if s.path is not None}


def _ee_args(*extra):
    return dolby_to_easyeffects.build_parser().parse_args(["--uninstall", *extra])


# --- What it removes ---------------------------------------------------------

def test_it_removes_the_presets_their_impulses_autoload_and_bypass(tree):
    preset = _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    legacy = _irs(tree, "Dolby-Balanced")
    entry = _autoload(tree, "speaker", "Dolby-Balanced")
    bypass = _bypass(tree)
    rc = _rc(tree)
    assert _uninstall() == 0
    for path in (preset, irs, legacy, entry, bypass):
        assert not path.exists(), path
    text = rc.read_text()
    assert "outputAutoloadingUsesFallback=false" in text
    # The minimal edit: the key and everything else stay.
    assert "outputAutoloadingFallbackPreset=Nothing" in text
    assert "someKey=kept" in text and "lastLoadedOutputPreset=Other" in text


def test_a_resaved_preset_is_removed_and_flagged(tree, capsys):
    """Re-saved from EasyEffects' window: no stamp, our impulse name. Ours by
    the doctor's test, but it may hold the user's edits, so the list says so
    before the reader answers."""
    preset = _preset(tree, "Dolby-Balanced", stamped=False)
    p = ee_uninstall.plan(_ee_args())
    step = next(s for s in p.steps if s.path == preset)
    assert step.note == ee_uninstall.RESAVED_NOTE


def _convolverrc(stem):
    db = ee_paths.DEFAULT_EASYEFFECTS_RC.parent
    (db / "convolverrc").write_text(f"[convolver#0]\nkernelName={stem}\n")


def test_a_convolverrc_reference_goes_once_the_chain_is_emptied(tree, monkeypatch):
    """The switch to an empty preset makes the saved settings' reference the
    uninstall's own to break."""
    _live(monkeypatch, "Dolby-Balanced")
    _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    _convolverrc("Dolby-Balanced-deadbeef")
    assert irs in _planned(ee_uninstall.plan(_ee_args()))


def test_a_convolverrc_reference_keeps_an_impulse_without_the_switch(tree, tmp_path):
    """--output-dir pointed elsewhere: EasyEffects' own preset of the same
    name stays, and its saved settings load this impulse at start."""
    out = tmp_path / "o"
    out.mkdir()
    (out / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    _convolverrc("Dolby-Balanced-deadbeef")
    args = _ee_args("--output-dir", str(out))
    assert irs not in _planned(ee_uninstall.plan(args))


# --- What it must not remove -------------------------------------------------

def test_a_foreign_preset_and_its_impulse_survive(tree):
    mine = tree / "output" / "MyHeadphones.json"
    mine.write_text(json.dumps({"output": {"convolver#0": {"kernel-name": "AutoEq"}}}))
    irs = _irs(tree, "AutoEq")
    _preset(tree, "Dolby-Balanced")
    p = ee_uninstall.plan(_ee_args())
    assert mine not in _planned(p) and irs not in _planned(p)


def test_an_unstamped_nothing_survives_with_its_fallback(tree, capsys):
    """`write_bypass_preset` keeps a `Nothing.json` that was already there,
    and builds from before the stamp (2026-04-22 → 2026-05-28) wrote none:
    either way nothing says it is ours."""
    _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree, stamped=False)
    rc = _rc(tree)
    assert _uninstall() == 0
    assert bypass.exists()
    assert "outputAutoloadingUsesFallback=true" in rc.read_text()
    assert "Nothing.json" in capsys.readouterr().out


def test_a_bypass_a_user_mapping_still_names_survives(tree):
    """The user mapped headphones → Nothing in EasyEffects' window: that is
    their mapping, and removing the preset would break it."""
    _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    headphones = _autoload(tree, "headphones", "Nothing")
    rc = _rc(tree)
    assert _uninstall() == 0
    assert bypass.exists() and headphones.exists()
    assert "outputAutoloadingUsesFallback=true" in rc.read_text()


def test_an_autoload_entry_for_a_foreign_preset_survives(tree):
    _preset(tree, "Dolby-Balanced")
    theirs = _autoload(tree, "headphones", "MyHeadphones")
    assert _uninstall() == 0
    assert theirs.exists()


def test_an_impulse_a_surviving_preset_names_survives(tree, capsys):
    """Saved-as from one of ours under a new name: foreign by the doctor's
    test, and its only impulse is ours."""
    _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    copy = _preset(tree, "MyTweak", stamped=False, kernel="Dolby-Balanced-deadbeef")
    assert _uninstall() == 0
    assert irs.exists() and copy.exists()
    assert "still used by your preset MyTweak" in capsys.readouterr().out


def test_an_impulse_a_no_copy_irs_conf_names_survives(tree, tmp_path):
    pw = tmp_path / "pw"
    pw.mkdir()
    (pw / "chain.conf").write_text(
        f'filename = "{tree}/irs/Dolby-Balanced-deadbeef.irs"\n')
    _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    assert _uninstall() == 0
    assert irs.exists()


def test_a_stamped_preset_pointing_at_a_user_impulse_leaves_it(tree):
    """Only impulse names this tool writes for the preset are candidates,
    never whatever its convolver happens to name."""
    _preset(tree, "Dolby-Balanced", kernel="MyRoomCorrection")
    irs = _irs(tree, "MyRoomCorrection")
    assert _uninstall() == 0
    assert irs.exists()


def test_a_running_easyeffects_still_gets_the_rc_edit(tree, monkeypatch, capsys):
    """EasyEffects saves only the keys it changed this session, so its quit
    leaves this edit alone (r-uninstall-live-state). It reads the file at
    start, which the list says."""
    monkeypatch.setattr(ee_socket, "easyeffects_running", lambda: True)
    _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    rc = _rc(tree)
    assert _uninstall() == 0
    assert "outputAutoloadingUsesFallback=false" in rc.read_text()
    assert not bypass.exists()
    assert "applies when EasyEffects next starts" in capsys.readouterr().out


def test_custom_dirs_replace_the_scan(tree, tmp_path):
    """Only the directories named: a run told --output-dir must not reach
    into the default tree."""
    live = _preset(tree, "Dolby-Balanced")
    out, irs_dir, auto = tmp_path / "o", tmp_path / "i", tmp_path / "a"
    out.mkdir()
    staged = out / "Dolby-Warm.json"
    staged.write_text(json.dumps(STAMP))
    assert _uninstall("--output-dir", str(out), "--irs-dir", str(irs_dir),
                      "--autoload-dir", str(auto)) == 0
    assert live.exists() and not staged.exists()


def test_both_installs_are_scanned(tmp_path, monkeypatch):
    """A user who switched EasyEffects packaging has our files in the tree
    prefer_flatpak no longer picks."""
    native, flatpak = tmp_path / "native", tmp_path / "flatpak"
    for base in (native, flatpak):
        (base / "output").mkdir(parents=True)
        (base / "output" / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    monkeypatch.setattr(ee_paths, "install_trees",
                        lambda: (("native", native, tmp_path / "n.rc"),
                                 ("Flatpak", flatpak, tmp_path / "f.rc")))
    args = dolby_to_easyeffects.build_parser().parse_args(["--uninstall"])
    planned = _planned(ee_uninstall.plan(args))
    assert {native / "output" / "Dolby-Balanced.json",
            flatpak / "output" / "Dolby-Balanced.json"} <= planned


# --- Order and races ---------------------------------------------------------

def test_a_failed_rc_edit_removes_nothing(tree, monkeypatch):
    """The rc goes first: Nothing gone with the fallback still pointing at
    it is the dangling state the order exists to prevent."""
    monkeypatch.setattr(autoload, "unset_autoload_fallback", lambda _p: False)
    preset = _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    _rc(tree)
    assert _uninstall() == 1
    assert preset.exists() and bypass.exists()


def test_a_file_that_stopped_being_ours_is_skipped(tree, capsys):
    """Between the list and the yes, the user re-saved it under our name
    from scratch: re-asked at removal time, it is no longer ours."""
    preset = _preset(tree, "Dolby-Balanced")
    p = ee_uninstall.plan(_ee_args())
    preset.write_text(json.dumps({"output": {}}))
    uninstall.apply(p, trash=False)
    assert preset.exists()
    assert "failed a last check" in capsys.readouterr().out


# --- Live EasyEffects --------------------------------------------------------

def _live(monkeypatch, playing):
    loads = []
    monkeypatch.setattr(ee_socket, "daemon_listening", lambda: True)
    monkeypatch.setattr(ee_socket, "last_loaded_output_preset",
                        lambda: ee_socket.EEReply(playing, True, True))

    def load(name, expect_kernel=None):
        loads.append((name, (ee_paths.DEFAULT_OUTPUT_DIR / f"{name}.json").exists()))
        return ee_socket.LoadResult("loaded", loaded=name)
    monkeypatch.setattr(ee_socket, "load_output_preset", load)
    return loads


def test_a_running_easyeffects_is_switched_to_an_empty_preset_first(tree, monkeypatch):
    """Deleting the files leaves EasyEffects applying the chain from its own
    settings, so the switch comes before anything is removed."""
    loads = _live(monkeypatch, "Dolby-Balanced")
    preset = _preset(tree, "Dolby-Balanced")
    p = ee_uninstall.plan(_ee_args())
    assert p.steps[0].action == "run"
    assert _uninstall() == 0
    # Loaded while its file existed, and that file is gone afterwards.
    assert loads == [(ee_uninstall.EMPTY_PRESET_NAME, True)]
    assert not (tree / "output" / f"{ee_uninstall.EMPTY_PRESET_NAME}.json").exists()
    assert not preset.exists()


def test_our_nothing_is_the_empty_preset_when_it_is_there(tree, monkeypatch):
    loads = _live(monkeypatch, "Dolby-Balanced")
    _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    assert _uninstall() == 0
    assert loads == [("Nothing", True)]
    assert not bypass.exists()


def test_a_running_easyeffects_on_a_foreign_preset_is_left_alone(tree, monkeypatch):
    loads = _live(monkeypatch, "MyHeadphones")
    _preset(tree, "Dolby-Balanced")
    assert _uninstall() == 0
    assert loads == []


def test_an_unreachable_easyeffects_on_ours_gets_a_manual_step(tree, capsys):
    """Flatpak, or not running: its saved settings name our preset, and it
    restores that chain from them."""
    _preset(tree, "Dolby-Balanced")
    rc = _rc(tree)
    rc.write_text(rc.read_text().replace("=Other", "=Dolby-Balanced"))
    assert _uninstall() == 0
    assert "keeps applying the 'Dolby-Balanced' effects" in capsys.readouterr().out


# --- Asking ------------------------------------------------------------------

def test_no_terminal_and_no_yes_changes_nothing(tree, capsys):
    preset = _preset(tree, "Dolby-Balanced")
    assert dolby_to_easyeffects.run_cli(["--uninstall", "--no-color"]) == 1
    assert preset.exists()
    assert "--yes" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "n", "no", EOFError])
def test_anything_but_yes_changes_nothing(tree, monkeypatch, answer):
    preset = _preset(tree, "Dolby-Balanced")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def ask(_prompt):
        if answer is EOFError:
            raise EOFError
        return answer
    monkeypatch.setattr("builtins.input", ask)
    assert dolby_to_easyeffects.run_cli(["--uninstall", "--no-color"]) == 1
    assert preset.exists()


def test_yes_at_the_prompt_removes(tree, monkeypatch):
    preset = _preset(tree, "Dolby-Balanced")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert dolby_to_easyeffects.run_cli(["--uninstall", "--no-color"]) == 0
    assert not preset.exists()


def test_dry_run_changes_nothing(tree):
    preset = _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    rc = _rc(tree)
    before = rc.read_text()
    assert _uninstall("--dry-run") == 0
    assert preset.exists() and bypass.exists() and rc.read_text() == before


def test_nothing_to_remove_asks_nothing(tree, capsys):
    assert dolby_to_easyeffects.run_cli(["--uninstall", "--no-color"]) == 0
    assert "Nothing left to remove" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["x.xml", "--uninstall"],
    ["--uninstall", "--autoload"],
    ["--yes"],
])
def test_flags_that_contradict_uninstall_are_refused(tree, argv):
    with pytest.raises(SystemExit) as exc:
        dolby_to_easyeffects.main(argv)
    assert exc.value.code == 2


# --- Trash -------------------------------------------------------------------

def test_with_gio_files_go_to_the_trash(tree, monkeypatch, capsys):
    monkeypatch.setattr(uninstall, "trash_available", lambda: True)
    calls = []

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def run(argv, **_kw):
        calls.append(argv)
        return Done()
    monkeypatch.setattr(tool_env, "run", run)
    preset = _preset(tree, "Dolby-Balanced")
    assert _uninstall() == 0
    assert calls == [["gio", "trash", "--", str(preset)]]
    # Moved by gio, not unlinked here: the fake moved nothing.
    assert preset.exists()
    assert "Trash" in capsys.readouterr().out


def test_a_failed_trash_is_not_turned_into_a_delete(tree, monkeypatch):
    monkeypatch.setattr(uninstall, "trash_available", lambda: True)

    class Failed:
        returncode, stdout, stderr = 1, "", "no trash here"
    monkeypatch.setattr(tool_env, "run", lambda *_a, **_k: Failed())
    preset = _preset(tree, "Dolby-Balanced")
    assert _uninstall() == 1
    assert preset.exists()


def test_without_gio_the_list_says_permanently(tree, capsys):
    _preset(tree, "Dolby-Balanced")
    _uninstall("--dry-run")
    assert "deleted permanently" in capsys.readouterr().out


# --- Round trip --------------------------------------------------------------

def _files(root: Path) -> set:
    return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}


def test_a_generator_run_then_uninstall_leaves_the_tree_as_found(tree, tmp_path):
    (tree / "output" / "MyHeadphones.json").write_text('{"output": {}}')
    rc = _rc(tree, fallback="", on=False)
    rc_before = rc.read_text()
    before = _files(tree)
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert dolby_to_easyeffects.run_cli(
        [str(xml), "--skip-ee-check", "--no-color"]) == 0
    autoload.write_bypass_preset(tree / "output", "Nothing")
    autoload.set_autoload_fallback(rc, "Nothing")
    assert _files(tree) != before
    assert _uninstall() == 0
    assert _files(tree) == before
    assert autoload.read_ee_rc(rc.read_text())["uses_fallback"] is False
    assert rc_before  # the pre-run file was there to compare against


# --- PipeWire ----------------------------------------------------------------

def _conf(d: Path, name: str, irs: Path | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.conf"
    irs = irs or d / f"{name}.irs"
    path.write_text(f"{pw_conf.CONF_HEADER_MARK}\n# version: v1\n"
                    f'filename = "{irs}"\n')
    return path


def test_pw_removes_our_conf_and_its_copy_only(tree, tmp_path):
    pw = tmp_path / "pw"
    conf = _conf(pw, "Dolby_Balanced")
    copy = pw / "Dolby_Balanced.irs"
    copy.write_bytes(b"RIFF")
    theirs = pw / "mine.conf"
    theirs.write_text("context.modules = []\n")
    p = pw_uninstall.plan(None)
    assert _planned(p) == {conf, copy}


def test_pw_leaves_an_impulse_outside_the_conf_folder(tree, tmp_path):
    """`--no-copy-irs --irs-dir ~/my-irs`: the conf names a file of the
    user's own, which is no copy this tool made."""
    users = tmp_path / "my-irs"
    users.mkdir()
    own = users / "Room.irs"
    own.write_bytes(b"RIFF")
    conf = _conf(tmp_path / "pw", "Dolby_Balanced", irs=own)
    p = pw_uninstall.plan(None)
    assert _planned(p) == {conf}


def test_pw_keeps_a_copy_a_foreign_conf_still_names(tree, tmp_path):
    pw = tmp_path / "pw"
    _conf(pw, "Dolby_Balanced")
    copy = pw / "Dolby_Balanced.irs"
    copy.write_bytes(b"RIFF")
    (pw / "mine.conf").write_text(f'filename = "{copy}"\n')
    p = pw_uninstall.plan(None)
    assert copy not in _planned(p)
    assert any(path == copy for path, _ in p.left)


def test_pw_restarts_filter_chain_for_a_conf_it_reads(tree, tmp_path, monkeypatch):
    _conf(tmp_path / "pw_fc", "Dolby_Balanced")
    ran = []

    class Ok:
        returncode = 0
    monkeypatch.setattr(tool_env, "run", lambda argv, **_k: ran.append(argv) or Ok())
    assert pw_uninstall.run(None, dry_run=False, assume_yes=True, restart=True) == 0
    assert ["systemctl", "--user", "restart", "filter-chain"] in ran
    assert pw_conf.PIPEWIRE_RESTART_CMD.split() in ran


def test_pw_restarts_nothing_when_nothing_was_removed(tree, monkeypatch):
    ran = []
    monkeypatch.setattr(tool_env, "run", lambda argv, **_k: ran.append(argv))
    assert pw_uninstall.run(None, dry_run=False, assume_yes=True, restart=True) == 0
    assert ran == []


def test_ee_to_pipewire_prints_the_restart_rather_than_running_it(tree, tmp_path,
                                                                  monkeypatch, capsys):
    _conf(tmp_path / "pw", "Dolby_Balanced")
    ran = []
    real = tool_env.run

    def run(argv, **kw):
        # git answers --version's question about the checkout, not the machine.
        if argv[0] == "git":
            return real(argv, **kw)
        ran.append(argv)
    monkeypatch.setattr(tool_env, "run", run)
    assert ee_to_pipewire.run_cli(["--uninstall", "--yes", "--no-color"]) == 0
    assert ran == []
    assert pw_conf.PIPEWIRE_RESTART_CMD in capsys.readouterr().out


def test_ee_to_pipewire_refuses_a_preset_with_uninstall(tree):
    with pytest.raises(SystemExit) as exc:
        ee_to_pipewire.main(["x.json", "--uninstall"])
    assert exc.value.code == 2


def test_wrapper_round_trip_empties_its_output_dir(tree, tmp_path, capsys):
    out = tmp_path / "confs"
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert dolby_to_pipewire.main(
        [str(xml), "--output-dir", str(out), "--target-sink", "",
         "--no-activate", "--no-validate", "--no-color"]) == 0
    assert list(out.iterdir())
    capsys.readouterr()
    assert dolby_to_pipewire.main(
        ["--uninstall", "--yes", "--no-activate", "--output-dir", str(out),
         "--no-color"]) == 0
    assert list(out.iterdir()) == []
    # A folder of the reader's own loads nowhere: no restart to ask for.
    assert pw_conf.PIPEWIRE_RESTART_CMD not in capsys.readouterr().out


def test_each_route_points_at_the_other(tree, tmp_path, capsys):
    _conf(tmp_path / "pw", "Dolby_Balanced")
    _preset(tree, "Dolby-Balanced")
    dolby_to_easyeffects.run_cli(["--uninstall", "--dry-run", "--no-color"])
    assert "python3 dolby_to_pipewire.py --uninstall" in capsys.readouterr().out
    ee_to_pipewire.run_cli(["--uninstall", "--dry-run", "--no-color"])
    assert "python3 dolby_to_easyeffects.py --uninstall" in capsys.readouterr().out


# --- Partial failures, races and path spellings ------------------------------

def _failing_removal(monkeypatch, failing: Path):
    """`_remove` fails for one path only, as a refused trash would."""
    real = uninstall._remove
    monkeypatch.setattr(uninstall, "_remove",
                        lambda p, trash: "refused" if p == failing else real(p, trash))


def test_an_impulse_stays_when_its_preset_failed_to_go(tree, monkeypatch):
    """The plan ignores presets it means to remove; the removal must not, or
    a preset that stayed is left convolving nothing."""
    preset = _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    _failing_removal(monkeypatch, preset)
    assert _uninstall() == 1
    assert preset.exists() and irs.exists()


def test_a_copy_stays_when_its_conf_failed_to_go(tree, tmp_path, monkeypatch):
    pw = tmp_path / "pw"
    conf = _conf(pw, "Dolby_Balanced")
    copy = pw / "Dolby_Balanced.irs"
    copy.write_bytes(b"RIFF")
    _failing_removal(monkeypatch, conf)
    assert pw_uninstall.run(None, dry_run=False, assume_yes=True,
                            restart=False) == 1
    assert copy.exists()


def test_a_partial_failure_still_restarts_pipewire(tree, tmp_path, monkeypatch):
    """Two chains gone and one refused: the two must not stay loaded."""
    pw = tmp_path / "pw"
    failing = _conf(pw, "Dolby_Balanced")
    _conf(pw, "Dolby_Warm")
    _failing_removal(monkeypatch, failing)
    ran = []

    class Ok:
        returncode = 0
    monkeypatch.setattr(tool_env, "run", lambda argv, **_k: ran.append(argv) or Ok())
    assert pw_uninstall.run(None, dry_run=False, assume_yes=True, restart=True) == 1
    assert pw_conf.PIPEWIRE_RESTART_CMD.split() in ran


def test_a_custom_irs_dir_still_switches_a_live_easyeffects(tree, tmp_path, monkeypatch):
    """--irs-dir alone leaves the presets in EasyEffects' own tree, which is
    the one a running EasyEffects plays from."""
    monkeypatch.setattr(ee_paths, "install_trees",
                        lambda: (("native", tree, ee_paths.DEFAULT_EASYEFFECTS_RC),))
    loads = _live(monkeypatch, "Dolby-Balanced")
    _preset(tree, "Dolby-Balanced")
    assert _uninstall("--irs-dir", str(tmp_path / "elsewhere")) == 0
    assert loads


def test_the_undo_line_carries_the_custom_dirs(tree, tmp_path, capsys):
    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    out, irs = tmp_path / "out", tmp_path / "irs"
    assert dolby_to_easyeffects.run_cli(
        [str(xml), "--output-dir", str(out), "--irs-dir", str(irs),
         "--skip-ee-check", "--no-color"]) == 0
    lines = capsys.readouterr().out.splitlines()
    at = next(i for i, l in enumerate(lines) if l.startswith("To undo"))
    line = lines[at + 1]          # the command, on a line of its own
    assert f"--output-dir {out}" in line and f"--irs-dir {irs}" in line
    assert "--autoload-dir" not in line


def test_the_pointer_back_sees_a_resaved_preset(tree):
    _preset(tree, "Dolby-Balanced", stamped=False)
    assert ee_uninstall.ours_present()


def test_a_relative_output_dir_names_the_same_directory(tree, tmp_path, monkeypatch):
    conf = _conf(tmp_path / "confs", "Dolby_Balanced")
    monkeypatch.chdir(tmp_path)
    assert _planned(pw_uninstall.plan(Path("confs"))) == {conf}


def test_a_copy_two_confs_name_is_listed_once(tree, tmp_path, monkeypatch):
    """--output renamed a conf whose node name matches another conf's stem."""
    pw = tmp_path / "pw"
    copy = pw / "Dolby_Balanced.irs"
    _conf(pw, "Dolby_Balanced", irs=copy)
    _conf(pw, "Renamed", irs=copy)
    copy.write_bytes(b"RIFF")
    real = checks.parse_conf

    def parsed(path):
        # As spa-json-dump reads it: the renamed conf's node is Dolby_Balanced.
        c = real(path)
        c.node_name, c.irs = "effect_input.Dolby_Balanced", [copy]
        return c
    monkeypatch.setattr(checks, "parse_conf", parsed)
    p = pw_uninstall.plan(None)
    # Both confs name it: one removes it, and it goes once both are gone.
    assert [s.path for s in p.steps].count(copy) == 1


def test_a_user_preset_named_after_its_own_impulse_is_kept(tree):
    """MyRoom.irs imported in EasyEffects and saved as preset MyRoom: the
    doctor's bare-name match calls that ours, which is fine for a check and
    wrong for a delete."""
    preset = _preset(tree, "MyRoom", stamped=False, kernel="MyRoom")
    irs = _irs(tree, "MyRoom")
    assert _uninstall() == 0
    assert preset.exists() and irs.exists()
    assert not ee_uninstall.ours_present()


def test_an_unstamped_preset_does_not_take_a_bare_named_impulse(tree):
    _preset(tree, "Dolby-Balanced", stamped=False)       # re-saved, hashed kernel
    bare = _irs(tree, "Dolby-Balanced")
    hashed = _irs(tree, "Dolby-Balanced-deadbeef")
    p = ee_uninstall.plan(_ee_args())
    assert hashed in _planned(p) and bare not in _planned(p)


def test_dry_run_hand_steps_read_as_after_a_real_run(tree, tmp_path, capsys):
    _conf(tmp_path / "pw", "Dolby_Balanced")
    ee_to_pipewire.run_cli(["--uninstall", "--dry-run", "--no-color"])
    out = capsys.readouterr().out
    assert "After a real run, still to do by hand:" in out
    assert "\nStill to do by hand:" not in out


def test_a_silent_easyeffects_adds_no_step_when_nothing_is_ours(tree, monkeypatch, capsys):
    monkeypatch.setattr(ee_socket, "daemon_listening", lambda: True)
    monkeypatch.setattr(ee_socket, "last_loaded_output_preset",
                        lambda: ee_socket.EEReply("", True, False))
    assert _uninstall() == 0
    assert "didn't say" not in capsys.readouterr().out


def test_a_symlinked_conf_dir_still_finds_the_copy(tree, tmp_path):
    """The conf keeps the path as installed; the uninstall gets the resolved
    spelling of the same directory."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    copy = link / "Dolby_Balanced.irs"
    _conf(link, "Dolby_Balanced", irs=copy)
    copy.write_bytes(b"RIFF")
    assert _planned(pw_uninstall.plan(link)) == {real / "Dolby_Balanced.conf",
                                                 real / "Dolby_Balanced.irs"}


def test_a_file_already_gone_is_not_counted_as_removed(tree, monkeypatch, capsys):
    """Gone is what was asked, but not this run's doing: no gio call, no
    failure, and nothing for finish() to unload."""
    monkeypatch.setattr(uninstall, "trash_available", lambda: True)
    monkeypatch.setattr(tool_env, "run", lambda *_a, **_k: pytest.fail("gio ran"))
    p = uninstall.Plan(steps=[uninstall.Step("Presets", tree / "output" / "gone.json")])
    assert uninstall.apply(p, trash=True) == (0, 0)
    assert "Already gone" in capsys.readouterr().out


def test_a_refused_trash_offers_the_delete(tree, monkeypatch, capsys):
    monkeypatch.setattr(uninstall, "trash_available", lambda: True)

    class Failed:
        returncode, stdout = 1, ""
        stderr = "Trashing on system internal mounts is not supported"
    monkeypatch.setattr(tool_env, "run", lambda *_a, **_k: Failed())
    preset = _preset(tree, "Dolby-Balanced")
    assert _uninstall() == 1
    assert f"rm {shlex.quote(str(preset))}" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--uninstall", "--profile", "music"],
    ["--uninstall", "--disable", "mbc"],
])
def test_ee_refuses_flags_uninstall_would_ignore(tree, argv):
    with pytest.raises(SystemExit) as exc:
        dolby_to_easyeffects.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["--uninstall", "--list"],
    ["--uninstall", "--variant", "all"],
    ["--uninstall", "--all-profiles"],
])
def test_wrapper_refuses_flags_uninstall_would_ignore(tree, argv):
    with pytest.raises(SystemExit) as exc:
        dolby_to_pipewire.main(argv)
    assert exc.value.code == 2


def test_a_flag_added_later_is_refused_until_kept():
    """The allowlist fails closed: nobody has to remember to list a new
    flag as conflicting."""
    import argparse
    parser = argparse.ArgumentParser()
    uninstall.add_args(parser.add_argument, "remove")
    parser.add_argument("--brand-new", default=None)
    args = parser.parse_args(["--uninstall", "--brand-new", "x"])
    with pytest.raises(SystemExit):
        uninstall.requested(parser, args, set())
    assert uninstall.requested(parser, args, {"brand_new"})


@pytest.mark.parametrize("main", [dolby_to_easyeffects.main, ee_to_pipewire.main,
                                  dolby_to_pipewire.main])
def test_uninstall_refuses_another_mode(tree, main):
    with pytest.raises(SystemExit) as exc:
        main(["--uninstall", "--doctor"])
    assert exc.value.code == 2


def test_custom_dirs_leave_the_live_autoload_entries(tree, tmp_path):
    """Presets of the same name in a folder of the reader's own say nothing
    about the entries EasyEffects resolves against its own tree."""
    _preset(tree, "Dolby-Balanced")
    entry = _autoload(tree, "speaker", "Dolby-Balanced")
    out = tmp_path / "o"
    out.mkdir()
    (out / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    assert _uninstall("--output-dir", str(out), "--irs-dir", str(tmp_path / "i")) == 0
    assert entry.exists()


def test_a_staging_copy_is_not_held_by_the_live_chain_of_the_same_name(tree, tmp_path):
    live = tmp_path / "pw"
    _conf(live, "Dolby_Balanced")
    (live / "Dolby_Balanced.irs").write_bytes(b"RIFF")
    staging = tmp_path / "staging"
    conf = _conf(staging, "Dolby_Balanced")
    copy = staging / "Dolby_Balanced.irs"
    copy.write_bytes(b"RIFF")
    assert _planned(pw_uninstall.plan(staging)) == {conf, copy}


def test_a_symlinked_default_dir_does_not_hold_its_own_copies(tree, tmp_path, monkeypatch):
    """The uninstall gets the resolved spelling, the live-dir scan the
    symlinked one: the same conf must not count as a user of its own copy."""
    real = tmp_path / "real"
    link = tmp_path / "link"
    conf = _conf(real, "Dolby_Balanced")
    copy = real / "Dolby_Balanced.irs"
    copy.write_bytes(b"RIFF")
    link.symlink_to(real)
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", link)
    assert _planned(pw_uninstall.plan(real)) == {conf, copy}


def test_another_spelling_of_the_ee_tree_is_still_live(tree, tmp_path, monkeypatch):
    monkeypatch.setattr(ee_paths, "install_trees",
                        lambda: (("native", tree, ee_paths.DEFAULT_EASYEFFECTS_RC),))
    loads = _live(monkeypatch, "Dolby-Balanced")
    _preset(tree, "Dolby-Balanced")
    assert _uninstall("--output-dir", str(tree / "output" / ".." / "output"),
                      "--irs-dir", str(tmp_path / "elsewhere")) == 0
    assert loads


def test_a_failed_switch_keeps_the_impulse_easyeffects_loads(tree, monkeypatch):
    """The saved settings' reference is the uninstall's to break only once
    the chain was emptied; a refused load leaves it naming this file."""
    _live(monkeypatch, "Dolby-Balanced")
    monkeypatch.setattr(ee_socket, "load_output_preset",
                        lambda *_a, **_k: ee_socket.LoadResult("silent"))
    _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    _convolverrc("Dolby-Balanced-deadbeef")
    _uninstall()
    assert irs.exists()


def test_each_install_is_checked_against_its_own_settings(tmp_path, monkeypatch):
    native, flatpak = tmp_path / "native", tmp_path / "flatpak"
    for base in (native, flatpak):
        (base / "output").mkdir(parents=True)
        (base / "irs").mkdir()
    (native / "output" / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    irs = native / "irs" / "Dolby-Balanced-deadbeef.irs"
    irs.write_bytes(b"RIFF")
    native_rc = tmp_path / "n" / "easyeffectsrc"
    native_rc.parent.mkdir()
    (native_rc.parent / "convolverrc").write_text(
        "[convolver#0]\nkernelName=Dolby-Balanced-deadbeef\n")
    monkeypatch.setattr(ee_paths, "install_trees",
                        lambda: (("native", native, native_rc),
                                 ("Flatpak", flatpak, tmp_path / "f" / "easyeffectsrc")))
    monkeypatch.setattr(ee_paths, "DEFAULT_EASYEFFECTS_RC", tmp_path / "f" / "easyeffectsrc")
    monkeypatch.setattr(checks, "DEFAULT_OUTPUT_DIR", tmp_path / "pw")
    monkeypatch.setattr(checks, "_UNSCANNED_CONF_DIR", tmp_path / "pw_fc")
    args = dolby_to_easyeffects.build_parser().parse_args(["--uninstall"])
    assert irs not in _planned(ee_uninstall.plan(args))


def test_a_quoted_filename_with_a_space_is_read_whole(tmp_path):
    conf = tmp_path / "x.conf"
    conf.write_text('config = { filename = "/home/me/My Audio/Dolby_Balanced.irs" }\n')
    assert checks.conf_filenames(conf) == [Path("/home/me/My Audio/Dolby_Balanced.irs")]


def test_a_step_that_raises_is_a_failed_step(capsys):
    def boom():
        raise OSError(30, "Read-only file system")
    p = uninstall.Plan(steps=[uninstall.Step("EasyEffects", None, action="run",
                                             label="switch", do=boom)])
    assert uninstall.apply(p, trash=False) == (0, 1)
    assert "Read-only" in capsys.readouterr().out


def test_the_ee_clause_of_the_restart_line_needs_a_running_easyeffects(
        tree, tmp_path, monkeypatch, capsys):
    """This route's reader usually has no EasyEffects; the clause is only
    true where one runs."""
    _conf(tmp_path / "pw", "Dolby_Balanced")
    pw_uninstall.run(None, dry_run=True, assume_yes=False, restart=True)
    assert "EasyEffects" not in capsys.readouterr().out
    monkeypatch.setattr(ee_socket, "easyeffects_running", lambda: True)
    pw_uninstall.run(None, dry_run=True, assume_yes=False, restart=True)
    assert "A running EasyEffects stops with it" in capsys.readouterr().out


def test_a_finished_run_says_so(tree, capsys):
    _preset(tree, "Dolby-Balanced")
    assert _uninstall() == 0
    assert "Done — 1 file(s) deleted." in capsys.readouterr().out


def test_the_refusal_names_the_way_out(tree, capsys):
    with pytest.raises(SystemExit):
        dolby_to_easyeffects.main(["--uninstall", "--profile", "music"])
    assert "run it without that flag" in capsys.readouterr().err


def test_printed_commands_follow_how_the_script_was_typed(monkeypatch):
    monkeypatch.setattr("sys.argv", ["../repo/dolby_to_easyeffects.py"])
    assert uninstall.command("dolby_to_pipewire.py", "--uninstall") == \
        "python3 ../repo/dolby_to_pipewire.py --uninstall"
    monkeypatch.setattr("sys.argv", ["pytest"])
    assert uninstall.command("dolby_to_pipewire.py") == "python3 dolby_to_pipewire.py"


def test_a_live_autoload_entry_under_a_custom_dir_says_to_delete_it_by_hand(
        tree, tmp_path):
    """No flag reaches it: --autoload-dir naming EasyEffects' own folder
    reads as not given, so pointing at that flag would loop."""
    entry = _autoload(tree, "speaker", "Dolby-Balanced")
    out = tmp_path / "o"
    out.mkdir()
    (out / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    p = ee_uninstall.plan(_ee_args("--output-dir", str(out)))
    why = dict(p.left)[entry]
    assert "delete it by hand" in why and "--autoload-dir" not in why


def test_an_impulse_only_easyeffects_settings_hold_gets_a_hand_step(tree):
    """A later run can't find it: impulses are found through the presets a
    run removes, and those are gone."""
    _preset(tree, "Dolby-Balanced")
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    _convolverrc("Dolby-Balanced-deadbeef")
    p = ee_uninstall.plan(_ee_args())
    assert any(isinstance(a, tuple) and str(irs) in a[1] for a in p.after)


def test_a_preset_elsewhere_is_not_called_yours(tree, tmp_path):
    """Under --output-dir, the preset in EasyEffects' own folder may be this
    tool's from an earlier install there."""
    _preset(tree, "Dolby-Balanced", kernel="Dolby-Balanced-deadbeef")
    out, irs_dir = tmp_path / "o", tree / "irs"
    out.mkdir()
    (out / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    irs = _irs(tree, "Dolby-Balanced-deadbeef")
    p = ee_uninstall.plan(_ee_args("--output-dir", str(out), "--irs-dir", str(irs_dir)))
    why = dict(p.left)[irs]
    assert "the preset Dolby-Balanced in" in why and "your preset" not in why


def test_a_custom_install_says_the_fallback_stays_on(tree, tmp_path):
    """--output-dir X --autoload turned the Fallback on in EasyEffects' own
    settings, pointing at X's Nothing."""
    _rc(tree)
    out = tmp_path / "o"
    out.mkdir()
    (out / "Dolby-Balanced.json").write_text(json.dumps(STAMP))
    (out / "Nothing.json").write_text(json.dumps({**STAMP, "output": {}}))
    p = ee_uninstall.plan(_ee_args("--output-dir", str(out)))
    assert ee_paths.DEFAULT_EASYEFFECTS_RC in dict(p.left)


def test_the_fallback_clause_needs_the_fallback_on(tree):
    _preset(tree, "Dolby-Balanced")
    bypass = _bypass(tree)
    _autoload(tree, "headphones", "Nothing")
    _rc(tree, on=False)
    assert "Fallback" not in dict(ee_uninstall.plan(_ee_args()).left)[bypass]


def test_a_custom_dir_footnote_says_there(tmp_path, capsys):
    from lib.pipewire import install
    conf = tmp_path / "Dolby_Balanced.conf"
    conf.write_text("x")
    install._print_undo([conf])
    assert "Or remove every chain this tool installed there:" in capsys.readouterr().out


def test_every_prompt_shares_one_tty_rule():
    """A TTY test of its own is how two prompts drift on when to ask: only
    lib/console.py's `interactive` may call isatty."""
    root = Path(__file__).resolve().parent.parent
    offenders = [str(p.relative_to(root))
                 for p in [*sorted((root / "lib").rglob("*.py")), *root.glob("*.py")]
                 if p.name != "console.py" and "isatty(" in p.read_text()]
    assert offenders == []
