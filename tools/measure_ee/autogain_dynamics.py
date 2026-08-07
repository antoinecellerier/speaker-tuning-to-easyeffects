#!/usr/bin/env python3
"""Autogain (volume leveler) dynamics protocol — the issue-#25 checks.

The LTI battery (capture_battery.py) can't see leveler behaviour; this
harness measures the two failure modes that decide whether autogain can
ship enabled (design-notes "The 2026-07 default-flip attempt"):

  crackle  30 s of near-silence between the old (-70 dB) and shipped
           (-50 dB) silence gates, then a notification-style burst.
           Measures the silence gain wind-up and how hard the burst hits
           the limiter, one arm per --gates value (each written explicitly,
           so the base preset's own gate can't collapse the comparison).
           The -70 counterfactual should wind up tens of dB; the shipped
           gate should hold within a couple of dB.
  speech   Steady low background with intermittent loud speech-like
           bursts (the content class that keeps autogain bypassed on
           HDA). Measures background boost and onset overshoot across a
           maximum-history / target sweep — as shipped, history is NOT a
           reaction-speed lever (~4 dB overshoot regardless).

Variants are generated on the fly from --base-preset's JSON (bypass /
silence-threshold / maximum-history / target edits only) into temporary
`_AgProto-*` presets, and removed afterwards; the base preset is
reloaded at exit.

Pre-flight (same as capture_battery.py):
  1. bash tools/measure_ee/setup_null_sink.sh
  2. python3 tools/measure_ee/smoke.py --target ee_capture.monitor  # PASS
Then:
  python3 tools/measure_ee/autogain_dynamics.py --base-preset Dolby-Balanced
Restore audio with tools/measure_ee/teardown.sh when done.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT))
import smoke  # noqa: E402
# Reuse the scripts' EasyEffects base detection so this harness follows a
# Flatpak install the same way the generated presets do.
from lib import ee_paths  # noqa: E402

EE_OUTPUT_DIR = ee_paths.DEFAULT_OUTPUT_DIR

SR = 48000
VARIANT_PREFIX = "_AgProto-"


def _tag(value: float) -> str:
    """Filename-safe token that never merges distinct values (-20.5 -> 20p5)."""
    return f"{abs(value):g}".replace(".", "p")


def _pink(n: int, rng: np.random.Generator) -> np.ndarray:
    """Pink-ish noise via FFT 1/sqrt(f) shaping, unit RMS."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = f[1]
    spec /= np.sqrt(f)
    x = np.fft.irfft(spec, n)
    return x / np.sqrt(np.mean(x**2))


def _rms_db(x: np.ndarray) -> float:
    r = np.sqrt(np.mean(x.astype(np.float64) ** 2))
    return 20 * np.log10(max(r, 1e-12))


def _load_preset(name: str) -> None:
    rc = subprocess.run(["easyeffects", "-l", name],
                        capture_output=True, text=True)
    if rc.returncode != 0:
        raise SystemExit(f"easyeffects -l {name!r} failed: {rc.stderr}")
    time.sleep(1.0)


def _make_variant(base: dict, name: str, **autogain_overrides) -> Path:
    """Write a temp preset that only differs in autogain settings.

    Written atomically: EasyEffects watches this directory, and a torn read
    would leave the capture running on the previous variant's settings while
    the results table claims the new ones.
    """
    p = json.loads(json.dumps(base))
    ag = p["output"].get("autogain#0")
    if ag is None:
        raise SystemExit("base preset has no autogain#0 stage")
    ag.update(autogain_overrides)
    path = EE_OUTPUT_DIR / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2))
    os.replace(tmp, path)
    return path


def _capture(preset: str, stim_path: Path, out_dir: Path) -> np.ndarray:
    _load_preset(preset)
    cap_path = out_dir / f"cap_{preset}.wav"
    print(f"[{preset}] capturing…", flush=True)
    smoke.play_and_capture(
        stim_path=stim_path, target="ee_capture.monitor",
        capture_path=cap_path, play_target="easyeffects_sink",
    )
    sr, cap = smoke.read_wav_f32(cap_path)
    if sr != SR:
        raise SystemExit(
            f"{cap_path.name}: captured at {sr} Hz but every analysis window "
            f"is indexed at {SR} Hz — segment boundaries would be wrong. "
            "Set the session rate to 48 kHz and re-run.")
    if cap.ndim == 1:
        cap = np.column_stack([cap, cap])
    return cap


def _align_offset(cap: np.ndarray, pre_s: float, label: str,
                  thresh_db: float = -50.0) -> int:
    """Offset of the capture's first onset vs the stimulus.

    Raises rather than returning a bogus offset: np.argmax on an all-False
    mask returns 0, which would silently shift every window by `pre_s` and
    make the measured overshoot ~0 dB for every variant.
    """
    env = np.abs(cap[:, 0])
    win = int(0.05 * SR)
    smoothed = np.convolve(env, np.ones(win) / win, mode="same")
    above = smoothed > 10 ** (thresh_db / 20)
    if not above.any():
        raise SystemExit(
            f"{label}: capture never rises above {thresh_db:g} dBFS — cannot "
            "align. Check that EE is routed to the null sink (smoke.py) and "
            "that the preset isn't muting output.")
    return int(np.argmax(above)) - int(pre_s * SR)


