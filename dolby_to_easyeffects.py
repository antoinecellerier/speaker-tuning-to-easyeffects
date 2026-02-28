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

import argparse
import glob
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy.io import wavfile

DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "share" / "easyeffects" / "output"
DEFAULT_IRS_DIR = Path.home() / ".local" / "share" / "easyeffects" / "irs"

SAMPLE_RATE = 48000
FIR_LENGTH = 4096  # ~85ms, plenty for EQ


def parse_csv_ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",")]


def get_audio_subsystem_ids():
    """Read HDA codec subsystem IDs from /proc/asound.

    Returns a list of (vendor_id, subsystem_id) tuples as uppercase hex
    strings, e.g. [("10EC0287", "17AA22E6")].
    """
    results = []
    for codec_path in sorted(Path("/proc/asound").glob("card*/codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        vendor_id = None
        subsys_id = None
        for line in text.splitlines():
            if line.startswith("Vendor Id:"):
                vendor_id = line.split("0x", 1)[-1].strip().upper()
            elif line.startswith("Subsystem Id:"):
                subsys_id = line.split("0x", 1)[-1].strip().upper()
        if vendor_id and subsys_id:
            results.append((vendor_id, subsys_id))
    return results


def find_tuning_xml(windows_root: Path):
    """Find the DAX3 tuning XML matching this machine's audio hardware.

    Searches the Windows DriverStore for DAX3 tuning XMLs and matches
    against the audio codec's subsystem ID from /proc/asound.
    """
    codecs = get_audio_subsystem_ids()
    if not codecs:
        raise FileNotFoundError(
            "No HDA codecs found in /proc/asound. "
            "Cannot auto-detect audio hardware."
        )

    # Extract just the subsystem IDs for matching
    subsys_ids = {s.upper() for _, s in codecs}

    # Search DriverStore for DAX3 tuning XMLs
    driver_store = windows_root / "System32" / "DriverStore" / "FileRepository"
    if not driver_store.is_dir():
        raise FileNotFoundError(
            f"DriverStore not found at {driver_store}. "
            f"Is '{windows_root}' the correct Windows directory?"
        )

    # Look for dax3_ext_*.inf_* directories
    candidates = []
    for dax_dir in sorted(driver_store.glob("dax3_ext_*.inf_*")):
        for xml_file in sorted(dax_dir.glob("DEV_*_SUBSYS_*.xml")):
            # Skip settings files
            if xml_file.name.endswith("_settings.xml"):
                continue
            # Extract subsystem ID from filename: DEV_XXXX_SUBSYS_YYYYYYYY_...
            match = re.search(r"SUBSYS_([0-9A-Fa-f]{8})", xml_file.name)
            if match:
                file_subsys = match.group(1).upper()
                if file_subsys in subsys_ids:
                    candidates.append(xml_file)

    if not candidates:
        codec_info = ", ".join(
            f"vendor={v} subsys={s}" for v, s in codecs
        )
        raise FileNotFoundError(
            f"No matching DAX3 tuning XML found in {driver_store}. "
            f"Detected codecs: {codec_info}"
        )

    if len(candidates) > 1:
        # Prefer the highest tuning version from the XML metadata
        def xml_sort_key(path):
            try:
                root = ET.parse(path).getroot()
                tv = root.find("tuning_version")
                version = int(tv.get("value", "0")) if tv is not None else 0
            except (ET.ParseError, ValueError, AttributeError):
                version = 0
            return version

        candidates.sort(key=xml_sort_key, reverse=True)
        print(f"Multiple matching XMLs found, using highest tuning version:")
        for c in candidates:
            try:
                root = ET.parse(c).getroot()
                tv = root.find("tuning_version")
                ver = tv.get("value", "?") if tv is not None else "?"
            except ET.ParseError:
                ver = "?"
            marker = "→ " if c == candidates[0] else "  "
            print(f"  {marker}{c} (tuning_version={ver})")

    return candidates[0]


def list_endpoints(path: Path):
    """Print available endpoints and profiles in the XML."""
    tree = ET.parse(path)
    root = tree.getroot()
    for ep in root.findall(".//endpoint"):
        ep_type = ep.get("type")
        op_mode = ep.get("operating_mode")
        profiles = [p.get("type") for p in ep.findall("profile")]
        print(f"  endpoint: {ep_type} (operating_mode={op_mode})")
        for p in profiles:
            print(f"    profile: {p}")


def parse_xml(path: Path, endpoint_type="internal_speaker",
              operating_mode="normal", profile_type=None):
    tree = ET.parse(path)
    root = tree.getroot()
    constant = root.find("constant")

    freqs = parse_csv_ints(constant.find("band_20_freq").get("fs_48000"))

    curves = {}
    for el in constant:
        if el.tag.startswith("ieq_"):
            curves[el.tag] = parse_csv_ints(el.get("target"))

    endpoint = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if endpoint is None:
        raise ValueError(
            f"Endpoint type='{endpoint_type}' operating_mode='{operating_mode}' "
            f"not found. Use --list to see available endpoints."
        )

    # Select the profile for vlldp settings (AO, PEQ, MB compressor)
    if profile_type:
        profile = endpoint.find(f"profile[@type='{profile_type}']")
        if profile is None:
            available = [p.get("type") for p in endpoint.findall("profile")]
            raise ValueError(
                f"Profile '{profile_type}' not found. "
                f"Available: {', '.join(available)}"
            )
    else:
        profile = endpoint.find("profile")

    # IEQ amount from the selected profile's tuning-cp (or first with IEQ enabled)
    ieq_amount = 10
    cp = profile.find("tuning-cp")
    if cp is not None:
        enable = cp.find("ieq-enable")
        if enable is not None and enable.get("value") == "1":
            amt = cp.find("ieq-amount")
            if amt is not None:
                ieq_amount = int(amt.get("value"))

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

    # Volume leveler settings (from tuning-cp of the selected profile)
    vol_leveler = None
    if cp is not None:
        vl_enable = cp.find("volume-leveler-enable")
        if vl_enable is not None:
            vl_amount = cp.find("volume-leveler-amount")
            vl_in = cp.find("volume-leveler-in-target")
            vl_out = cp.find("volume-leveler-out-target")
            vol_leveler = {
                "enable": int(vl_enable.get("value")),
                "amount": int(vl_amount.get("value")) if vl_amount is not None else 0,
                "in_target": int(vl_in.get("value")) / 16.0 if vl_in is not None else -20.0,
                "out_target": int(vl_out.get("value")) / 16.0 if vl_out is not None else -20.0,
            }

    # Multi-band compressor settings (from tuning-vlldp)
    mb_comp = None
    mbc_enable = vlldp.find("mb-compressor-enable")
    if mbc_enable is not None and mbc_enable.get("value") == "1":
        mbc_tuning = vlldp.find("mb-compressor-tuning")
        if mbc_tuning is not None:
            group_count = int(mbc_tuning.find("group_count").get("value"))
            band_groups = []
            for i in range(4):
                bg = mbc_tuning.find(f"band_group_{i}")
                if bg is not None:
                    band_groups.append(parse_csv_ints(bg.get("value")))
            target_power = vlldp.find("mb-compressor-target-power-level")
            # volmax-boost is in tuning-cp
            volmax = cp.find("volmax-boost") if cp is not None else None
            # Also grab regulator stress for additional context
            reg_stress = vlldp.find("regulator-stress-amount")
            mb_comp = {
                "group_count": group_count,
                "band_groups": band_groups,
                "target_power": int(target_power.get("value")) / 16.0 if target_power is not None else -5.0,
                "volmax_boost": int(volmax.get("value")) / 16.0 if volmax is not None else 0.0,
                "reg_stress": parse_csv_ints(reg_stress.get("value")) if reg_stress is not None else [],
            }

    return freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, mb_comp


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


def make_hp_band(freq: float, order: int) -> dict:
    """High-pass filter band for speaker protection."""
    # order 4 = 24 dB/oct = x4 slope
    slope_map = {1: "x1", 2: "x2", 3: "x3", 4: "x4"}
    return {
        "frequency": freq,
        "gain": 0.0,
        "mode": "RLC (BT)",
        "mute": False,
        "q": 0.707,
        "slope": slope_map.get(order, "x4"),
        "solo": False,
        "type": "Hi-pass",
        "width": 4.0,
    }


def make_peq_eq(peq_filters):
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Includes both bell filters and high-pass filters from the
    speaker-peq-filters section. The HP protects laptop speakers
    from sub-bass energy they can't reproduce.
    """
    peq_left_bells = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    peq_right_bells = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    hp_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 9]
    hp_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 9]

    num_bells = max(len(peq_left_bells), len(peq_right_bells))
    num_hp = max(len(hp_left), len(hp_right))
    num_bands = num_hp + num_bells

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    # HP filters first
    for j, pf in enumerate(hp_left):
        left_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])
    for j, pf in enumerate(hp_right):
        right_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])

    # Bell filters after
    for j, pf in enumerate(peq_left_bells):
        left_bands[f"band{num_hp + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])
    for j, pf in enumerate(peq_right_bells):
        right_bands[f"band{num_hp + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])

    # Fill missing
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            if idx < num_hp:
                left_bands[key] = make_hp_band(100.0, 4)
            else:
                left_bands[key] = make_band(1000.0, 0.0)
        if key not in right_bands:
            if idx < num_hp:
                right_bands[key] = make_hp_band(100.0, 4)
            else:
                right_bands[key] = make_band(1000.0, 0.0)

    # Compensate for peak PEQ boost to prevent clipping
    all_peq = peq_left_bells + peq_right_bells
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


def make_autogain(vol_leveler):
    """Autogain plugin mapping from Dolby volume leveler.

    The Dolby volume leveler brings quiet passages up to a target loudness.
    EasyEffects' autogain does the same using EBU R 128 loudness measurement.

    Dolby volume-leveler-amount (0-2) maps to aggressiveness:
      0 = gentle (long history window)
      2 = aggressive (short history window)
    """
    if not vol_leveler or not vol_leveler["enable"]:
        return None

    # Map Dolby amount (0-2) to maximum-history (seconds).
    # Higher amount = shorter window = more aggressive leveling.
    # amount 0 → 30s (gentle), amount 2 → 10s (aggressive)
    amount = vol_leveler["amount"]
    max_history = max(30 - amount * 10, 5)

    # Dolby target is in dBFS; use as LUFS target (reasonable approximation)
    target = vol_leveler["out_target"]

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "maximum-history": max_history,
        "reference": "Geometric Mean (MSI)",
        "silence-threshold": -70.0,
        "target": round(target, 1),
    }


def decode_mbc_time_constant(coeff, block_size=256):
    """Decode a Dolby time constant coefficient to milliseconds.

    Dolby stores time constants as exponential smoothing coefficients
    in Q15 fixed-point format, operating per block (not per sample).
    coeff/32768 = (1 - alpha), where alpha = 1 - exp(-1/(tau * blocks_per_sec)).
    """
    blocks_per_sec = SAMPLE_RATE / block_size
    one_minus_alpha = coeff / 32768.0
    if one_minus_alpha <= 0.0 or one_minus_alpha >= 1.0:
        return 100.0  # fallback
    tau = -1.0 / (blocks_per_sec * math.log(one_minus_alpha))
    return tau * 1000.0  # seconds to ms


def make_multiband_compressor(mb_comp, freqs):
    """Multi-band compressor mapping from Dolby mb-compressor-tuning.

    The Dolby MB compressor uses raw DSP coefficients in 6-tuples:
      [crossover_band_idx, threshold_q4, gain_coeff_q15,
       attack_coeff_q15, release_coeff_q15, makeup_q4]

    Where:
      - crossover_band_idx: index into the 20-band frequency table
      - threshold: in 1/16 dB
      - gain_coeff: Q15 fixed-point, 32767 = unity (bypass)
        ratio ≈ 1 / (gain_coeff / 32768)
      - attack/release: exponential smoothing coefficients (block-rate)
      - makeup: in 1/16 dB
    """
    if not mb_comp or mb_comp["group_count"] < 2:
        return None

    band_groups = mb_comp["band_groups"]
    if len(band_groups) < 2:
        return None

    def decode_band(bg):
        xover_idx, thresh_raw, gain_raw, attack_raw, release_raw, makeup_raw = bg
        threshold = thresh_raw / 16.0
        # gain_coeff → ratio: 32767 = 1:1 (bypass), lower = more compression
        gain_frac = gain_raw / 32768.0
        ratio = 1.0 / gain_frac if gain_frac > 0.01 else 100.0
        attack_ms = decode_mbc_time_constant(attack_raw)
        release_ms = decode_mbc_time_constant(release_raw)
        makeup = makeup_raw / 16.0
        return {
            "xover_idx": xover_idx,
            "threshold": threshold,
            "ratio": ratio,
            "attack_ms": attack_ms,
            "release_ms": release_ms,
            "makeup": makeup,
        }

    band0 = decode_band(band_groups[0])
    band1 = decode_band(band_groups[1])

    # Crossover frequency from band index into the 20-freq table
    xover_idx = band0["xover_idx"]
    if 0 <= xover_idx < len(freqs):
        crossover_freq = float(freqs[xover_idx])
    else:
        crossover_freq = 500.0  # fallback

    # Build EasyEffects multiband compressor with 2 active bands
    # Band 0 = low (below crossover), Band 1 = high (above crossover)
    # Bands 2-7 are disabled
    result = {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(mb_comp["volmax_boost"], 1),
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i == 0:
            # Low band — always enabled, no split-frequency
            b = band0
            result[bandn] = {
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 1),
                "attack-time": round(b["attack_ms"], 1),
                "release-threshold": -80.01,
                "release-time": round(b["release_ms"], 1),
                "ratio": round(b["ratio"], 2),
                "knee": -6.0,
                "makeup": round(b["makeup"], 1),
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": crossover_freq,
                "boost-threshold": -72.0,
                "boost-amount": 6.0,
            }
        elif i == 1:
            # High band
            b = band1
            result[bandn] = {
                "enable-band": True,
                "split-frequency": crossover_freq,
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 1),
                "attack-time": round(b["attack_ms"], 1),
                "release-threshold": -80.01,
                "release-time": round(b["release_ms"], 1),
                "ratio": round(b["ratio"], 2),
                "knee": -6.0,
                "makeup": round(b["makeup"], 1),
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": crossover_freq,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -72.0,
                "boost-amount": 6.0,
            }
        else:
            # Disabled bands
            result[bandn] = {
                "enable-band": False,
                "compressor-enable": False,
                "mute": False,
                "solo": False,
                "attack-threshold": -12.0,
                "attack-time": 20.0,
                "release-threshold": -80.01,
                "release-time": 100.0,
                "ratio": 1.0,
                "knee": -6.0,
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -72.0,
                "boost-amount": 6.0,
            }

    return result


def make_preset(kernel_name, peq_filters, vol_leveler=None, mb_comp=None, freqs=None):
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

    mbc = make_multiband_compressor(mb_comp, freqs)
    if mbc:
        preset["output"]["multiband_compressor#0"] = mbc
        preset["output"]["plugins_order"].append("multiband_compressor#0")

    autogain = make_autogain(vol_leveler)
    if autogain:
        preset["output"]["autogain#0"] = autogain
        preset["output"]["plugins_order"].append("autogain#0")

    return preset


def main():
    parser = argparse.ArgumentParser(
        description="Convert Dolby DAX3 tuning XML to EasyEffects output presets.",
    )
    parser.add_argument(
        "xml_file",
        nargs="?",
        type=Path,
        default=None,
        help="path to the Dolby DAX3 tuning XML (e.g. DEV_0287_SUBSYS_*.xml)",
    )
    parser.add_argument(
        "--windows",
        type=Path,
        default=None,
        metavar="DIR",
        help="path to a mounted Windows directory (e.g. /mnt/windows/Windows); "
             "auto-discovers the correct tuning XML by matching the audio "
             "codec subsystem ID from /proc/asound",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"EasyEffects output preset directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--irs-dir",
        type=Path,
        default=DEFAULT_IRS_DIR,
        help=f"EasyEffects impulse response directory (default: {DEFAULT_IRS_DIR})",
    )
    parser.add_argument(
        "--prefix",
        default="Dolby",
        help="prefix for preset names (default: Dolby → Dolby-Balanced, etc.)",
    )
    parser.add_argument(
        "--endpoint",
        default="internal_speaker",
        help="endpoint type from the XML (default: internal_speaker)",
    )
    parser.add_argument(
        "--mode",
        default="normal",
        help="endpoint operating mode (default: normal)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="profile type, e.g. dynamic, music, voice (default: first profile)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list available endpoints and profiles, then exit",
    )
    args = parser.parse_args()

    # Resolve the XML file path
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")
    elif args.windows:
        xml_path = find_tuning_xml(args.windows)
        print(f"Auto-detected: {xml_path}")
    elif args.xml_file:
        xml_path = args.xml_file
    else:
        parser.error("either xml_file or --windows is required")

    if args.list:
        print(f"Endpoints and profiles in {xml_path}:")
        list_endpoints(xml_path)
        return

    print(f"Endpoint: {args.endpoint} (mode={args.mode})")
    print(f"Profile: {args.profile or '(first)'}")

    freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, mb_comp = parse_xml(
        xml_path,
        endpoint_type=args.endpoint,
        operating_mode=args.mode,
        profile_type=args.profile,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.irs_dir.mkdir(parents=True, exist_ok=True)

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
            print(f"  [{spk}] HP @ {pf['f0']} Hz, order {pf['order']} ({pf['order'] * 6} dB/oct)")
        elif pf["type"] == 1:
            print(f"  [{spk}] Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, Q={pf['q']}")

    if vol_leveler:
        print(f"\nVolume leveler: {'enabled' if vol_leveler['enable'] else 'disabled'}")
        print(f"  amount: {vol_leveler['amount']}")
        print(f"  in-target: {vol_leveler['in_target']:.1f} dB")
        print(f"  out-target: {vol_leveler['out_target']:.1f} dB")

    if mb_comp:
        print(f"\nMulti-band compressor: {mb_comp['group_count']} bands")
        print(f"  target-power-level: {mb_comp['target_power']:.1f} dB")
        print(f"  volmax-boost: {mb_comp['volmax_boost']:.1f} dB")
        for i, bg in enumerate(mb_comp["band_groups"][:mb_comp["group_count"]]):
            xover_idx = bg[0]
            xover_hz = freqs[xover_idx] if 0 <= xover_idx < len(freqs) else "?"
            thresh = bg[1] / 16.0
            ratio_frac = bg[2] / 32768.0
            ratio = 1.0 / ratio_frac if ratio_frac > 0.01 else float('inf')
            attack = decode_mbc_time_constant(bg[3])
            release = decode_mbc_time_constant(bg[4])
            makeup = bg[5] / 16.0
            print(f"  band {i}: xover={xover_hz} Hz, thresh={thresh:+.1f} dB, "
                  f"ratio={ratio:.2f}:1, attack={attack:.1f} ms, "
                  f"release={release:.1f} ms, makeup={makeup:+.1f} dB")
    print()

    # Build preset name mapping using the configured prefix.
    # Include endpoint mode and profile in the name when explicitly specified.
    name_parts = [args.prefix]
    if args.mode != "normal":
        name_parts.append(args.mode.title())
    if args.profile:
        name_parts.append(args.profile.title())
    name_base = "-".join(name_parts)

    ieq_presets = {
        f"{name_base}-Balanced": "ieq_balanced",
        f"{name_base}-Detailed": "ieq_detailed",
        f"{name_base}-Warm": "ieq_warm",
    }

    for preset_name, curve_key in ieq_presets.items():
        if curve_key not in curves:
            print(f"  Skipping {preset_name}: curve '{curve_key}' not found in XML")
            continue

        gains_raw = curves[curve_key]
        ieq_db = np.array(gains_raw) / 16.0 * scale

        # Combined target: IEQ + audio-optimizer (summed in dB)
        combined_left = ieq_db + ao_db_left
        combined_right = ieq_db + ao_db_right

        # Generate FIR impulse responses
        fir_left = make_fir(float_freqs, combined_left, normalize=True)
        fir_right = make_fir(float_freqs, combined_right, normalize=True)

        # Save stereo impulse response
        irs_path = args.irs_dir / f"{preset_name}.irs"
        save_wav_stereo(irs_path, fir_left, fir_right)

        # Create preset (kernel-name is the WAV filename stem)
        preset = make_preset(preset_name, peq_filters, vol_leveler, mb_comp, freqs)
        out_path = args.output_dir / f"{preset_name}.json"
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
