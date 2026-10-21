"""Who still names an impulse file, asked before one of ours is removed.

Two removers ask: the generator's stale-impulse sweep
(`lib/preset/emit.py` `_drop_stale_impulses`) and ``--uninstall``
(`lib/preset/uninstall.py`). Its own module rather than emit's, because emit
imports numpy and ``--uninstall`` must not pay for it.

Stdlib-only. The one edge out of `lib/preset/` is the lazy PipeWire import
below, explained where it is made.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lib import doctor, ee_paths


# How `referrers` names EasyEffects' own saved settings as a user.
EE_SETTINGS = "EasyEffects' saved settings"


def referrers(stem: str, output_dir: Path, *, ignore=frozenset(),
              ee_db: bool = True, rc_dir: Path | None = None) -> list[str]:
    """Presets, PipeWire confs and EE saved settings that name `{stem}.irs`.

    A preset saved from EasyEffects' GUI keeps the kernel name of the preset
    it was derived from, so a file this run would call stale can be the only
    impulse another preset has. EasyEffects loads a missing kernel without a
    word, leaving that preset convolving nothing. A conf written
    with `ee_to_pipewire.py --no-copy-irs` pins the EE-side path the same
    way, and there a missing file stops the whole conf loading. And
    EasyEffects restores the convolver's kernel *name* from its config db on
    start, not from the preset JSON: with that file gone, a fresh instance
    logged "Kernel 'Dolby-Balanced' not found … Entering passthrough mode"
    (dev machine, 2026-08-27). That stays silent until the next preset load,
    which without autoload is never. Until a load names the new impulse, the
    one the db names is what the next start plays.

    Reads EasyEffects' own preset directory as well as this run's, and both
    PipeWire conf directories. The comment beside each scan says why.

    ``ignore`` names preset files that don't count as users, and ``ee_db``
    False skips EasyEffects' saved settings. Both are for ``--uninstall``
    (`lib/preset/uninstall.py`): it removes those presets in the same run,
    and when it has emptied a running EasyEffects' chain first, the saved
    settings' reference is its own to break. ``rc_dir`` is the settings
    folder to read, for an uninstall walking an install the run's defaults
    don't point at.
    """
    users: list[str] = []
    if ee_db:
        try:
            folder = (rc_dir if rc_dir is not None
                      else ee_paths.DEFAULT_EASYEFFECTS_RC.parent)
            db = (folder / "convolverrc").read_text()
            if re.search(rf"^kernelName={re.escape(stem)}\s*$", db, re.M):
                users.append(EE_SETTINGS)
        except OSError:
            pass
    # This run's tree and EasyEffects' own. They differ when --output-dir
    # alone was passed: --irs-dir then still means the live tree, and the
    # presets naming a file there are the live ones, which this run never
    # rewrote. A directory too many only ever keeps a file.
    preset_dirs = [output_dir]
    if ee_paths.DEFAULT_OUTPUT_DIR.resolve() != output_dir.resolve():
        preset_dirs.append(ee_paths.DEFAULT_OUTPUT_DIR)
    for preset_dir in preset_dirs:
        try:
            presets = sorted(preset_dir.glob("*.json"))
        except OSError:
            continue
        for path in presets:
            if path in ignore:
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out = data.get("output") if isinstance(data, dict) else None
            if not isinstance(out, dict):
                continue
            if any(key.startswith("convolver") and isinstance(block, dict)
                   and block.get("kernel-name") == stem
                   for key, block in out.items()):
                users.append(path.stem if preset_dir is output_dir
                             else f"{path.stem} in {doctor.tilde(preset_dir)}")
    # Lazy and one-way: nothing under lib/pipewire imports lib/preset, and
    # this is the one place the EasyEffects writer needs to know where the
    # PipeWire confs live. It scans both directories a conf is live from:
    # the daemon's and filter-chain.service's. docs/alternative-pipelines.md
    # hands out a skeleton for the second.
    from lib.pipewire import checks
    return users + checks.confs_naming(f"{stem}.irs", *checks.live_conf_dirs())
