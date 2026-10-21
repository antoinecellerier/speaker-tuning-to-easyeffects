"""``dolby_to_easyeffects.py --uninstall``: what the EasyEffects route installed.

Builds the `lib.uninstall.Plan` the shared runner lists, asks about and
carries out. Every removal rests on an ownership test that already exists,
never on a name that merely looks right:

- **Presets**: the ``_generator`` stamp, or a convolver naming the hashed
  impulse name this tool writes: the doctor's `is_generated_preset` without
  its bare-name case (`_ours` says why). One that matches by impulse only
  was re-saved from EasyEffects' window and may hold the user's edits, so
  the list says so.
- **The `Nothing` bypass**: the stamp alone. The doctor counts any
  ``Nothing.json`` as ours by name, but ``write_bypass_preset`` keeps one
  that was already there, so an unstamped one may be the user's. One a
  surviving autoload entry names is the user's mapping and stays too.
- **Impulse files**: ``kernel_belongs_to`` against a preset being removed,
  and no surviving preset or PipeWire conf naming it
  (`lib/preset/impulses.py`).
- **Autoload entries**: the preset they name is one being removed. They
  carry no stamp, so that reference is the only evidence.
- **The Fallback Preset setting**: on, pointing at the ``Nothing`` being
  removed.

Deleting the files does not stop EasyEffects processing. It keeps the
active chain in its own settings, not in the preset file: even its own
``Manager::remove`` resets only ``lastLoadedOutputPreset``
(upstream ``src/presets_manager.cpp``). So a running EasyEffects that is
playing one of ours is first switched to an empty preset over its socket,
and one this run can't reach is left as a step for the reader. What was
observed live: research `r-uninstall-live-state`.
"""

from __future__ import annotations

import glob
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from lib import doctor, ee_paths, ee_socket, uninstall
from lib.preset import autoload, impulses
from lib.uninstall import Plan, Step

# The empty preset a running EasyEffects is switched to when no `Nothing` of
# ours is there to load. EasyEffects shows the loaded preset's name in its
# window, so the name says what happened. Written, loaded and deleted in one
# step: a load reads the file once into EasyEffects' own settings.
EMPTY_PRESET_NAME = "Speaker-Tuning-Removed"

RESAVED_NOTE = "re-saved in EasyEffects, so it may hold your own edits"


@dataclass
class _Tree:
    """One EasyEffects tree to look in. ``rc`` is the settings file whose
    Fallback Preset points into it, or None when it is not a live tree."""
    output_dir: Path
    irs_dir: Path
    autoload_dir: Path | None
    rc: Path | None
    live: bool = False          # the tree a reachable EasyEffects reads
    presets: list = field(default_factory=list)   # (path, resaved)
    bypass: Path | None = None
    bypass_unstamped: Path | None = None


def _trees(args) -> list[_Tree]:
    """The trees to look in: exactly the ones named, or every install's.

    Without overrides, both installs: ``prefer_flatpak`` picks one per run,
    and a user who switched EasyEffects packaging has our files in the other.
    """
    output_dir = _resolved(args.output_dir)
    autoload_named = not _same(args.autoload_dir, ee_paths.DEFAULT_AUTOLOAD_DIR)
    if (not _same(output_dir, ee_paths.DEFAULT_OUTPUT_DIR)
            or not _same(args.irs_dir, ee_paths.DEFAULT_IRS_DIR)
            or autoload_named):
        in_ee_tree = _same(output_dir, ee_paths.DEFAULT_OUTPUT_DIR)
        rc = ee_paths.DEFAULT_EASYEFFECTS_RC if in_ee_tree else None
        native = ee_paths.install_trees()[0][1] / "output"
        # An autoload entry names a preset EasyEffects looks up in its own
        # tree. So EasyEffects' autoload folder is in scope only with that
        # tree, or when the reader named the folder: presets of the same
        # name in a folder of their own say nothing about those entries.
        autoload_dir = (_resolved(args.autoload_dir)
                        if autoload_named or in_ee_tree else None)
        return [_Tree(output_dir, _resolved(args.irs_dir), autoload_dir, rc,
                      live=_same(output_dir, native))]
    return [_Tree(base / "output", base / "irs", base / "autoload" / "output",
                  rc, live=(label == "native"))
            for label, base, rc in ee_paths.install_trees() if base.exists()]


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _same(a: Path, b: Path) -> bool:
    """One directory however it is spelled: relative, ``~``, ``..`` or
    through a symlink. A raw compare would treat another spelling of
    EasyEffects' own folder as a folder of the reader's."""
    return _resolved(a) == _resolved(b)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _ours(path: Path) -> bool:
    """The preset test, re-askable at removal time.

    Stricter than the doctor's `is_generated_preset`, which also accepts a
    convolver naming the preset's own bare name (the legacy unhashed
    impulse). A user who imports ``MyRoom.irs`` and saves a preset called
    ``MyRoom`` matches that, and a test that only decides what to *check*
    can afford it; one that decides what to delete can't. So without the
    stamp, only the hashed name ``kernel_name`` writes counts.
    """
    data = _read_json(path)
    return _stamped_data(data) or _hashed_kernel(data, path.stem)