# ---------------------------------------------------------------------------
# Protocol 1: silence wind-up + notification burst (the #25 crackle)
# ---------------------------------------------------------------------------

CRACKLE_SEGMENTS = [  # (name, seconds)
    ("pre", 0.5),
    ("settle", 10.0),   # pink @ -20 dBFS RMS
    ("floor", 30.0),    # noise @ -60 dBFS RMS: above -70, below -50
    ("burst", 0.4),     # notification chime @ -6 dBFS peak
    ("tail", 1.0),
]


def _crackle_stimulus() -> np.ndarray:
    rng = np.random.default_rng(42)
    parts = []
    for name, dur in CRACKLE_SEGMENTS:
        n = int(round(dur * SR))
        if name in ("pre", "tail"):
            parts.append(np.zeros(n))
        elif name == "settle":
            parts.append(_pink(n, rng) * 10 ** (-20 / 20))
        elif name == "floor":
            parts.append(_pink(n, rng) * 10 ** (-60 / 20))
        elif name == "burst":
            t = np.arange(n) / SR
            chime = 0.6 * np.sin(2 * np.pi * 880 * t) + 0.4 * np.sin(
                2 * np.pi * 1320 * t)
            fade = int(0.005 * SR)
            env = np.ones(n)
            env[:fade] = np.linspace(0, 1, fade)
            env[-fade:] = np.linspace(1, 0, fade)
            chime *= env * (10 ** (-6 / 20) / np.max(np.abs(chime)))
            parts.append(chime)
    mono = np.concatenate(parts).astype(np.float32)
    return np.column_stack([mono, mono])


def run_crackle(base: dict, out_dir: Path, gates: list[float]) -> None:
    """Compare the silence gates in `gates` against a bypassed reference.

    Each gate is written explicitly rather than inherited from the base
    preset: deriving one arm from the base collapses the comparison
    whenever the base already carries the gate under test (e.g. a preset
    generated before the -50 dB default), which would read as "the gate
    makes no difference" — the opposite of the real result.
    """
    stim = _crackle_stimulus()
    stim_path = out_dir / "stimulus_crackle.wav"
    smoke.write_wav_f32(stim_path, stim)

    seen, gate_list = set(), []
    for g in gates:
        if g not in seen:
            seen.add(g)
            gate_list.append(g)
    variants = [(f"{VARIANT_PREFIX}Bypassed", {"bypass": True})]
    variants += [
        (f"{VARIANT_PREFIX}Gate{_tag(g)}",
         {"bypass": False, "silence-threshold": g})
        for g in gate_list
    ]
    bounds, pos = {}, 0
    for name, dur in CRACKLE_SEGMENTS:
        n = int(round(dur * SR))
        bounds[name] = (pos, pos + n)
        pos += n

    print("\n=== crackle protocol (silence wind-up + burst) ===")
    rows = []
    for vname, overrides in variants:
        _make_variant(base, vname, **overrides)
        cap = _capture(vname, stim_path, out_dir)
        off = _align_offset(cap, 0.5, vname)

        def g(name, t0=0.0, t1=None):
            s, e = bounds[name]
            s += int(t0 * SR)
            if t1 is not None:
                e = bounds[name][0] + int(t1 * SR)
            return (_rms_db(cap[s + off:e + off, 0])
                    - _rms_db(stim[s:e, 0]))

        settle = g("settle", 7.0)
        burst_seg = cap[bounds["burst"][0] + off:bounds["burst"][1] + off, 0]
        rows.append((vname, settle, g("floor", 28.0) - settle,
                     g("burst") - settle, float(np.max(np.abs(burst_seg)))))

    print("\n%-24s %8s %8s %16s %9s" % (
        "variant", "settle", "windup", "burst-vs-settle", "burst-pk"))
    for r in rows:
        print("%-24s %7.2f  %7.2f  %15.2f  %8.3f" % r)
    print("windup = gain ride over the -60 dBFS floor (the crackle "
          "mechanism); burst-pk near the limiter ceiling (~0.89) means "
          "the burst is being slammed.")


# ---------------------------------------------------------------------------
# Protocol 2: speech-over-quiet-background overshoot
# ---------------------------------------------------------------------------

PRE_S, SETTLE_S, BURST_S, BG_S, N_BURSTS = 0.5, 6.0, 1.2, 3.0, 4
BG_DB, BURST_DB = -38.0, -18.0


