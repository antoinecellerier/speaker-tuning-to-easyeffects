#!/usr/bin/env python3
"""On-device EE-vs-PW proof for the autogain -> LSP autogain_stereo translation.

Builds an *autogain-only* EE preset (every other stage stripped from
plugins_order, so only the leveler acts — no convolver/MBC/limiter confounds),
plays a loud->quiet->loud->silence pink stimulus through both the live EE chain
and the PW filter-chain rendering of the same preset, and compares the output
short-term-LUFS trajectories (ffmpeg ebur128).

Subcommands:
  build              write the AGProof EE preset + the stimulus wav
  capture --side X   play stimulus through side X (ee|pw), capture to wav
  analyze            ebur128 both captures, compare segments, print verdict

Reroutes sinks and plays audio: run it through the /audio-validate handoff,
not ad hoc. Stimuli and captures (hundreds of MB) stay untracked under
--out-dir; only this harness is committed.
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools" / "measure_ee"))

DEFAULT_OUT_DIR = REPO / "localresearch" / "measure_pw" / "autogain_proof"
SR = 48000
PRESET_NAME = "AGProof"
PRESET_PATH = Path.home() / ".local/share/easyeffects/output" / f"{PRESET_NAME}.json"

# Set from --out-dir in main(); every artifact this harness reads or writes
# hangs off it, so a run can be pointed at a scratch dir without touching the
# reference captures.
STIM_PATH = EE_CAP = PW_CAP = TRAJ_PATH = None


def _set_out_dir(out_dir: Path) -> None:
    global STIM_PATH, EE_CAP, PW_CAP, TRAJ_PATH
    out_dir.mkdir(parents=True, exist_ok=True)
    STIM_PATH = out_dir / "stim_levelsteps.wav"
    EE_CAP = out_dir / "cap_ee.wav"
    PW_CAP = out_dir / "cap_pw.wav"
    TRAJ_PATH = out_dir / "trajectories.npz"

# Segment plan (seconds, dBFS-RMS of pink). Seg1 settles initial conditions;
# the transitions seg1->2 (boost) and seg2->3 (attenuate) carry the ride-speed
# signal; seg4 tests the silence gate. Target is -22 LUFS (see build()).
SEGMENTS = [
    ("loud_settle", 12.0, -16.0),
    ("quiet",       12.0, -34.0),
    ("loud_again",  12.0, -16.0),
    ("silence",      6.0, None),
]


# --- active autogain block (mirrors make_autogain conservative path) --------
def autogain_block() -> dict:
    # vol_leveler amount=5, out_target=-16 -> conservative path:
    #   maximum-history = max(40 - 5*4, 15) = 20 s ; target = -16 - 6 = -22 LUFS
    return {
        "bypass": False, "input-gain": 0.0, "output-gain": 0.0,
        "maximum-history": 20.0, "reference": "Geometric Mean (MSI)",
        "silence-threshold": -50.0, "target": -22.0,
    }


def build() -> None:
    PRESET_PATH.parent.mkdir(parents=True, exist_ok=True)
    preset = {
        "_generator": "agproof (autogain-only EE-vs-PW proof)",
        "output": {
            "blocklist": [],
            "autogain#0": autogain_block(),
            "plugins_order": ["autogain#0"],
        },
    }
    PRESET_PATH.write_text(json.dumps(preset, indent=2))
    print(f"wrote EE preset: {PRESET_PATH}")

    rng = np.random.default_rng(20260622)
    pieces = []
    for name, dur, dbfs in SEGMENTS:
        n = int(dur * SR)
        if dbfs is None:
            seg = np.zeros((n, 2), dtype=np.float32)
        else:
            seg = _pink(rng, n)
            cur = np.sqrt(np.mean(seg ** 2))
            seg *= (10 ** (dbfs / 20.0)) / max(cur, 1e-12)
            seg = np.column_stack([seg, seg]).astype(np.float32)
        pieces.append(seg)
    stim = np.concatenate(pieces, axis=0)
    np.clip(stim, -1.0, 1.0, out=stim)
    _wav_write_f32(STIM_PATH, stim)
    boundaries = np.cumsum([0.0] + [d for _, d, _ in SEGMENTS])
    print(f"wrote stimulus: {STIM_PATH}  ({stim.shape[0]/SR:.1f}s)")
    print("segment boundaries (s):",
          ", ".join(f"{n}@{boundaries[i]:.0f}-{boundaries[i+1]:.0f}"
                    for i, (n, _, _) in enumerate(SEGMENTS)))


def _pink(rng, n: int) -> np.ndarray:
    """1/f pink noise via FFT shaping of white noise."""
    white = rng.standard_normal(n)
    spec = np.fft.rfft(white)
    f = np.arange(spec.size)
    f[0] = 1
    spec /= np.sqrt(f)
    out = np.fft.irfft(spec, n=n)
    return (out / np.max(np.abs(out) + 1e-12)).astype(np.float32)


def _wav_write_f32(path: Path, data: np.ndarray) -> None:
    import wave
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# --- capture ----------------------------------------------------------------
def capture(side: str) -> None:
    from smoke import play_and_capture
    if side == "ee":
        play_target, cap = "easyeffects_sink", EE_CAP
    elif side == "pw":
        play_target, cap = f"effect_input.{PRESET_NAME}", PW_CAP
    else:
        raise SystemExit("side must be ee|pw")
    print(f"[{side}] play {STIM_PATH.name} -> {play_target}, capture -> {cap.name}")
    play_and_capture(STIM_PATH, "ee_capture.monitor", cap,
                     play_target=play_target, verbose=True)
    print(f"[{side}] captured {cap} ({cap.stat().st_size} bytes)")


# --- analysis ---------------------------------------------------------------
HOP = 0.1   # s, RMS-envelope hop/window


def _wav_read(path: Path) -> np.ndarray:
    # Reuse the scaffolding's format-aware reader (pw-record writes f32, which
    # Python's stdlib `wave` can't parse). Returns mono mix (both ch identical).
    from smoke import read_wav_f32
    _, x = read_wav_f32(path)
    x = np.asarray(x, dtype=np.float64)
    return x.mean(axis=1) if x.ndim == 2 else x


def _rms_env(mono: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """100 ms-hop RMS envelope in dBFS."""
    w = int(HOP * SR)
    n = len(mono) // w
    seg = mono[: n * w].reshape(n, w)
    rms = np.sqrt(np.mean(seg ** 2, axis=1) + 1e-20)
    t = np.arange(n) * HOP
    return t, 20 * np.log10(rms + 1e-12)


def _onset(t, db, thresh=-50.0) -> float:
    """First time the envelope clears `thresh` dBFS = stimulus start."""
    above = np.where(db > thresh)[0]
    return float(t[above[0]]) if above.size else 0.0


def _seg_window(name: str):
    b = np.cumsum([0.0] + [d for _, d, _ in SEGMENTS])
    for i, (n, _, _) in enumerate(SEGMENTS):
        if n == name:
            return b[i], b[i + 1]
    raise KeyError(name)


def _settled(t, db, t0, name, tail=4.0):
    lo, hi = _seg_window(name)
    m = (t >= t0 + hi - tail) & (t < t0 + hi)
    return float(np.median(db[m])) if m.any() else float("nan")


def _rise_time(t, db, t0, seg_from, seg_to):
    """Time (s) after the seg_to onset for the envelope to cover 90% of the
    move between the two segments' settled levels."""
    onset = t0 + _seg_window(seg_to)[0]
    start = _settled(t, db, t0, seg_from)
    end = _settled(t, db, t0, seg_to)
    tgt = start + 0.9 * (end - start)
    rising = end > start
    for tt, ss in zip(t, db):
        if tt < onset:
            continue
        if (rising and ss >= tgt) or (not rising and ss <= tgt):
            return tt - onset
    return float("nan")