def _stamped(path: Path) -> bool:
    return _stamped_data(_read_json(path))


def _stamped_data(data) -> bool:
    return bool(autoload.generator_version(data))


def _hashed_kernel(data, preset: str) -> bool:
    output = data.get("output") if isinstance(data, dict) else None
    if not isinstance(output, dict):
        return False
    hashed = re.compile(rf"{re.escape(preset)}-[0-9a-f]{{8}}")
    return any(key.startswith("convolver#") and isinstance(block, dict)
               and hashed.fullmatch(str(block.get("kernel-name", "")))
               for key, block in output.items())


def _find(tree: _Tree) -> None:
    try:
        paths = sorted(tree.output_dir.glob("*.json"))
    except OSError:
        return
    for path in paths:
        data = _read_json(path)
        stamped = _stamped_data(data)
        if path.stem == autoload.BYPASS_PRESET_NAME:
            if stamped:
                tree.bypass = path
            else:
                tree.bypass_unstamped = path
        elif stamped or _hashed_kernel(data, path.stem):
            tree.presets.append((path, not stamped))


def plan(args) -> Plan:
    """Everything ``--uninstall`` would do with these arguments, doing none
    of it."""
    trees = _trees(args)
    for tree in trees:
        _find(tree)
    removing = {path for tree in trees for path, _ in tree.presets}
    # Only a definite yes: the note it gates says when an edit applies.
    running = ee_socket.easyeffects_running() is True
    result = Plan()
    live, rc_steps, entry_steps, preset_steps, irs_steps = [], [], [], [], []
    for tree in trees:
        names = {path.stem for path, _ in tree.presets}
        rc = _read_rc(tree.rc)

        bypass_users = []
        entries = (autoload.autoload_files(tree.autoload_dir)
                   if tree.autoload_dir is not None else [])
        for path, entry in entries:
            name = entry.get("preset-name")
            if name in names:
                entry_steps.append(Step(
                    "Automatic preset switching (autoload)", path,
                    note=f"it made this output load {name}",
                    recheck=lambda p=path, n=name: _names(p, n)))
            elif name == autoload.BYPASS_PRESET_NAME:
                bypass_users.append(path)

        if tree.autoload_dir is None and names:
            # --output-dir pointed elsewhere, but --autoload still wrote
            # into EasyEffects' own folder. Those entries name presets
            # EasyEffects looks up in its own tree, so they aren't removed
            # on a name match here; listed, so the run doesn't read as clean.
            for p, entry in autoload.autoload_files(ee_paths.DEFAULT_AUTOLOAD_DIR):
                if entry.get("preset-name") in names:
                    # No flag reaches it: --autoload-dir naming EasyEffects'
                    # own folder reads as not given. So the hand step is the
                    # remedy, said plainly.
                    result.left.append((p, (
                        "EasyEffects' own autoload entry for a preset of that "
                        "name; delete it by hand if you no longer want it")))

        for path, resaved in tree.presets:
            preset_steps.append(Step(
                "Presets", path, note=RESAVED_NOTE if resaved else "",
                recheck=lambda p=path: _ours(p)))

        fallback_ours = tree.rc is not None and _fallback_on(tree.rc)
        bypass_going = tree.bypass is not None and not bypass_users
        if tree.bypass is not None and bypass_users:
            result.left.append((tree.bypass, (
                f"still the autoload preset for {len(bypass_users)} "
                "output(s) of yours, so it stays"
                + (", with the Fallback Preset setting" if fallback_ours
                   else ""))))
        elif tree.bypass is not None:
            preset_steps.append(Step(
                "Presets", tree.bypass,
                note=("the empty preset --autoload adds for outputs with "
                      "no preset of their own"),
                recheck=lambda p=tree.bypass: _stamped(p)))
            if fallback_ours:
                # Safe while EasyEffects runs: it saves through KConfig, which
                # writes only the keys it changed this session, merged into
                # the file, so its quit leaves this edit alone
                # (r-uninstall-live-state). It reads the file only at start;
                # until then the setting names the removed preset, and a
                # missing preset file loads nothing.
                rc_steps.append(Step(
                    "EasyEffects settings", tree.rc, action="edit",
                    label=(f"{doctor.tilde(tree.rc)}: turn off the Fallback "
                           "Preset that --autoload turned on"),
                    note=("applies when EasyEffects next starts" if running
                          else ""),
                    recheck=lambda p=tree.rc: _fallback_on(p),
                    do=lambda p=tree.rc: autoload.unset_autoload_fallback(p),
                    required=True))
        if (tree.rc is None and bypass_going
                and _fallback_on(ee_paths.DEFAULT_EASYEFFECTS_RC)
                and not (ee_paths.DEFAULT_OUTPUT_DIR
                         / f"{autoload.BYPASS_PRESET_NAME}.json").exists()):
            # --output-dir X --autoload turned the Fallback on in
            # EasyEffects' own settings, pointing at a Nothing that lives in
            # X. This tree has no settings file of its own to edit, so say so
            # rather than leave a setting nobody is told about.
            result.left.append((ee_paths.DEFAULT_EASYEFFECTS_RC, (
                "its Fallback Preset still points at the removed "
                f"{autoload.BYPASS_PRESET_NAME} preset; turn it off in "
                "EasyEffects' autoload settings")))
        if tree.bypass is None and tree.bypass_unstamped is not None and fallback_ours and tree.presets:
            result.left.append((tree.bypass_unstamped, (
                "carries no stamp of this tool's, so it may be your own; the "
                "Fallback Preset setting pointing at it stays on too")))

        switched = {"ok": False}
        switch = (_live_steps(tree, names, bypass_going, result, switched)
                  if tree.live else [])
        if not tree.live and rc and rc["last_output_preset"] in names:
            result.after.append(_still_applies(rc["last_output_preset"]))
        live += switch
        # EasyEffects' saved settings name the impulse its convolver loads at
        # start. That reference is ours to break only once the switch above
        # has emptied the chain; otherwise it may be a preset that stays
        # (one of the same name in EasyEffects' tree, when --output-dir
        # pointed elsewhere), which would come up in passthrough. The list
        # assumes a planned switch works; the removal re-checks with what it
        # did. The settings read are this tree's own install's.
        rc_dir = (tree.rc or ee_paths.DEFAULT_EASYEFFECTS_RC).parent
        for path, resaved in tree.presets:
            for irs in _impulses_of(tree.irs_dir, path.stem, legacy=not resaved):
                users = impulses.referrers(irs.stem, tree.output_dir,
                                           ignore=removing, ee_db=not switch,
                                           rc_dir=rc_dir)
                if users:
                    result.left.append((irs, "kept: still used by "
                                        + ", ".join(map(_user, users))
                                        + "; nothing to do"))
                    if users == [impulses.EE_SETTINGS]:
                        # Nothing names it once another preset is loaded,
                        # and a later run can't find it: impulses are found
                        # through the presets this run removes.
                        result.after.append((
                            "Once EasyEffects has loaded another preset, "
                            "delete this file by hand:",
                            "rm " + shlex.quote(str(irs))))
                    continue
                irs_steps.append(Step(
                    "Filter files those presets use (.irs)", irs,
                    recheck=lambda i=irs, t=tree, d=rc_dir: i.exists()
                    and not impulses.referrers(i.stem, t.output_dir,
                                               ee_db=not switched["ok"],
                                               rc_dir=d)))

    result.steps = live + rc_steps + entry_steps + preset_steps + irs_steps
    result.where = (" or ".join(doctor.tilde(t.output_dir) for t in trees)
                    or doctor.tilde(args.output_dir))
    if result.steps:
        result.prepare = lambda: _hide_window(args)
    return result


