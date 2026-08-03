#!/usr/bin/env python3
"""Scaling-campaign DAX-side analysis (2026-06 Windows session, post-update).

Reproduces the entry 1/2/6/8/11 + q-mode + Finding-9 results from the
DAX captures in dax_captures/ (analyze.py outputs) vs the prior Linux
EE captures in ee_captures/. Run from the repo root after staging both
dirs (see WINDOWS_CAPTURE_RUNBOOK.md "Bring back to Linux").

Stimulus integrity: the DAX speech sidecar sha256 matches the shipped
espeak stimulus_speech.wav. Volume pinned at 50% (scalar 0.5006); the
DAX OFF raw transfer is -0.01 dB, i.e. WASAPI loopback taps the engine
mix bus PRE-volume, so the master-volume term does not enter captures.
"""
import argparse
import os
from pathlib import Path

import numpy as np

# Defaults point into the untracked localresearch tree, where the capture
# batteries write; --ee-dir/--dax-dir/--dax-archive re-point them at another
# session's staged captures.
_ROOT = Path(__file__).resolve().parents[2]
EE = str(_ROOT / "localresearch/scaling-campaign/ee_captures")
DAX = str(_ROOT / "localresearch/scaling-campaign/dax_captures")
DAXC = str(_ROOT / "localresearch/measure_dax/captures")  # archived stepped (-18) baselines
SR = 48000
BELLS = [(280., 2., 3.), (400., 4.6, 4.), (516., 1.5, -4.)]


def spec(d, tag, label, key="eq_gain_db", ch="L"):
    z = np.load(os.path.join(d, f"spectrum_{tag}_{label}_{ch}.npz"))
    return z["f"], z[key] if key in z.files else z["eq_gain_db"]


def stepped(path):
    z = np.load(path)
    return z["freqs_hz"], z["static_db"], z["adaptive_span_db"]


def n1k(f, c):
    return c - np.interp(1000, f, c)


def rbj(f, f0, q, g):
    A = 10**(g/40); w0 = 2*np.pi*f0/SR; al = np.sin(w0)/(2*q)
    z = np.exp(-1j*2*np.pi*f/SR)
    return 20*np.log10(np.abs(((1+al*A)+(-2*np.cos(w0))*z+(1-al*A)*z**2) /
                              ((1+al/A)+(-2*np.cos(w0))*z+(1-al/A)*z**2))+1e-30)


def rlc(f, f0, q, g):
    gg = 10**(g/20); ang = np.arctan(gg); k = 2*(1/gg+gg)/(1+2*q)
    kt, kb = k*np.sin(ang), k*np.cos(ang)
    wp = 2*SR*np.tan(2*np.pi*f0/(2*SR)); z = np.exp(-1j*2*np.pi*f/SR)
    s = 2*SR*(1-z)/(1+z)/wp
    return 20*np.log10(np.abs((s**2+kt*s+1)/(s**2+kb*s+1))+1e-30)


def finding9():
    print("== Finding 9 confirmation: EE−DAX pink RMS (200 Hz-18 kHz), second session ==")
    for p in ["dynamic", "movie", "music", "game", "voice"]:
        f, ee = spec(EE, "pink", f"ee_{p}_balanced"); _, dx = spec(DAX, "pink", p)
        d = n1k(f, ee) - n1k(f, np.interp(f, f, dx))
        b = (f >= 200) & (f <= 18000)
        hf = float(n1k(f, ee)[np.argmin(np.abs(f-19688))] -
                   n1k(f, dx)[np.argmin(np.abs(f-19688))])
        print(f"  {p:>8}: RMS {np.sqrt(np.mean(d[b]**2)):.2f}  max {np.max(np.abs(d[b])):.2f}  "
              f"19.7kHz Δ {hf:+.1f} dB")


def entry2():
    print("\n== Entry 2 (surround /20): S/M widening, decorrelated pink ==")
    for d, lbl, nm in [(DAX, "off", "DAX off"), (DAX, "dynamic", "DAX s96"),
                       (DAX, "game", "DAX s0"), (EE, "ee_dynamic_balanced", "EE s96"),
                       (EE, "ee_game_balanced", "EE s0")]:
        f, sm = spec(d, "stereo_pink", lbl, key="sm_delta_db")
        b = (f >= 200) & (f <= 18000)
        print(f"  {nm:>8}: median {np.median(sm[b]):+.2f} dB")


def entries_6_11():
    print("\n== Entries 6/11 (dynamics): level-dependent GR, loud(-2) vs normal(-18) ==")
    f, dl, _ = stepped(f"{DAX}/stepped_stepped_loud_dynamic_L.npz")
    _, dn, _ = stepped(f"{DAXC}/stepped_stepped_dynamic_L.npz")
    fe, el, _ = stepped(f"{EE}/stepped_stepped_loud_ee_dynamic_balanced_L.npz")
    _, en, _ = stepped(f"{EE}/stepped_stepped_ee_dynamic_balanced_L.npz")
    ref = (f >= 900) & (f <= 1100)
    gd = (dl-np.mean(dl[ref])) - (dn-np.mean(dn[ref]))
    ge = (el-np.mean(el[ref])) - (en-np.mean(en[ref]))
    print(f"  DAX max GR {gd.min():+.1f} dB @ {int(f[gd.argmin()])} Hz; "
          f"EE max GR {ge.min():+.1f} dB @ {int(fe[ge.argmin()])} Hz")
    for fr in (141, 234, 277, 2250, 3000):
        i = int(np.argmin(np.abs(f-fr)))
        print(f"    {fr:>5} Hz: DAX {gd[i]:+6.2f}  EE {ge[i]:+6.2f}")


def qmode():
    print("\n== q-mode (bell convention): RLC−RBJ signature fit to EE−DAX, 150-800 Hz ==")
    for p in ["dynamic", "movie", "game"]:
        f, ee = spec(EE, "pink", f"ee_{p}_balanced"); _, dx = spec(DAX, "pink", p)
        sel = (f >= 150) & (f <= 800); fb = f[sel]
        r = (n1k(f, ee)-n1k(f, dx))[sel]; r -= np.mean(r)
        dlt = sum(rlc(fb, *b)-rbj(fb, *b) for b in BELLS); dlt -= np.mean(dlt)
        a = np.linalg.lstsq(np.column_stack([dlt, np.ones_like(dlt)]), r, rcond=None)[0][0]
        print(f"  {p:>8}: a={a:+.2f} (a→1 cookbook, a→0 RLC-like; signature std {np.std(dlt):.2f} dB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ee-dir", default=EE, help="EE-side captures (analyze.py outputs)")
    ap.add_argument("--dax-dir", default=DAX, help="DAX-side captures from the same session")
    ap.add_argument("--dax-archive", default=DAXC,
                    help="archived stepped (-18 dBFS) DAX baselines")
    args = ap.parse_args()
    EE, DAX, DAXC = args.ee_dir, args.dax_dir, args.dax_archive
    missing = [p for p in (EE, DAX, DAXC) if not os.path.isdir(p)]
    if missing:
        raise SystemExit("missing capture dir(s) — this report runs over "
                         "captures staged locally, not over anything "
                         "committed:\n  " + "\n  ".join(missing))
    finding9(); entry2(); entries_6_11(); qmode()
