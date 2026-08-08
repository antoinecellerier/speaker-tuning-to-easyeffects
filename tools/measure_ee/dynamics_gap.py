#!/usr/bin/env python3
"""Entries 6/11 follow-up: is the EE↔DAX dynamics GR gap a wrong MBC decode,
the upstream bass/low-mid level gap, or both?

READ-ONLY analysis over existing stepped captures (no audio capture). Run from
the repo root:  python3 tools/measure_ee/dynamics_gap.py

Inputs (analyze.py outputs, peak-normalized PER capture; cross-level compares
align at the 1 kHz dormant band):
  DAX  -2  loud   : scaling-campaign/dax_captures/stepped_stepped_loud_{dynamic,off}_L.npz
  DAX -18 normal  : measure_dax/captures/stepped_stepped_{dynamic,off}_L.npz
  DAX -42 quiet   : measure_dax/captures/stepped_stepped_quiet_{dynamic,off}_L.npz
  EE   -2/-18/-42 : scaling-campaign/ee_captures/stepped_stepped_[quiet_|loud_]ee_dynamic_balanced_L.npz

What it does:
  1. Reproduce the headline loud-vs-normal GR table (DAX & EE) — sanity check.
  2. Build a 3-level GR-vs-input curve per band (−42→−18→−2 = +24, +16 dB steps),
     for DAX and EE, dynamics aligned to OFF (removes static EQ) and to 1 kHz.
  3. Q-a: predict EE GR from the *decoded* MBC (ratio 1.67, thr −6.44, RMS, knee
     −6 dB) at the input level the EE chain actually delivers to its MBC
     (stimulus + post-FIR + post-PEQ magnitude). Compare to measured EE GR.
  4. Q-b: back out DAX's effective threshold+ratio per band from its 3-level
     curve; compare to 1.67/−6.44; estimate the LF/low-mid input-level gap
     (how much more level DAX delivers to its dynamics than EE).
"""
import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lib.dax import parse  # noqa: E402
from lib.preset import fir  # noqa: E402
from lib.preset import bands
from lib.preset import plugins

SR = 48000
# Defaults point into the untracked localresearch tree, where the capture
# batteries write; every one is overridable so a new device's captures can be
# analysed from wherever they were staged.
EE = ROOT / "localresearch/scaling-campaign/ee_captures"
DAX_LOUD = ROOT / "localresearch/scaling-campaign/dax_captures"
DAX_BASE = ROOT / "localresearch/measure_dax/captures"
XML = ROOT / "localresearch/DEV_0287_SUBSYS_17AA22E6_PCI_SUBSYS_22E617AA.xml"

# Stimulus full-scale per level (dBFS of the stepped sine). The campaign used
# -2 (loud), -18 (normal), -42 (quiet).
LEVELS = {"loud": -2.0, "norm": -18.0, "quiet": -42.0}

# Diagnostic probe bands (≥140 Hz; 50/80/120 are pre-attenuated by FIR/HP).
PROBES = [141, 234, 277, 392, 555, 844, 1031, 1949, 2250, 2598, 3000, 4193]


def load(path):
    z = np.load(str(path))
    return z["freqs_hz"].astype(float), z["static_db"].astype(float)


def at(f, c, fr):
    return float(c[int(np.argmin(np.abs(f - fr)))])


def align_1k(f, c):
    """Peak-normalized curve, re-referenced so the 1 kHz region = 0 dB."""
    ref = (f >= 900) & (f <= 1100)
    return c - np.mean(c[ref])


# ---------------------------------------------------------------------------
# Capture paths. Rebuilt by _set_dirs() so --ee-dir/--dax-dir/--dax-archive can
# point the analysis at another session's captures.
DAX = EEP = None