def _ebur128_I(wav: Path, ss: float, dur: float) -> float:
    """Integrated LUFS of a trimmed window (absolute target check)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{ss:.3f}", "-t",
         f"{dur:.3f}", "-i", str(wav), "-af", "ebur128", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    m = re.findall(r"I:\s*(-?[\d.]+)\s*LUFS", proc.stderr)
    return float(m[-1]) if m else float("nan")


def analyze() -> None:
    if not EE_CAP.exists() or not PW_CAP.exists():
        raise SystemExit("missing captures; run capture --side ee and --side pw first")
    ee, pw = _wav_read(EE_CAP), _wav_read(PW_CAP)
    ee_t, ee_db = _rms_env(ee)
    pw_t, pw_db = _rms_env(pw)
    ee0, pw0 = _onset(ee_t, ee_db), _onset(pw_t, pw_db)
    print(f"capture len EE {len(ee)/SR:.1f}s PW {len(pw)/SR:.1f}s | "
          f"onset EE {ee0:.2f}s PW {pw0:.2f}s")

    print("\n=== Settled output RMS per segment (dBFS, aligned to onset) ===")
    print(f"{'segment':<12}{'in dBFS':>9}{'EE':>9}{'PW':>9}{'EE-PW':>9}{'EE gain':>9}")
    for name, _, dbfs in SEGMENTS:
        e = _settled(ee_t, ee_db, ee0, name)
        p = _settled(pw_t, pw_db, pw0, name)
        g = "" if dbfs is None else f"{e - dbfs:+.1f}"
        ind = "silence" if dbfs is None else f"{dbfs:.0f}"
        print(f"{name:<12}{ind:>9}{e:>9.2f}{p:>9.2f}{e - p:>9.2f}{g:>9}")

    print("\n=== (a) convergence to target (-22 LUFS) — integrated LUFS of settled tails ===")
    for name in ("loud_settle", "quiet", "loud_again"):
        lo, hi = _seg_window(name)
        eI = _ebur128_I(EE_CAP, ee0 + hi - 5.0, 4.0)
        pI = _ebur128_I(PW_CAP, pw0 + hi - 5.0, 4.0)
        print(f"  {name:<12} EE {eI:+.2f} LUFS  PW {pI:+.2f}  "
              f"(Δtarget EE {eI+22:+.2f} PW {pI+22:+.2f}, EE-PW {eI-pI:+.2f})")

    print("\n=== (b) silence gate (output RMS during silence; lower = better) ===")
    se = _settled(ee_t, ee_db, ee0, "silence")
    sp = _settled(pw_t, pw_db, pw0, "silence")
    print(f"  EE {se:.1f} dBFS   PW {sp:.1f} dBFS")

    print("\n=== (c) ride speed (90% rise/fall time of the gain ride) ===")
    for a, b in (("loud_settle", "quiet"), ("quiet", "loud_again")):
        re_ = _rise_time(ee_t, ee_db, ee0, a, b)
        rp_ = _rise_time(pw_t, pw_db, pw0, a, b)
        ratio = rp_ / re_ if re_ and not np.isnan(re_) else float("nan")
        print(f"  {a}->{b:<11} EE {re_:6.2f}s  PW {rp_:6.2f}s  (PW/EE {ratio:.2f})")

    np.savez(TRAJ_PATH,
             ee_t=ee_t - ee0, ee_db=ee_db, pw_t=pw_t - pw0, pw_db=pw_db)
    print(f"\nsaved trajectories -> {TRAJ_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help="where the stimulus, captures and trajectories live "
                         "(default: the untracked localresearch tree, "
                         "regardless of the working directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    cp = sub.add_parser("capture")
    cp.add_argument("--side", required=True, choices=["ee", "pw"])
    sub.add_parser("analyze")
    args = ap.parse_args()
    _set_out_dir(args.out_dir)
    if args.cmd == "build":
        build()
    elif args.cmd == "capture":
        capture(args.side)
    elif args.cmd == "analyze":
        analyze()