def _speech_stimulus() -> np.ndarray:
    rng = np.random.default_rng(7)
    parts = [np.zeros(int(PRE_S * SR)),
             _pink(int(SETTLE_S * SR), rng) * 10 ** (BG_DB / 20)]
    for _ in range(N_BURSTS):
        n = int(BURST_S * SR)
        t = np.arange(n) / SR
        am = 0.65 + 0.35 * np.sin(2 * np.pi * 4.0 * t)  # syllabic AM
        burst = _pink(n, rng) * am
        burst *= 10 ** (BURST_DB / 20) / np.sqrt(np.mean(burst**2))
        fade = int(0.01 * SR)
        burst[:fade] *= np.linspace(0, 1, fade)
        burst[-fade:] *= np.linspace(1, 0, fade)
        parts.append(burst)
        parts.append(_pink(int(BG_S * SR), rng) * 10 ** (BG_DB / 20))
    parts.append(np.zeros(SR))
    mono = np.concatenate(parts).astype(np.float32)
    return np.column_stack([mono, mono])


def run_speech(base: dict, out_dir: Path,
               histories: list[int], targets: list[float]) -> None:
    stim = _speech_stimulus()
    stim_path = out_dir / "stimulus_speech.wav"
    smoke.write_wav_f32(stim_path, stim)

    def burst_start(k: int) -> float:
        return PRE_S + SETTLE_S + k * (BURST_S + BG_S)

    variants = [(f"{VARIANT_PREFIX}Bypassed", {"bypass": True})]
    for h in histories:
        for t in targets:
            variants.append((f"{VARIANT_PREFIX}H{_tag(h)}T{_tag(t)}",
                             {"bypass": False, "maximum-history": h,
                              "target": t}))
    names = [v[0] for v in variants]
    if len(set(names)) != len(names):
        raise SystemExit(f"duplicate variant names in sweep: {sorted(names)}")

    print("\n=== speech protocol (background boost + onset overshoot) ===")
    results = {}
    for vname, overrides in variants:
        _make_variant(base, vname, **overrides)
        cap = _capture(vname, stim_path, out_dir)
        off = _align_offset(cap, PRE_S, vname)

        def g(t0: float, t1: float) -> float:
            s, e = int(t0 * SR), int(t1 * SR)
            return (_rms_db(cap[s + off:e + off, 0])
                    - _rms_db(stim[s:e, 0]))

        overshoots, voices = [], []
        for k in range(1, N_BURSTS):
            b0 = burst_start(k)
            overshoots.append(g(b0, b0 + 0.25)
                              - g(b0 + BURST_S - 0.3, b0 + BURST_S))
            voices.append(g(b0 + 0.25, b0 + BURST_S))
        bg3 = burst_start(2) - 1.0
        results[vname] = (g(bg3, bg3 + 0.9), float(np.mean(overshoots)),
                          float(np.mean(voices)))

    ref_bg, _, ref_voice = results[f"{VARIANT_PREFIX}Bypassed"]
    print("\n%-24s %9s %10s %8s" % (
        "variant", "bg-boost", "overshoot", "voice"))
    for v, (bg, osh, voice) in results.items():
        print("%-24s %8.2f  %9.2f  %7.2f" % (
            v, bg - ref_bg, osh, voice - ref_voice))
    print("bg-boost & voice are dB vs the bypassed reference; overshoot "
          "is onset-vs-tail within each burst. As shipped, overshoot "
          "~4 dB regardless of history = the reason HDA stays bypassed.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-preset", default="Dolby-Balanced",
                    help="generated EE preset to derive variants from "
                         "(default Dolby-Balanced)")
    ap.add_argument("--protocol", choices=["crackle", "speech", "both"],
                    default="both")
    ap.add_argument("--gates", type=float, nargs="*", default=[-50.0, -70.0],
                    help="silence-threshold values to compare in the crackle "
                         "protocol (default: the shipped -50 vs EE's -70 "
                         "plugin default). Each is written explicitly, so the "
                         "base preset's own gate doesn't affect the arms")
    ap.add_argument("--histories", type=int, nargs="*", default=[20, 32, 40],
                    help="maximum-history values for the speech sweep")
    ap.add_argument("--targets", type=float, nargs="*", default=[-20.0, -23.0],
                    help="target values for the speech sweep")
    ap.add_argument(
        "--out-dir", type=Path,
        default=REPO_ROOT / "localresearch/measure_ee/autogain_dynamics",
        help="capture/stimulus output dir (default: the repo's gitignored "
             "localresearch tree, regardless of the working directory)")
    args = ap.parse_args()

    base_path = EE_OUTPUT_DIR / f"{args.base_preset}.json"
    if not base_path.is_file():
        raise SystemExit(f"base preset not found: {base_path}")
    base = json.loads(base_path.read_text())
    args.out_dir.mkdir(parents=True, exist_ok=True)

    try:
        if args.protocol in ("crackle", "both"):
            run_crackle(base, args.out_dir, args.gates)
        if args.protocol in ("speech", "both"):
            run_speech(base, args.out_dir, args.histories, args.targets)
    finally:
        removed = 0
        for pattern in (f"{VARIANT_PREFIX}*.json", f"{VARIANT_PREFIX}*.json.tmp"):
            for p in EE_OUTPUT_DIR.glob(pattern):
                p.unlink()
                removed += 1
        subprocess.run(["easyeffects", "-l", args.base_preset],
                       capture_output=True)
        print(f"\ncleaned up {removed} variant presets; "
              f"reloaded {args.base_preset!r}. Remember teardown.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