def _names(path: Path, preset: str) -> bool:
    data = _read_json(path)
    return isinstance(data, dict) and data.get("preset-name") == preset


def _read_rc(rc_path: Path | None) -> dict | None:
    if rc_path is None:
        return None
    try:
        return autoload.read_ee_rc(rc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _fallback_on(rc_path: Path) -> bool:
    rc = _read_rc(rc_path)
    return bool(rc and rc["uses_fallback"]
                and rc["fallback_preset"] == autoload.BYPASS_PRESET_NAME)


def _impulses_of(irs_dir: Path, preset: str, legacy: bool) -> list[Path]:
    """The impulse files this tool wrote for ``preset``, every build of it.

    The legacy unhashed ``{preset}.irs`` only with ``legacy``, for a stamped
    preset: beside an unstamped one that bare name is the `_ours` case it
    can't tell from a user's own impulse."""
    try:
        return sorted(p for p in irs_dir.glob(f"{glob.escape(preset)}*.irs")
                      if autoload.kernel_belongs_to(preset, p.stem)
                      and (legacy or p.stem != preset))
    except OSError:
        return []


def _user(name: str) -> str:
    """Who `impulses.referrers` says still uses a file, as the reader knows
    it. A preset that stays in a folder this run scans is not ours (ours
    there are all being removed), so it is theirs; one elsewhere, and a
    conf, may be either."""
    if name.endswith(".conf"):
        return f"the PipeWire conf {name}"
    if name == impulses.EE_SETTINGS:
        return name
    if " in " in name:
        # A preset in a folder this run doesn't scan (``referrers`` names
        # EasyEffects' own when --output-dir pointed elsewhere): it may be
        # this tool's from an earlier install there, so not "yours".
        return f"the preset {name}"
    return f"your preset {name}"


def _still_applies(preset: str) -> str:
    return (f"EasyEffects keeps applying the '{preset}' effects from its own "
            "settings, even with the preset file gone. Load another preset in "
            "EasyEffects, or remove its effects, to clear them.")


def _live_steps(tree: _Tree, names: set[str], bypass_going: bool,
                result: Plan, switched: dict) -> list[Step]:
    """Switch a running EasyEffects off our preset before its files go.

    Only a native EasyEffects answers on its socket (a Flatpak's is inside
    the sandbox), and only the native tree is the one it reads. Anything
    this can't settle becomes a step for the reader, said once."""
    if not names and not bypass_going:
        return []
    current = (ee_socket.last_loaded_output_preset()
               if ee_socket.daemon_listening() else ee_socket.EEReply())
    if not current.reached:
        rc = _read_rc(tree.rc)
        if rc and rc["last_output_preset"] in names:
            result.after.append(_still_applies(rc["last_output_preset"]))
        return []
    if not current.answered:
        result.after.append(
            "EasyEffects didn't say which preset it is playing. If it is one "
            "of the presets removed above, load another preset there.")
        return []
    playing = current.value
    if playing not in names and not (playing == autoload.BYPASS_PRESET_NAME
                                     and bypass_going):
        return []
    if bypass_going and playing != autoload.BYPASS_PRESET_NAME:
        empty, write = autoload.BYPASS_PRESET_NAME, False
    else:
        empty, write = EMPTY_PRESET_NAME, True
        clash = tree.output_dir / f"{empty}.json"
        if clash.exists() and not _stamped(clash):
            result.after.append(_still_applies(playing))
            return []

    def switch() -> bool:
        path = tree.output_dir / f"{empty}.json"
        if write:
            autoload.write_bypass_preset(tree.output_dir, empty)
        try:
            loaded = ee_socket.load_output_preset(empty).outcome == "loaded"
        finally:
            if write:
                path.unlink(missing_ok=True)
        if not loaded:
            result.after.append(_still_applies(playing))
        switched["ok"] = loaded
        return loaded

    return [Step("EasyEffects", None, action="run",
                 label=(f"switch the running EasyEffects from '{playing}' to "
                        f"an empty preset ('{empty}')"),
                 do=switch)]


def _hide_window(args) -> None:
    from lib.preset import reload
    reload.hide_window_before_writing(args)


def ours_present() -> bool:
    """Whether any EasyEffects tree holds a preset this tool wrote, for the
    PipeWire route's pointer back here."""
    for _label, base, _rc in ee_paths.install_trees():
        try:
            if any(_ours(p) for p in (base / "output").glob("*.json")):
                return True
        except OSError:
            continue
    return False


def run(args) -> int:
    """List, ask, remove: ``dolby_to_easyeffects.py --uninstall``."""
    return uninstall.run(plan(args), dry_run=args.dry_run, assume_yes=args.yes)