def _set_dirs(ee: Path, dax_loud: Path, dax_base: Path) -> None:
    global DAX, EEP
    DAX = {
        "loud": (dax_loud / "stepped_stepped_loud_dynamic_L.npz",
                 dax_loud / "stepped_stepped_loud_off_L.npz"),
        "norm": (dax_base / "stepped_stepped_dynamic_L.npz",
                 dax_base / "stepped_stepped_off_L.npz"),
        "quiet": (dax_base / "stepped_stepped_quiet_dynamic_L.npz",
                  dax_base / "stepped_stepped_quiet_off_L.npz"),
    }
    EEP = {
        "loud": ee / "stepped_stepped_loud_ee_dynamic_balanced_L.npz",
        "norm": ee / "stepped_stepped_ee_dynamic_balanced_L.npz",
        "quiet": ee / "stepped_stepped_quiet_ee_dynamic_balanced_L.npz",
    }


def headline():
    print("=" * 72)
    print("1. HEADLINE GR (loud -2 vs normal -18, aligned at 1 kHz) — reproduce")
    print("=" * 72)
    f, dl = load(DAX["loud"][0]); _, dn = load(DAX["norm"][0])
    fe, el = load(EEP["loud"]); _, en = load(EEP["norm"])
    gd = align_1k(f, dl) - align_1k(f, dn)
    ge = align_1k(fe, el) - align_1k(fe, en)
    print(f"{'band':>6} {'DAX GR':>8} {'EE GR':>8} {'gap':>8}")
    for fr in (141, 234, 277, 2250, 3000):
        gdv, gev = at(f, gd, fr), at(fe, ge, fr)
        print(f"{fr:>6} {gdv:>+8.2f} {gev:>+8.2f} {gdv - gev:>+8.2f}")


def gr_curve(static_paths, off_path=None):
    """GR(level, band): per-band gain reduction vs the quietest level, with the
    dynamics curve re-referenced to OFF (when given) to strip the static EQ,
    then to 1 kHz. Returns dict band->(quiet,norm,loud) cumulative GR in dB
    relative to the quiet capture (negative = compressed more at higher level).
    """
    aligned = {}
    for lvl, sp in static_paths.items():
        f, c = load(sp)
        a = align_1k(f, c)
        if off_path is not None:
            fo, co = load(off_path[lvl])
            a = a - align_1k(fo, co)  # remove static EQ shape (off is ~flat)
        aligned[lvl] = (f, a)
    f = aligned["quiet"][0]
    out = {}
    for fr in PROBES:
        q = at(f, aligned["quiet"][1], fr)
        n = at(f, aligned["norm"][1], fr)
        l = at(f, aligned["loud"][1], fr)
        # GR relative to quiet baseline (the curve at each level minus quiet)
        out[fr] = (0.0, n - q, l - q)
    return out


def fit_thr_ratio(levels_db, gr_db):
    """Fit an ideal downward compressor: GR(L) = max(0, (L - thr)*(1 - 1/R)).
    Grid-search threshold (in absolute input dBFS at the band) and ratio.
    levels_db = absolute input level at the band for each measured point.
    gr_db = measured *additional* GR at that level (>=0, positive = reduction).
    Returns (thr, ratio, rms_err)."""
    levels_db = np.asarray(levels_db, float)
    gr = np.asarray(gr_db, float)
    best = (None, None, 1e9)
    for thr in np.arange(-50, 5, 0.5):
        excess = np.maximum(levels_db - thr, 0.0)
        # GR = excess * (1 - 1/R) = excess * k  ->  k = lstsq slope, k in [0,1)
        if np.all(excess == 0):
            continue
        k = np.sum(excess * gr) / np.sum(excess * excess)
        k = min(max(k, 0.0), 0.999)
        pred = excess * k
        err = np.sqrt(np.mean((pred - gr) ** 2))
        if err < best[2]:
            ratio = 1.0 / (1.0 - k) if k < 0.999 else 1e3
            best = (thr, ratio, err)
    return best


