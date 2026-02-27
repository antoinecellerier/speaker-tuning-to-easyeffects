#!/usr/bin/env python3
"""Convert Dolby DAX3 tuning XML to EasyEffects output presets.

Generates minimum-phase FIR impulse responses from the Dolby IEQ target
curves and audio-optimizer speaker correction, then creates EasyEffects
presets using the Convolver plugin for the combined EQ and a parametric
Equalizer for the explicit speaker PEQ filters.

This avoids all parametric bell filter overlap/solver issues — the FIR
directly implements the exact target frequency response.

Output chain:
  - convolver#0: IEQ curve + audio-optimizer (as FIR impulse response)
  - equalizer#0: speaker PEQ bells (explicit parametric filters from Dolby)
"""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy.io import wavfile

XML_PATH = Path(__file__).parent / "DEV_0287_SUBSYS_17AA22E6_PCI_SUBSYS_22E617AA.xml"
OUTPUT_DIR = Path.home() / ".local" / "share" / "easyeffects" / "output"
IRS_DIR = Path.home() / ".local" / "share" / "easyeffects" / "irs"

SAMPLE_RATE = 48000
FIR_LENGTH = 4096  # ~85ms, plenty for EQ

IEQ_CURVES = {
    "Dolby-Balanced": "ieq_balanced",
    "Dolby-Detailed": "ieq_detailed",
    "Dolby-Warm": "ieq_warm",
}


def parse_csv_ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",")]


def parse_xml(path: Path):
    tree = ET.parse(path)
    root = tree.getroot()
    constant = root.find("constant")

    freqs = parse_csv_ints(constant.find("band_20_freq").get("fs_48000"))

    curves = {}
    for el in constant:
        if el.tag.startswith("ieq_"):
            curves[el.tag] = parse_csv_ints(el.get("target"))

    endpoint = root.find(".//endpoint[@type='internal_speaker'][@operating_mode='normal']")
    ieq_amount = 10
    for profile in endpoint.findall("profile"):
        cp = profile.find("tuning-cp")
        if cp is not None:
            enable = cp.find("ieq-enable")
            if enable is not None and enable.get("value") == "1":
                amt = cp.find("ieq-amount")
                if amt is not None:
                    ieq_amount = int(amt.get("value"))
                break

    profile = endpoint.find("profile")
    vlldp = profile.find("tuning-vlldp")

    ao_bands = vlldp.find("audio-optimizer-bands")
    ao_left = parse_csv_ints(ao_bands.find("ch_00").get("value"))
    ao_right = parse_csv_ints(ao_bands.find("ch_01").get("value"))

    peq_filters = []
    for f in vlldp.findall(".//speaker-peq-filters/filter"):
        peq_filters.append({
            "speaker": int(f.get("speaker")),
            "enabled": int(f.get("enabled")),
            "type": int(f.get("type")),
            "f0": float(f.get("f0")),
            "gain": float(f.get("gain", "0")),
            "q": float(f.get("q", "0.707")),
            "order": int(f.get("order", "0")),
        })

    return freqs, curves, ieq_amount, ao_left, ao_right, peq_filters


# --- FIR generation ---

def interpolate_curve_db(band_freqs, band_gains_db, fft_freqs):
    """Interpolate a gain curve (in dB) to FFT frequency bins.

    Uses log-frequency interpolation with linear dB values.
    Extrapolates flat beyond the band edges.
    """
    log_bands = np.log(np.maximum(band_freqs, 1.0))
    log_fft = np.log(np.maximum(fft_freqs, 1.0))
    return np.interp(log_fft, log_bands, band_gains_db,
                     left=band_gains_db[0], right=band_gains_db[-1])


