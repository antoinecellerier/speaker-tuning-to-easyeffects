#!/usr/bin/env python3
"""Full-chain EE-vs-PW clipping/pumping test for force-enabled autogain.

Recreates the *original* failure mode that motivated bypassing autogain on HDA:
the full HDA chain (steep IEQ+AO convolver -> ... -> autogain -> MBC -> limiter)
with autogain FORCED ACTIVE. A long quiet segment lets the leveler ramp gain up,
then a hard loud onset arrives while gain is still high -> overshoot into the
brickwall limiter (-1 dBFS). Compares EE (libebur128) vs PW (autogain_stereo):
does PW reproduce or *worsen* EE's limiting/pumping on the transient?

  build              force autogain active in the AGFull preset + write stimulus
  capture --side X   play stimulus through side X (ee|pw), capture
  analyze            per-segment level/peak/crest + loud-onset overshoot + verdict
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools" / "measure_ee"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Sibling harness in this directory: reuse _pink, _wav_write_f32, _rms_env,
# _onset, _wav_read rather than carrying a second copy of the signal helpers.
import autogain_proof as ap

DEFAULT_OUT_DIR = REPO / "localresearch" / "measure_pw" / "autogain_fullchain"
SR = 48000
PRESET_NAME = "AGFull-Dynamic-Balanced"
NODE_NAME = "AGFull"   # PW filter-chain node name (effect_input.AGFull)
PRESET_PATH = Path.home() / ".local/share/easyeffects/output" / f"{PRESET_NAME}.json"

# Set from --out-dir in main(), as in the sibling harness.
STIM = EE_CAP = PW_CAP = None


def _set_out_dir(out_dir: Path) -> None:
    global STIM, EE_CAP, PW_CAP
    out_dir.mkdir(parents=True, exist_ok=True)
    STIM = out_dir / "stim_transient.wav"
    EE_CAP = out_dir / "cap_ee_full.wav"
    PW_CAP = out_dir / "cap_pw_full.wav"
LIMITER_CEIL_DB = -1.0

# Worst-case transient: settle, long quiet (gain ramps UP), hard loud onset
# (leftover high gain overshoots into the limiter), hold.
SEGMENTS = [
    ("settle",     6.0, -20.0),
    ("quiet",     18.0, -45.0),
    ("loud_onset", 6.0,  -8.0),
    ("loud_hold",  8.0,  -8.0),
]


def build() -> None:
    d = json.loads(PRESET_PATH.read_text())
    ag = d["output"]["autogain#0"]
    ag["bypass"] = False   # FORCE ACTIVE (HDA default is bypass=true)
    PRESET_PATH.write_text(json.dumps(d, indent=2))
    print(f"forced autogain active in {PRESET_PATH.name}: {json.dumps(ag)}")

    rng = np.random.default_rng(20260622)
    pieces = []
    for _, dur, dbfs in SEGMENTS:
        n = int(dur * SR)
        seg = ap._pink(rng, n)
        seg *= (10 ** (dbfs / 20.0)) / max(np.sqrt(np.mean(seg ** 2)), 1e-12)
        pieces.append(np.column_stack([seg, seg]).astype(np.float32))
    stim = np.concatenate(pieces, axis=0)
    np.clip(stim, -1, 1, out=stim)
    ap._wav_write_f32(STIM, stim)
    b = np.cumsum([0.0] + [s[1] for s in SEGMENTS])
    print(f"wrote {STIM.name} ({stim.shape[0]/SR:.0f}s); "
          f"loud onset at {b[2]:.0f}s")


def capture(side: str) -> None:
    from smoke import play_and_capture
    if side == "ee":
        play_target, cap = "easyeffects_sink", EE_CAP
    else:
        play_target, cap = f"effect_input.{NODE_NAME}", PW_CAP
    print(f"[{side}] {STIM.name} -> {play_target} -> {cap.name}")
    play_and_capture(STIM, "ee_capture.monitor", cap, play_target=play_target)
    print(f"[{side}] captured ({cap.stat().st_size} bytes)")


def _seg_win(name):
    b = np.cumsum([0.0] + [s[1] for s in SEGMENTS])
    for i, (n, _, _) in enumerate(SEGMENTS):
        if n == name:
            return b[i], b[i + 1]
    raise KeyError(name)


def _peak_db(mono, t0, lo, hi):
    s = mono[int((t0 + lo) * SR):int((t0 + hi) * SR)]
    return 20 * np.log10(np.max(np.abs(s)) + 1e-12) if len(s) else float("nan")


def _ceiling_frac(mono, t0, lo, hi, ceil_db=LIMITER_CEIL_DB, tol=0.5):
    s = np.abs(mono[int((t0 + lo) * SR):int((t0 + hi) * SR)])
    if not len(s):
        return float("nan")
    return float(np.mean(20 * np.log10(s + 1e-12) >= ceil_db - tol))


def _side(tag, cap):
    mono = ap._wav_read(cap)
    t, db = ap._rms_env(mono)
    t0 = ap._onset(t, db)
    print(f"\n--- {tag}  (onset {t0:.2f}s, len {len(mono)/SR:.1f}s, "
          f"clipped samples |x|>=0.999: {int(np.sum(np.abs(mono)>=0.999))}) ---")
    print(f"{'segment':<11}{'in':>6}{'RMSout':>9}{'gain':>7}{'peak':>8}{'crest':>7}{'@ceil%':>8}")
    rows = {}
    for name, _, dbfs in SEGMENTS:
        lo, hi = _seg_win(name)
        m = (t >= t0 + lo) & (t < t0 + hi)
        rms = float(np.median(db[m])) if m.any() else float("nan")
        pk = _peak_db(mono, t0, lo, hi)
        cf = pk - rms
        ceil = 100 * _ceiling_frac(mono, t0, lo, hi)
        rows[name] = dict(rms=rms, pk=pk, cf=cf, ceil=ceil)
        print(f"{name:<11}{dbfs:>6.0f}{rms:>9.2f}{rms-dbfs:>+7.1f}{pk:>8.2f}{cf:>7.1f}{ceil:>8.1f}")
    # loud-onset overshoot windows (first 0.5/1/3 s after the onset)
    lo = _seg_win("loud_onset")[0]
    print("  onset overshoot peak dBFS:",
          " ".join(f"{w}s={_peak_db(mono,t0,lo,lo+w):+.2f}" for w in (0.5, 1.0, 3.0)))
    return rows


def analyze() -> None:
    ee = _side("EE  (libebur128 autogain, full HDA chain)", EE_CAP)
    pw = _side("PW  (autogain_stereo, full HDA chain)", PW_CAP)
    print("\n=== EE vs PW on the loud-onset transient (the clip/pump stress) ===")
    for name in ("loud_onset", "loud_hold"):
        e, p = ee[name], pw[name]
        print(f"  {name:<11} peak EE {e['pk']:+.2f} PW {p['pk']:+.2f} (Δ{e['pk']-p['pk']:+.2f}) | "
              f"@ceil EE {e['ceil']:.1f}% PW {p['ceil']:.1f}% | "
              f"crest EE {e['cf']:.1f} PW {p['cf']:.1f} dB")
    print("\nHigher peak / higher @ceil% / lower crest on PW than EE = PW limits/"
          "pumps the transient harder than EE (the risk).")


if __name__ == "__main__":
    a = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                   help="where the stimulus and captures live (default: the "
                        "untracked localresearch tree, regardless of the "
                        "working directory)")
    s = a.add_subparsers(dest="cmd", required=True)
    s.add_parser("build")
    c = s.add_parser("capture"); c.add_argument("--side", required=True, choices=["ee", "pw"])
    s.add_parser("analyze")
    args = a.parse_args()
    _set_out_dir(args.out_dir)
    ap._set_out_dir(args.out_dir)   # sibling's paths back the shared helpers
    {"build": build, "analyze": analyze}.get(args.cmd, lambda: capture(args.side))()