# ---------------------------------------------------------------------------
# EE chain magnitude delivered to its MBC: post-FIR (convolver) + post-PEQ.
def ee_chain_mag_db(tuning, freqs_probe):
    """dB the EE chain applies before its MBC at each probe freq, exactly as
    the converter builds it (HDA path, convolver_gain=0):
      - convolver#0: peak-normalized FIR of (IEQ_balanced + audio-optimizer), L
      - equalizer#0: speaker PEQ — HP(100,4th) + bells, plus the block-wide
        output-gain the builder applies (-peak_boost to avoid clipping)
      - equalizer#1: dialog enhancer (+gain @ 2.5 kHz) if present
    This is the magnitude that rides on top of the stimulus into the MBC."""
    scale = tuning.ieq_amount / 100.0
    float_freqs = np.array(tuning.freqs, float)
    # Mirrors the gain staging _emit_ieq_presets does on its way to make_fir.
    # A second copy on purpose: this tool has to reproduce what the converter
    # emitted to measure against it, so it is written out rather than called.
    ieq_db = np.array(tuning.curves["ieq_balanced"]) / parse.DB_FIXED_POINT_SCALE * scale
    ao_db_left = np.array(tuning.ao_left) / parse.DB_FIXED_POINT_SCALE
    combined = ieq_db + ao_db_left
    fir_left, _ = fir.make_fir(float_freqs, combined, normalize=True)
    H = np.fft.rfft(fir_left, n=fir.FIR_LENGTH)
    fft_f = np.fft.rfftfreq(fir.FIR_LENGTH, d=1.0 / SR)
    fir_mag = 20 * np.log10(np.abs(H) + fir.LOG_MAG_FLOOR)
    # PEQ (left channel) — read the builder output so the -peak_boost
    # output-gain is included exactly as emitted.
    peq = bands.make_peq_eq([pf for pf in tuning.peq_filters if pf["speaker"] == 0])
    peq_out_gain = peq["output-gain"] if peq else 0.0
    peq_bands = list(peq["left"].values()) if peq else []
    # dialog enhancer (HDA path) — broad +gain @ 2.5 kHz
    de = plugins.make_dialog_enhancer(tuning.dialog_enhancer, is_soundwire=False)
    de_bands = list(de["left"].values()) if de else []
    out = {}
    for fr in freqs_probe:
        idx = int(np.argmin(np.abs(fft_f - fr)))
        fmag = fir_mag[idx]
        pmag = peq_band_sum_db(peq_bands, fr) + peq_out_gain
        dmag = peq_band_sum_db(de_bands, fr)
        out[fr] = (fmag, pmag, dmag, fmag + pmag + dmag)
    return out


def peq_band_sum_db(bands, fr):
    """Sum a list of EE PEQ band-dicts (RLC(BT) bells + HP) at fr, in dB."""
    total = 0.0
    for band in bands:
        if not isinstance(band, dict) or "type" not in band:
            continue
        f0 = band.get("frequency", 0.0)
        gain = band.get("gain", 0.0)
        q = band.get("q", 1.0)
        typ = band.get("type", "")
        slope = band.get("slope", "x1")
        if typ == "Bell":
            total += rlc_bell_db(fr, f0, q, gain)
        elif typ == "Hi-pass":
            order = {"x1": 2, "x2": 4, "x3": 6, "x4": 8}.get(slope, 4)
            total += hp_db(fr, f0, order)
    return total


def rlc_bell_db(f, f0, q, gdb):
    if gdb == 0:
        return 0.0
    g = 10 ** (gdb / 20)
    ang = np.arctan(g)
    k = 2 * (1 / g + g) / (1 + 2 * q)
    kt, kb = k * np.sin(ang), k * np.cos(ang)
    wp = 2 * SR * np.tan(2 * np.pi * f0 / (2 * SR))
    z = np.exp(-1j * 2 * np.pi * f / SR)
    s = 2 * SR * (1 - z) / (1 + z) / wp
    H = (s ** 2 + kt * s + 1) / (s ** 2 + kb * s + 1)
    return float(20 * np.log10(np.abs(H) + 1e-30))


def hp_db(f, f0, order):
    """Butterworth-ish HP magnitude, order N (6 dB/oct per order/2... use
    -order*... actually LSP nSlope=order => order*6 dB/oct? No: order is the
    filter order, 6 dB/oct per order). Use analog Butterworth |H|."""
    n = order
    return float(-10 * np.log10(1 + (f0 / max(f, 1e-9)) ** (2 * n)))