def make_fir(band_freqs, gains_db, normalize=True):
    """Generate a minimum-phase FIR filter from a target dB curve.

    Uses homomorphic processing: the minimum-phase impulse response
    is constructed from the log-magnitude spectrum via the cepstrum.
    """
    n = FIR_LENGTH
    fft_freqs = np.fft.rfftfreq(n, d=1.0 / SAMPLE_RATE)

    # Interpolate target curve to FFT bins
    gains_at_bins = interpolate_curve_db(
        np.array(band_freqs, dtype=float),
        np.array(gains_db, dtype=float),
        fft_freqs
    )

    # Log magnitude (natural log for cepstral processing)
    log_mag = gains_at_bins * (np.log(10.0) / 20.0)  # dB to ln(linear)

    # Minimum-phase via cepstrum:
    # 1. IFFT of log-magnitude gives the real cepstrum
    # 2. Causal windowing (double positive-time, zero negative-time)
    # 3. FFT back gives log(H_min) = log|H| + j*phase_min
    # 4. exp() gives H_min, IFFT gives impulse response
    cepstrum = np.fft.irfft(log_mag, n=n)
    # Causal window: keep n=0, double n=1..N/2-1, zero n=N/2..N-1
    cepstrum[1:n // 2] *= 2.0
    cepstrum[n // 2 + 1:] = 0.0
    # Reconstruct minimum-phase spectrum
    log_H_min = np.fft.rfft(cepstrum, n=n)
    H_min = np.exp(log_H_min)
    fir = np.fft.irfft(H_min, n=n)

    if normalize:
        peak_mag = np.max(np.abs(H_min))
        if peak_mag > 0:
            fir /= peak_mag

    return fir


def save_wav_stereo(path, fir_left, fir_right):
    """Save stereo impulse response as 32-bit float WAV."""
    stereo = np.column_stack([fir_left, fir_right]).astype(np.float32)
    wavfile.write(str(path), SAMPLE_RATE, stereo)


# --- EasyEffects preset builders ---

def make_band(freq: float, gain: float, q=1.5) -> dict:
    return {
        "frequency": freq,
        "gain": round(gain, 4),
        "mode": "RLC (BT)",
        "mute": False,
        "q": q,
        "slope": "x1",
        "solo": False,
        "type": "Bell",
        "width": 4.0,
    }


def make_convolver(kernel_name: str):
    """Convolver plugin config referencing an IR by name.

    EasyEffects 8.x uses kernel-name (filename stem without extension),
    and looks for the WAV in its irs/ directory.
    """
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "kernel-name": kernel_name,
        "ir-width": 100,
    }


def make_peq_eq(peq_filters):
    """Parametric EQ for the explicit speaker PEQ bells from Dolby.

    These are individual filter specs (not target curves), so they
    map directly to parametric EQ bands without overlap issues.
    """
    peq_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    peq_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    num_bands = max(len(peq_left), len(peq_right))

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}
    for j, pf in enumerate(peq_left):
        left_bands[f"band{j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])
    for j, pf in enumerate(peq_right):
        right_bands[f"band{j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])

    # Fill missing
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            left_bands[key] = make_band(1000.0, 0.0)
        if key not in right_bands:
            right_bands[key] = make_band(1000.0, 0.0)

    # Compensate for peak PEQ boost to prevent clipping
    all_peq = peq_left + peq_right
    peak_boost = max((pf["gain"] for pf in all_peq), default=0.0)
    output_gain = -max(peak_boost, 0.0)

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(output_gain, 2),
        "mode": "IIR",
        "num-bands": num_bands,
        "split-channels": True,
        "left": left_bands,
        "right": right_bands,
    }


def make_preset(kernel_name, peq_filters):
    preset = {
        "output": {
            "blocklist": [],
            "convolver#0": make_convolver(kernel_name),
            "plugins_order": ["convolver#0"],
        }
    }

    peq = make_peq_eq(peq_filters)
    if peq:
        preset["output"]["equalizer#0"] = peq
        preset["output"]["plugins_order"].append("equalizer#0")

    return preset


def main():
    freqs, curves, ieq_amount, ao_left, ao_right, peq_filters = parse_xml(XML_PATH)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    IRS_DIR.mkdir(parents=True, exist_ok=True)

    scale = ieq_amount / 10.0
    print(f"ieq-amount: {ieq_amount}/10 (scale: {scale:.2f})")

    # Audio-optimizer curves in dB
    ao_db_left = np.array(ao_left) / 16.0
    ao_db_right = np.array(ao_right) / 16.0
    float_freqs = np.array(freqs, dtype=float)

    print(f"\nAudio-optimizer (dB):")
    print(f"  Left:  {[f'{x:+.1f}' for x in ao_db_left]}")
    print(f"  Right: {[f'{x:+.1f}' for x in ao_db_right]}")

    print(f"\nPEQ filters (kept as parametric EQ):")
    for pf in peq_filters:
        spk = "L" if pf["speaker"] == 0 else "R"
        if pf["type"] == 9:
            print(f"  [{spk}] HP @ {pf['f0']} Hz, order {pf['order']} (skipped)")
        elif pf["type"] == 1:
            print(f"  [{spk}] Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, Q={pf['q']}")
    print()

    for preset_name, curve_key in IEQ_CURVES.items():
        gains_raw = curves[curve_key]
        ieq_db = np.array(gains_raw) / 16.0 * scale

        # Combined target: IEQ + audio-optimizer (summed in dB)
        combined_left = ieq_db + ao_db_left
        combined_right = ieq_db + ao_db_right

        # Generate FIR impulse responses
        fir_left = make_fir(float_freqs, combined_left, normalize=True)
        fir_right = make_fir(float_freqs, combined_right, normalize=True)

        # Save stereo impulse response
        irs_path = IRS_DIR / f"{preset_name}.irs"
        save_wav_stereo(irs_path, fir_left, fir_right)

        # Create preset (kernel-name is the WAV filename stem)
        preset = make_preset(preset_name, peq_filters)
        out_path = OUTPUT_DIR / f"{preset_name}.json"
        out_path.write_text(json.dumps(preset, indent=4) + "\n")

        print(f"Wrote {irs_path}")
        print(f"Wrote {out_path}")
        print(f"  {curve_key} combined IEQ+AO curve (left channel):")
        print(f"  {'freq':>8}  {'IEQ':>6}  {'AO':>6}  {'combined':>8}")
        for i, f in enumerate(freqs):
            print(f"  {f:>7} Hz  {ieq_db[i]:+5.1f}  {ao_db_left[i]:+5.1f}  {combined_left[i]:+7.1f}")

        # Verify FIR frequency response
        H = np.fft.rfft(fir_left, n=FIR_LENGTH)
        fft_freqs = np.fft.rfftfreq(FIR_LENGTH, d=1.0 / SAMPLE_RATE)
        mag_db = 20.0 * np.log10(np.abs(H) + 1e-12)
        # Check response at center frequencies
        print(f"\n  FIR verification (left, normalized to peak=0):")
        for i, f in enumerate(freqs):
            idx = np.argmin(np.abs(fft_freqs - f))
            print(f"  {f:>7} Hz  target: {combined_left[i] - np.max(combined_left):+6.1f}  "
                  f"actual: {mag_db[idx]:+6.1f}  "
                  f"error: {mag_db[idx] - (combined_left[i] - np.max(combined_left)):+5.2f}")
        print()


if __name__ == "__main__":
    main()
