"""``--uninstall`` on the PipeWire route: the filter-chain confs and their
impulse copies.

Shared by ``ee_to_pipewire.py`` and ``dolby_to_pipewire.py``, which write the
same files. Builds the `lib.uninstall.Plan` the shared runner lists, asks
about and carries out.

- **A conf** is ours when it starts with `conf.CONF_HEADER_MARK`, the test
  the doctor uses (`checks.installed_confs`).
- **An impulse file** is ours only when it is the copy the converter makes:
  ``<conf dir>/<node name>.irs`` (``ee_to_pipewire.py``'s copy step), named
  by that conf and by no conf that stays. Anything else a conf names is
  someone else's file, the EasyEffects tree's under ``--no-copy-irs`` or a
  folder of the user's own, and is listed as left in place.

Nothing here imports ``lib/preset``: the pointer to the EasyEffects route's
uninstall is the entry scripts' to print.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lib import console, doctor, ee_socket, tool_env, uninstall
from lib.pipewire import checks, install
from lib.pipewire import conf as pw_conf
from lib.uninstall import Plan, Step

FILTER_CHAIN_RESTART_CMD = "systemctl --user restart filter-chain"


def _copy_names(found: checks.InstalledConf) -> list[Path]:
    """Where the converter would have put this conf's impulse copy.

    The node name when it could be read (``effect_input.<name>``), and the
    conf's own stem, which is the same name unless ``--output`` renamed it.
    """
    names = []
    prefix = "effect_input."
    if found.node_name.startswith(prefix):
        names.append(found.node_name[len(prefix):])
    names.append(found.path.stem)
    return [found.path.parent / f"{n}.irs" for n in dict.fromkeys(names)]


def _named_by(irs: Path, conf_path: Path, parsed: list[Path]) -> bool:
    """Does the conf name ``irs``? From the parsed paths when spa-json-dump
    read it, else from its ``filename`` lines. Compared resolved, since the
    conf keeps the path as the install spelled it and the uninstall may
    have been handed another spelling of the same directory."""
    named = parsed or checks.conf_filenames(conf_path)
    return irs.resolve() in {p.resolve() for p in named}


def plan(output_dir: Path | None) -> Plan:
    """What ``--uninstall`` would remove from ``output_dir``, or from both
    directories a conf is live from, doing none of it."""
    dirs = _dirs(output_dir)
    found = checks.installed_confs(*dirs)
    removing = {c.path for c in found}
    # Every conf that could still name a copy: the ones looked in, and both
    # live directories even when --output-dir pointed elsewhere.
    scan = (*dirs, *checks.live_conf_dirs())
    result = Plan()
    confs, copies, seen = [], [], set()
    for c in found:
        confs.append(Step("Speaker filters (PipeWire config files)", c.path,
                          recheck=lambda p=c.path: checks.is_generated_conf(p)))
        copy_paths = [p for p in _copy_names(c) if p.is_file()
                      and _named_by(p, c.path, c.irs)]
        for irs in copy_paths:
            if irs in seen:
                continue
            seen.add(irs)
            users = checks.confs_naming_file(irs, *scan, exclude=removing)
            if users:
                result.left.append((irs, "kept: still used by "
                                    + ", ".join(f"the conf {u}" for u in users)))
                continue
            copies.append(Step(
                "Filter files those configs use (.irs)", irs,
                recheck=lambda i=irs: i.is_file()
                and not checks.confs_naming_file(i, *scan)))
        copied = {p.resolve() for p in copy_paths}
        for other in c.irs:
            if other.resolve() not in copied:
                result.left.append((other, (
                    f"named by {c.path.name}, but not a copy this tool made "
                    "beside it")))
    result.steps = confs + copies
    result.where = where(output_dir)
    return result


def touches_filter_chain_service(p: Plan) -> bool:
    """Whether a conf on the list sits where ``filter-chain.service`` reads.

    That unit loads its confs itself (`checks._UNSCANNED_CONF_DIR` says why
    the directory exists), so it gets a restart of its own. Debian's stock
    unit is ``BindsTo=pipewire.service`` and may follow PipeWire's restart
    anyway (read from the unit file, not observed); a second restart then
    costs nothing.
    """
    unscanned = checks.live_conf_dirs()[1].resolve()
    return any(s.path is not None and s.path.parent.resolve() == unscanned
               and s.path.suffix == ".conf" for s in p.steps)


def touches_live_dirs(p: Plan) -> bool:
    """Whether a conf on the list is one PipeWire or filter-chain.service
    loads: a folder of the reader's own (``--output-dir`` staging) loads
    nowhere, and restarting audio for it would cut sound for nothing."""
    live = {d.expanduser().resolve() for d in checks.live_conf_dirs()}
    return any(s.path is not None and s.path.suffix == ".conf"
               and s.path.parent.resolve() in live for s in p.steps)


def filter_chain_service_active() -> bool:
    try:
        proc = tool_env.run(["systemctl", "--user", "is-active", "--quiet",
                             "filter-chain"], timeout=5)
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


def _dirs(output_dir: Path | None) -> tuple[Path, ...]:
    """Resolved like the install path resolves it, so the same flag value
    names the same directory, and comparisons against the live directories
    hold through a relative path or a symlink."""
    if output_dir is None:
        return checks.live_conf_dirs()
    return (output_dir.expanduser().resolve(),)


def where(output_dir: Path | None) -> str:
    return " or ".join(doctor.tilde(d) for d in _dirs(output_dir))


def run(output_dir: Path | None, *, dry_run: bool, assume_yes: bool,
        restart: bool) -> int:
    """List, ask, remove, then unload what was removed.

    ``restart`` is the wrapper's activation default: it restarts PipeWire
    after removing a chain, as it does after installing one. False
    (``ee_to_pipewire.py``, or ``--no-activate``) prints the command
    instead, the same split as on install.
    """
    p = plan(output_dir)
    service = touches_filter_chain_service(p) and filter_chain_service_active()
    commands = [pw_conf.PIPEWIRE_RESTART_CMD] + (
        [FILTER_CHAIN_RESTART_CMD] if service else [])
    loaded = touches_live_dirs(p)
    restart = restart and loaded
    if restart:
        # The EasyEffects clause only where it is true: this route's reader
        # usually has no EasyEffects at all.
        ee = (" A running EasyEffects stops with it; start it again if "
              "you use it."
              if ee_socket.easyeffects_running() is True else "")
        p.then.append(f"restart PipeWire ({' && '.join(commands)}). Sound "
                      f"stops for a moment while it restarts.{ee}")
    elif loaded:
        p.after.append(("Restart PipeWire so the removed filters stop playing:",
                        " && ".join(commands)))
    return uninstall.run(p, dry_run=dry_run, assume_yes=assume_yes,
                         finish=(lambda: _restart(service)) if restart else None)


def _restart(service: bool) -> int:
    console.cprint("head", "Restarting PipeWire")
    rc = install.restart_pipewire()
    if rc is not None:
        return rc
    if service:
        try:
            proc = tool_env.run(FILTER_CHAIN_RESTART_CMD.split())
        except (subprocess.SubprocessError, OSError):
            proc = None
        if proc is None or proc.returncode != 0:
            console.cprint("err", "error: filter-chain restart failed — run "
                           f"it manually: {FILTER_CHAIN_RESTART_CMD}")
            return 1
    console.cprint("dim", "  PipeWire restarted; the removed filters no "
                   "longer play.")
    return 0
