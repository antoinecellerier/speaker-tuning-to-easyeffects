#!/usr/bin/env python3
"""Capture the full DAX3 stimulus battery through the live EasyEffects
chain into the validated null-sink route.

Outputs (in --out-dir, default cwd):

    loopback_<stim_tag>_<label>.wav  (32-bit float, stereo, 48 kHz)
    loopback_<stim_tag>_<label>.json (sidecar — analyzer-compatible)

The sidecar mirrors the schema produced by tools/measure_dax/capture_dax.py
closely enough that tools/measure_dax/analyze.py reads it without changes.
The capture_source/endpoint fields make it clear the data came from EE,
not from Windows DAX3.

Pre-flight:
  1. Run tools/measure_ee/setup_null_sink.sh once.
  2. Run tools/measure_ee/smoke.py --target ee_capture.monitor and
     confirm PASS on the Nothing preset.
  3. Then run this script.

Example:

    python tools/measure_ee/capture_battery.py \\
        --stimulus-dir ~/dax-measure \\
        --preset Dolby-Balanced \\
        --label ee_dolby_balanced \\
        --target ee_capture.monitor \\
        --out-dir ~/dax-measure/ee_captures
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

SR = 48000
SCHEMA_VERSION = 1

# Reuse the harness primitives so playback timing stays identical
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import smoke as _smoke  # noqa: E402

STIMULUS_NAMES = (
    "stimulus_sweep.wav",
    "stimulus_sweep_quiet.wav",
    "stimulus_pink.wav",
    "stimulus_pink_quiet.wav",
    "stimulus_multitone.wav",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stimulus_meta(stim_path: Path) -> dict:
    """Read `stimulus_<x>.json` next to the stimulus."""
    meta_path = stim_path.with_suffix(".json")
    if not meta_path.is_file():
        raise SystemExit(
            f"missing stimulus meta JSON: {meta_path} — re-run "
            f"tools/measure_dax/make_stimulus.py to generate it"
        )
    return json.loads(meta_path.read_text())


def _capture_metrics(cap: np.ndarray) -> dict:
    """Compute the stats the analyzer's sidecar consumes."""
    if cap.ndim == 1:
        cap = np.column_stack([cap, cap])
    rms = np.sqrt(np.mean(cap.astype(np.float64) ** 2, axis=0))
    peak = np.max(np.abs(cap), axis=0)
    return {
        "samples": int(cap.shape[0]),
        "duration_seconds": float(cap.shape[0] / SR),
        "channels": int(cap.shape[1]),
        "sample_rate_hz": SR,
        "peak_per_channel": [float(p) for p in peak],
        "rms_per_channel": [float(r) for r in rms],
    }


def _load_preset(preset: str, verbose: bool) -> None:
    rc = subprocess.run(
        ["easyeffects", "-l", preset], capture_output=True, text=True
    )
    if rc.returncode != 0:
        raise SystemExit(
            f"easyeffects -l {preset!r} failed: {rc.stderr.strip()}"
        )
    if verbose:
        print(f"loaded preset {preset!r}")
    # Give EE a moment to reroute its plugin chain
    time.sleep(0.8)


def capture_one(
    stim_path: Path, target: str, out_path: Path,
    label: str, preset: str, verbose: bool,
) -> None:
    meta = _stimulus_meta(stim_path)
    stim_kind = meta.get("kind") or "sweep"
    # tag is derived from the filename stem so stimulus_sweep.wav and
    # stimulus_sweep_quiet.wav don't collide. Mirrors capture_dax.py.
    stim_tag = stim_path.stem.replace("stimulus_", "", 1) or stim_kind

    _smoke.play_and_capture(
        stim_path=stim_path,
        target=target,
        capture_path=out_path,
        play_target="easyeffects_sink",
        verbose=verbose,
    )
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise SystemExit(f"capture failed (no data): {out_path}")

    sr, cap = wavfile.read(str(out_path))
    if sr != SR:
        print(f"WARN: capture sr={sr} != expected {SR}", file=sys.stderr)
    if cap.dtype == np.int16:
        cap_f = cap.astype(np.float32) / 32768.0
    elif cap.dtype == np.int32:
        cap_f = cap.astype(np.float32) / 2147483648.0
    elif cap.dtype == np.float32:
        cap_f = cap
    else:
        cap_f = cap.astype(np.float32)

    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "capture_source": "easyeffects",
        "timestamp_utc": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "label": label,
        "stimulus_tag": stim_tag,
        "stimulus_kind": stim_kind,
        "wav_filename": out_path.name,
        "endpoint": {
            "name": target,
            "node": "ee_capture",
            "preset": preset,
            "easyeffects_version": _easyeffects_version(),
        },
        "stimulus": {
            "path": str(stim_path),
            "sample_rate_hz": SR,
            "channels": 2,
            "sha256": _sha256_file(stim_path),
            "sweep_seconds": meta.get("sweep_seconds"),
            "tail_seconds": meta.get("tail_seconds"),
            "stimulus_meta": meta,
        },
        "capture": _capture_metrics(cap_f),
        "system": {
            "platform": _platform_str(),
            "python_version": sys.version.split()[0],
            "pipewire_version": _pipewire_version(),
        },
    }
    out_path.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2, default=str)
    )
    if verbose:
        print(f"wrote {out_path} + sidecar")