def predicted_mbc_gr(input_dbfs, thr=-6.4375, ratio=1.6685, knee_db=6.0):
    """Ideal downward compressor GR for a steady RMS input at input_dbfs.
    Soft knee of width knee_db centred on thr (LSP knee is total width in dB,
    stored as -6 → 6 dB wide here). Returns GR (>=0)."""
    over = input_dbfs - thr
    half = knee_db / 2.0
    if over <= -half:
        return 0.0
    slope = 1.0 - 1.0 / ratio
    if over >= half:
        return over * slope
    # quadratic soft-knee region
    x = over + half  # 0..knee_db
    return slope * x * x / (2 * knee_db)


def analyse():
    headline()
    tuning = parse.parse_xml(XML, profile_type="dynamic")

    # ---- decoded chain numbers ----
    decoded = plugins.decode_mbc_bands(tuning.mb_comp)
    print("\n" + "=" * 72)
    print("DECODED EE DYNAMICS (from dev XML, profile=dynamic)")
    print("=" * 72)
    xfreq = tuning.freqs
    for i, b in enumerate(decoded):
        xo = xfreq[b["xover_idx"]] if b["xover_idx"] < len(xfreq) else "Nyq"
        print(f"  MBC band{i}: <={xo} Hz  thr={b['threshold']:+.2f}  "
              f"ratio={b['ratio']:.2f}  makeup={b['makeup']:+.1f}  "
              f"atk={b['attack_ms']:.0f}ms rel={b['release_ms']:.0f}ms")
    th = tuning.regulator["threshold_high"]
    print(f"  Regulator thr_high (dB) per 20-band: "
          f"{[f'{x:+.0f}' for x in th]}")
    print(f"  Regulator slope={tuning.regulator['distortion_slope']:.2f} "
          f"(ratio={'100:1' if tuning.regulator['distortion_slope']>=1 else '?'}), "
          f"active below ~{xfreq[max(i for i,x in enumerate(th) if x<0)]} Hz")

    # ---- chain magnitude delivered to MBC ----
    chain = ee_chain_mag_db(tuning, PROBES)
    print("\n" + "=" * 72)
    print("2. EE CHAIN MAGNITUDE DELIVERED TO ITS MBC (dB, vs flat input)")
    print("   (convolver FIR peak-norm + speaker PEQ + dialog; rides on stimulus)")
    print("=" * 72)
    print(f"{'band':>6} {'FIR':>7} {'PEQ':>7} {'DE':>7} {'total':>7}")
    for fr in PROBES:
        fmag, pmag, dmag, tot = chain[fr]
        print(f"{fr:>6} {fmag:>+7.2f} {pmag:>+7.2f} {dmag:>+7.2f} {tot:>+7.2f}")

    # ---- 3-level GR curves ----
    dax_gr = gr_curve({k: DAX[k][0] for k in DAX}, off_path={k: DAX[k][1] for k in DAX})
    ee_gr = gr_curve(EEP)  # no off baseline for EE; rely on 1 kHz alignment

    print("\n" + "=" * 72)
    print("3. Q-a: does EE measured GR match the DECODED MBC at the level the")
    print("   EE chain delivers? (input = stimulus + chain mag; ratio 1.67,")
    print("   thr -6.44, knee 6 dB). loud step = -2 dBFS.")
    print("=" * 72)
    print(f"{'band':>6} {'in@loud':>8} {'predMBC':>8} {'measEE':>8} {'meas-pred':>9}")
    for fr in PROBES:
        chain_db = chain[fr][3]
        in_loud = LEVELS["loud"] + chain_db
        pred = predicted_mbc_gr(in_loud)
        # EE measured GR loud vs norm (the headline metric), positive=reduction
        meas = -(ee_gr[fr][2] - ee_gr[fr][1])
        # predicted incremental GR going norm(-18)->loud(-2):
        pred_norm = predicted_mbc_gr(LEVELS["norm"] + chain_db)
        pred_inc = pred - pred_norm
        print(f"{fr:>6} {in_loud:>+8.2f} {pred_inc:>8.2f} {meas:>8.2f} "
              f"{meas - pred_inc:>+9.2f}")

    print("\n" + "=" * 72)
    print("4. Q-b: DAX effective threshold/ratio per band (3-level fit) vs our")
    print("   decoded 1.67/-6.44. Fit uses absolute input at band = stimulus +")
    print("   DAX's own delivered EQ (dynamics-off shape, 1k-aligned).")
    print("=" * 72)
    # DAX EQ shape (dynamic at quiet level, where dynamics dormant, minus off)
    f, dq = load(DAX["quiet"][0]); fo, oq = load(DAX["quiet"][1])
    dax_eq = align_1k(f, dq) - align_1k(fo, oq)  # dB DAX applies pre-dynamics
    print(f"{'band':>6} {'DAXeq':>7} {'GR@-18':>7} {'GR@-2':>7} "
          f"{'thr':>7} {'ratio':>7} {'err':>6}")
    for fr in PROBES:
        eqdb = at(f, dax_eq, fr)
        # absolute input level at the band for each capture level
        ins = [LEVELS[k] + eqdb for k in ("quiet", "norm", "loud")]
        grs = [-(dax_gr[fr][i]) for i in range(3)]  # positive reduction
        # normalize so quiet=0 reduction (baseline), measure incremental
        grs = [g - grs[0] for g in grs]
        thr, ratio, err = fit_thr_ratio(ins, grs)
        print(f"{fr:>6} {eqdb:>+7.2f} {grs[1]:>7.2f} {grs[2]:>7.2f} "
              f"{thr:>+7.1f} {ratio:>7.2f} {err:>6.2f}")

    print("\n" + "=" * 72)
    print("4b. EE effective threshold/ratio per band (same 3-level fit) — what")
    print("    the EE chain ACTUALLY realises (vs decoded 1.67/-6.44).")
    print("=" * 72)
    print(f"{'band':>6} {'GR@-18':>7} {'GR@-2':>7} {'thr*':>7} {'ratio':>7} {'err':>6}")
    for fr in PROBES:
        chain_db = chain[fr][3]
        ins = [LEVELS[k] + chain_db for k in ("quiet", "norm", "loud")]
        grs = [-(ee_gr[fr][i]) for i in range(3)]
        grs = [g - grs[0] for g in grs]
        thr, ratio, err = fit_thr_ratio(ins, grs)
        print(f"{fr:>6} {grs[1]:>7.2f} {grs[2]:>7.2f} {thr:>+7.1f} "
              f"{ratio:>7.2f} {err:>6.2f}   (*thr in chain-relative dBFS)")

    print("\n" + "=" * 72)
    print("5. INPUT-LEVEL-SHAPE GAP: DAX delivers more level to its dynamics at")
    print("   the diagnostic bands. Both shapes 1 kHz-referenced (1031 Hz = 0).")
    print("   gap = how much hotter DAX's pre-dynamics signal is vs EE's, per band.")
    print("=" * 72)
    ee_ref = chain[1031][3]
    print(f"{'band':>6} {'DAXeq':>7} {'EEchain':>8} {'gap(DAX-EE)':>12}")
    for fr in PROBES:
        eqdb = at(f, dax_eq, fr)
        eedb = chain[fr][3] - ee_ref  # 1k-reference the EE chain shape too
        print(f"{fr:>6} {eqdb:>+7.2f} {eedb:>+8.2f} {eqdb - eedb:>+12.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ee-dir", type=Path, default=EE,
                    help="EE-side stepped captures (analyze.py .npz outputs)")
    ap.add_argument("--dax-dir", type=Path, default=DAX_LOUD,
                    help="DAX-side loud stepped captures")
    ap.add_argument("--dax-archive", type=Path, default=DAX_BASE,
                    help="DAX-side normal/quiet stepped captures")
    ap.add_argument("--xml", type=Path, default=XML,
                    help="tuning XML the EE side was generated from")
    args = ap.parse_args()
    _set_dirs(args.ee_dir, args.dax_dir, args.dax_archive)
    XML = args.xml
    missing = [p for p in (args.ee_dir, args.dax_dir, args.dax_archive, args.xml)
               if not p.exists()]
    if missing:
        raise SystemExit("missing input(s) — this analysis runs over captures "
                         "staged locally, not over anything committed:\n  "
                         + "\n  ".join(str(p) for p in missing))
    analyse()
