#!/usr/bin/env python3
"""Check the PipeWire LV2-loader probe and its remedy on real distributions.

For each distribution image, in a throwaway podman container: install a
minimal PipeWire, start it headless as a non-root user, and run
`session.lv2_loader()` and `packages.lv2_loader_steps()` from this checkout's
`lib/`, mounted read-only. Where the remedy names a package the package
manager has, install exactly the command it prints (minus `sudo`) and probe
again. That is
the end-to-end proof that the name `lib/packages.py` prints fixes the fault,
not just that it exists.

Not a test tier: it needs the network and several minutes, and the facts it
checks are per-release packaging. Run it by hand after changing the loader
rows in `lib/packages.py`, and record the date and results in that module's
provenance note. Nothing on the host is touched besides podman's image store.

    python3 tools/pw_distro_matrix.py              # every row
    python3 tools/pw_distro_matrix.py fedora-44    # one row
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# name: (image, install-a-minimal-PipeWire-as-root, extra setup for a user).
# Minimal on purpose: no recommends, nothing a desktop would add, so the row
# shows what PipeWire alone pulls in.
_APT = ("apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install "
        "-y -qq --no-install-recommends pipewire pipewire-bin python3 "
        "procps >/dev/null")
_DNF = ("dnf -y -q install pipewire pipewire-utils python3 shadow-utils "
        "util-linux >/dev/null")
ROWS = {
    "fedora-44": ("registry.fedoraproject.org/fedora:44", _DNF),
    "rocky-9": ("docker.io/rockylinux/rockylinux:9", _DNF),
    "ubuntu-26.04": ("docker.io/library/ubuntu:26.04", _APT),
    "ubuntu-24.04": ("docker.io/library/ubuntu:24.04", _APT),
    "debian-stable": ("docker.io/library/debian:stable-slim", _APT),
    "debian-12": ("docker.io/library/debian:12", _APT),
    # With pipewire-pulse, as a desktop has it: bare `pipewire` lacks the
    # graph core, and the probe then rightly says it doesn't know.
    "arch": ("docker.io/library/archlinux:latest",
             "pacman -Sy --noconfirm --needed pipewire pipewire-pulse python "
             ">/dev/null"),
    # Alpine edge ships the filter-chain module in pipewire-pulse and the
    # graph core in pipewire-filter-graph, which nothing depends on. Without
    # both the module can't load at all, and the probe says it doesn't know;
    # with both, the LV2 loader is the one piece left to miss.
    "alpine-edge": ("docker.io/library/alpine:edge",
                    "apk add -q pipewire pipewire-pulse pipewire-filter-graph "
                    "pipewire-tools python3 bash shadow runuser"),
    "opensuse-leap-16": ("registry.opensuse.org/opensuse/leap:16.0",
                         "zypper -n -q install pipewire pipewire-tools "
                         "python3 >/dev/null"),
}

# Runs inside the container, as the non-root user, from the read-only mount.
_PROBE = '''
import sys
from pathlib import Path
sys.path.insert(0, "/src")
from lib import packages
from lib.pipewire import session

# This container's daemon is the one being asked, so the toolbox guard,
# which assumes the host's, would be wrong here: hide its marker.
_path = session.host.path
session.host.path = lambda p: (Path("/nonexistent")
                               if str(p) == "/run/.containerenv" else _path(p))
loader = session.lv2_loader()
fam = packages.family()
print("pipewire:", session.pipewire_version().text or "?")
print("probe:", "present" if loader.present else
      "unknown (" + loader.reason + ")" if loader.present is None else
      "ABSENT" + (" (built without LV2)" if loader.built_without else ""))
if loader.line:
    print("pipewire said:", loader.line)
command = ""
if loader.present is False and not loader.built_without and fam:
    if packages.installable(packages.PW_LV2_LOADER, fam):
        command = packages.install_command([packages.PW_LV2_LOADER], fam)
if loader.present is False:
    for style, text in packages.lv2_loader_steps(loader.built_without):
        print("remedy:", text)
print("INSTALL=" + command)
'''

_INNER = r'''
set -e
{install}
id u >/dev/null 2>&1 || useradd -m u
probe() {{
  runuser -u u -- env XDG_RUNTIME_DIR=/tmp/xdg-u PYTHONDONTWRITEBYTECODE=1 sh -c '
    mkdir -p -m 700 "$XDG_RUNTIME_DIR"
    pipewire >/tmp/pw.log 2>&1 & daemon=$!
    for i in 1 2 3 4 5 6 7 8 9 10; do
      [ -S "$XDG_RUNTIME_DIR/pipewire-0" ] && break; sleep 0.5; done
    python3 /m/probe.py 2>&1 || echo "probe failed: $?"
    kill $daemon 2>/dev/null; wait $daemon 2>/dev/null; true' 2>&1
}}
echo "--- as installed"
out=$(probe); echo "$out"
cmd=$(echo "$out" | sed -n 's/^INSTALL=sudo //p')
if [ -n "$cmd" ]; then
  echo "--- after: $cmd"
  case "$cmd" in
    apt*) DEBIAN_FRONTEND=noninteractive $cmd -y -qq >/dev/null ;;
    apk*) $cmd -q ;;
    dnf*) $cmd -y -q >/dev/null ;;
    pacman*) $cmd --noconfirm >/dev/null ;;
    *) $cmd ;;
  esac
  probe
fi
'''


def run_row(name: str, image: str, install: str, work: Path) -> str:
    """One distribution's transcript, or why it couldn't run."""
    # Read by the container's non-root user, who is not the host's.
    work.chmod(0o755)
    (work / "probe.py").write_text(_PROBE)
    (work / "inner.sh").write_text(_INNER.format(install=install))
    # Only lib/, read-only, and SELinux labelling off rather than `:Z`,
    # which would relabel every file mounted on each row.
    argv = ["podman", "run", "--rm", "--security-opt", "label=disable",
            "-v", f"{ROOT / 'lib'}:/src/lib:ro", "-v", f"{work}:/m:ro",
            image, "sh", "/m/inner.sh"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=600)
    except subprocess.TimeoutExpired:
        return "timed out after 600 s"
    out = "\n".join(line for line in proc.stdout.splitlines()
                    if not line.startswith("INSTALL="))
    if proc.returncode != 0:
        out += f"\n(exit {proc.returncode})\n{proc.stderr.strip()[-600:]}"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rows", nargs="*",
                        help=f"rows to run (default: all): {', '.join(ROWS)}")
    parser.add_argument("--out-dir", type=Path,
                        default=ROOT / "localresearch" / "pw-distro-matrix",
                        help="where the transcript is written")
    args = parser.parse_args(argv)
    unknown = [r for r in args.rows if r not in ROWS]
    if unknown:
        parser.error(f"unknown row(s): {', '.join(unknown)}")
    if shutil.which("podman") is None:
        parser.error("podman not found")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for name in args.rows or ROWS:
        image, install = ROWS[name]
        print(f"=== {name} ({image})", flush=True)
        with tempfile.TemporaryDirectory() as tmp:
            text = run_row(name, image, install, Path(tmp))
        print(text, flush=True)
        report.append(f"=== {name} ({image})\n{text}\n")
        # After every row, so a long run shows its progress on disk.
        out = args.out_dir / "matrix.txt"
        out.write_text("\n".join(report))
    print(f"\nWrote {args.out_dir / 'matrix.txt'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