def _easyeffects_version() -> str | None:
    try:
        rc = subprocess.run(
            ["easyeffects", "-v"], capture_output=True, text=True, timeout=2,
        )
        return rc.stdout.strip() or None
    except Exception:
        return None


def _pipewire_version() -> str | None:
    try:
        rc = subprocess.run(
            ["pipewire", "--version"], capture_output=True, text=True, timeout=2,
        )
        return rc.stdout.strip() or None
    except Exception:
        return None


def _platform_str() -> str:
    import platform
    return platform.platform()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stimulus-dir", type=Path, required=True,
                    help="Directory containing stimulus_*.{wav,json}")
    ap.add_argument("--preset", required=True,
                    help="EE preset to load (e.g. Dolby-Balanced)")
    ap.add_argument("--label", required=True,
                    help='Output filename label (e.g. "ee_dolby_balanced")')
    ap.add_argument("--target", default="ee_capture.monitor",
                    help="PipeWire capture node (default ee_capture.monitor)")
    ap.add_argument("--out-dir", type=Path, default=Path.cwd(),
                    help="Output directory (default cwd)")
    ap.add_argument("--stimuli", nargs="*",
                    help="Subset of stimulus filenames "
                         "(default all 5 in stimulus-dir)")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="Don't run a Nothing-preset smoke check first")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if shutil.which("pw-cat") is None or shutil.which("pw-record") is None:
        print("ERROR: pw-cat / pw-record not in PATH", file=sys.stderr)
        return 2
    if shutil.which("easyeffects") is None:
        print("ERROR: easyeffects not in PATH", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # 1. smoke gate (Nothing preset) -----------------------------------------

    if not args.skip_smoke:
        print("=== smoke gate (Nothing preset) ===")
        smoke_rc = subprocess.run([
            sys.executable, str(SCRIPT_DIR / "smoke.py"),
            "--target", args.target,
            "--label", "battery_pre_smoke",
            "--out-dir", str(args.out_dir / "smoke_gate"),
        ])
        if smoke_rc.returncode != 0:
            print("FAIL: smoke gate did not pass — refusing to run battery",
                  file=sys.stderr)
            return 1
        print("smoke gate PASSED")
        print()

    # 2. switch to the real preset -------------------------------------------

    print(f"=== capturing battery, preset={args.preset!r} ===")
    _load_preset(args.preset, verbose=args.verbose)

    # 3. resolve stimulus files ---------------------------------------------

    if args.stimuli:
        stim_files = [args.stimulus_dir / s for s in args.stimuli]
    else:
        stim_files = [args.stimulus_dir / s for s in STIMULUS_NAMES]
    missing = [str(s) for s in stim_files if not s.is_file()]
    if missing:
        print(f"ERROR: missing stimulus files: {missing}", file=sys.stderr)
        return 2

    # 4. run them ------------------------------------------------------------

    for stim in stim_files:
        # Tag mirrors capture_dax.py — derived from stimulus filename stem
        tag = stim.stem.replace("stimulus_", "", 1)
        out_path = args.out_dir / f"loopback_{tag}_{args.label}.wav"
        print(f"[{tag}] capturing -> {out_path.name}")
        t0 = time.monotonic()
        capture_one(stim, args.target, out_path, args.label, args.preset,
                    verbose=args.verbose)
        print(f"  done in {time.monotonic() - t0:.1f} s")

    print()
    print(f"battery complete. {len(stim_files)} captures in {args.out_dir}")
    print("next: run tools/measure_dax/analyze.py against these files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
