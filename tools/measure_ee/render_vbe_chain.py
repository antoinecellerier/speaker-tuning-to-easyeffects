#!/usr/bin/env python3
"""Render the issue-#14 virtual-bass chain offline (design-notes Finding 8).

Rebuilds the LSP-cascade + Calf Saturator proof-of-concept as a saved,
re-runnable generator:

    HP@35 (BWC BT x16) -> LP@160 (BWC BT x16) -> Calf Saturator
    (drive=4, mix=1.0, blend=0, level_out=1.0, internal pre/post
    filters OFF) -> HP@180 (BWC BT x8) -> LP@800 (BWC BT x8)

    With these defaults the five stage renders reproduce the
    2026-05-06 PoC bit-for-bit (lsp-plugins-lv2 1.2.33,
    calf-plugins 0.90.9).

One lv2apply subprocess per stage, every intermediate written, plus a
provenance sidecar. Entirely file-in/file-out: no audio routing is
touched, so this needs **no audio handoff** (unlike the capture harness
in this directory).

Every filter stage runs the IIR engine (`mode 0`) — zero added latency,
matching the project's no-look-ahead invariant.

Known plugin quirk this script tolerates: Calf Saturator renders the
full output, then aborts during host teardown (glibc heap-corruption
abort, exit 134). The output is complete; each stage is therefore
validated by frame count against the stimulus rather than by exit code
alone.

Usage:

    python3 tools/measure_ee/render_vbe_chain.py

Analyze the renders with analyze_vbe_chain.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import struct
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

FILTER_URI = "http://lsp-plug.in/plugins/lv2/filter_stereo"
SATURATOR_URI = "http://calf.sourceforge.net/plugins/Saturator"

# lv2info-verified enum values (lsp-plugins-lv2 1.2.33).
SLOPES = {"x1": 0, "x2": 1, "x3": 2, "x4": 3, "x6": 4, "x8": 5, "x12": 6,
          "x16": 7}
BWC_MODES = {"bt": 2, "mt": 3}  # fm: 2 = "BWC (BT)", 3 = "BWC (MT)"
FT_LOPASS = 0
FT_HIPASS = 1
ENGINE_IIR = 0  # `mode` port: IIR engine, zero added latency

PROVENANCE_PACKAGES = ("lsp-plugins-lv2", "calf-plugins")


def _lsp_controls(ft: int, freq: float, slope: str, bwc: str) -> list[str]:
    return [
        "-c", "enabled", "1",
        "-c", "mode", str(ENGINE_IIR),
        "-c", "ft", str(ft),
        "-c", "fm", str(BWC_MODES[bwc]),
        "-c", "s", str(SLOPES[slope]),
        "-c", "f", str(freq),
    ]


def _saturator_controls(args: argparse.Namespace) -> list[str]:
    return [
        "-c", "bypass", "0",
        "-c", "level_in", "1.0",
        "-c", "level_out", str(args.level_out),
        "-c", "mix", str(args.mix),
        "-c", "drive", str(args.drive),
        "-c", "blend", str(args.blend),
        # The load-bearing bit: Calf's internal pre/post filters OFF —
        # band-limiting is done by the external LSP stages instead.
        "-c", "pre", "0",
        "-c", "post", "0",
    ]


def stage_commands(args: argparse.Namespace, stimulus: Path, out_dir: Path
                   ) -> list[tuple[str, Path, list[str]]]:
    """The five (name, output, argv) stages. Pure function of the args —
    this is the surface tests/test_render_vbe_chain.py locks down."""
    stages = [
        ("hp1", _lsp_controls(FT_HIPASS, args.hp1, args.slope_in, args.bwc),
         FILTER_URI),
        ("bp", _lsp_controls(FT_LOPASS, args.lp1, args.slope_in, args.bwc),
         FILTER_URI),
        ("sat", _saturator_controls(args), SATURATOR_URI),
        ("hp2", _lsp_controls(FT_HIPASS, args.hp2, args.slope_out, args.bwc),
         FILTER_URI),
        ("final", _lsp_controls(FT_LOPASS, args.lp2, args.slope_out, args.bwc),
         FILTER_URI),
    ]
    commands = []
    src = stimulus
    for name, controls, uri in stages:
        out = out_dir / f"{args.label}_{name}.wav"
        commands.append(
            (name, out,
             ["lv2apply", "-i", str(src), "-o", str(out)] + controls + [uri]))
        src = out
    return commands


def wav_frames(path: Path) -> int:
    """Frame count via a minimal RIFF walk (the outputs are float32, which
    the stdlib `wave` module rejects)."""
    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a RIFF/WAVE file")
    channels = bits = None
    i = 12
    while i + 8 <= len(raw):
        cid = raw[i:i + 4]
        sz = struct.unpack("<I", raw[i + 4:i + 8])[0]
        if cid == b"fmt ":
            channels = struct.unpack("<H", raw[i + 10:i + 12])[0]
            bits = struct.unpack("<H", raw[i + 22:i + 24])[0]
        elif cid == b"data":
            if channels is None:
                raise ValueError(f"{path}: data chunk before fmt")
            return sz // (channels * (bits // 8))
        i += 8 + sz + (sz & 1)
    raise ValueError(f"{path}: no data chunk")


def run_stage(name: str, out: Path, argv: list[str], want_frames: int) -> None:
    print(f"[{name}] {shlex.join(argv)}")
    proc = subprocess.run(argv)
    if proc.returncode == 0:
        return
    # Calf Saturator aborts in teardown *after* the render completes; accept
    # a non-zero exit iff the output is present and frame-complete.
    try:
        got = wav_frames(out)
    except (OSError, ValueError):
        got = -1
    if got == want_frames:
        print(f"[{name}] plugin aborted at teardown (exit "
              f"{proc.returncode}) after a complete render — continuing")
        return
    sys.exit(f"[{name}] failed (exit {proc.returncode}, "
             f"{got}/{want_frames} frames): {shlex.join(argv)}")


def package_versions() -> dict[str, str]:
    versions = {}
    for pkg in PROVENANCE_PACKAGES:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f", "${Version}", pkg],
            capture_output=True, text=True)
        versions[pkg] = proc.stdout.strip() if proc.returncode == 0 \
            else "unknown"
    return versions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the virtual-bass LSP-cascade + Saturator chain "
                    "offline (issue #14). No audio handoff needed.")
    parser.add_argument(
        "--stimulus", type=Path,
        default=REPO_ROOT / "localresearch/measure_dax/stimulus_bass_burst.wav")
    parser.add_argument(
        "--out-dir", type=Path,
        default=REPO_ROOT / "localresearch/measure_ee/vbe_chain")
    parser.add_argument("--label", default="vbe",
                        help="output filename prefix (default: vbe)")
    parser.add_argument("--hp1", type=float, default=35.0,
                        help="source-band low edge, Hz (XML "
                             "virtual-bass-src-freqs[0])")
    parser.add_argument("--lp1", type=float, default=160.0,
                        help="source-band high edge, Hz (src-freqs[1])")
    parser.add_argument("--hp2", type=float, default=180.0,
                        help="harmonic-band low edge, Hz")
    parser.add_argument("--lp2", type=float, default=800.0,
                        help="harmonic-band high edge, Hz")
    parser.add_argument("--slope-in", choices=sorted(SLOPES), default="x16",
                        help="input band-pass slope (default: x16)")
    parser.add_argument("--slope-out", choices=sorted(SLOPES), default="x8",
                        help="output band-pass slope (default: x8)")
    parser.add_argument("--bwc", choices=sorted(BWC_MODES), default="bt",
                        help="BWC transform variant (default: bt)")
    parser.add_argument("--drive", type=float, default=4.0)
    parser.add_argument("--level-out", type=float, default=1.0,
                    help="Saturator output gain; the chain PoC ran unity (the\n"
                         "single-plugin conf's 4.0 does not apply here)")
    parser.add_argument("--blend", type=float, default=0.0)
    parser.add_argument("--mix", type=float, default=1.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not args.stimulus.is_file():
        sys.exit(f"stimulus not found: {args.stimulus} "
                 "(generate with tools/measure_dax/make_stimulus.py)")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    want_frames = wav_frames(args.stimulus)
    commands = stage_commands(args, args.stimulus, args.out_dir)
    for name, out, argv in commands:
        run_stage(name, out, argv, want_frames)

    sidecar = args.out_dir / f"{args.label}_chain.json"
    sidecar.write_text(json.dumps({
        "stimulus": str(args.stimulus),
        "stimulus_sha256": hashlib.sha256(
            args.stimulus.read_bytes()).hexdigest(),
        "host": "lv2apply",
        "packages": package_versions(),
        "rendered_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "params": {k: (str(v) if isinstance(v, Path) else v)
                   for k, v in vars(args).items()},
        "stages": [{"name": name, "output": str(out),
                    "argv": shlex.join(argv)}
                   for name, out, argv in commands],
    }, indent=2) + "\n")
    print(f"\nWrote {len(commands)} stage renders and {sidecar.name} "
          f"under {args.out_dir}")


if __name__ == "__main__":
    main()
