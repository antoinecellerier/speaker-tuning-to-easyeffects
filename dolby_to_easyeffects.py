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
  - stereo_tools#0: surround virtualizer (stereo widening from surround-boost)
  - equalizer#0: speaker PEQ bells + high-pass (parametric filters from Dolby)
  - equalizer#1: dialog enhancer (speech presence boost from dialog-enhancer settings)
  - autogain#0: volume leveler (from volume-leveler settings)
  - multiband_compressor#0: dynamics processing (from mb-compressor-tuning)
  - multiband_compressor#1: per-band limiter (from regulator-tuning)
  - limiter#0: brickwall output limiter (safety net)
"""

import argparse
import json
import math
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from scipy.io import wavfile

DEFAULT_OUTPUT_DIR = Path.home() / ".local" / "share" / "easyeffects" / "output"
DEFAULT_IRS_DIR = Path.home() / ".local" / "share" / "easyeffects" / "irs"
DEFAULT_AUTOLOAD_DIR = Path.home() / ".local" / "share" / "easyeffects" / "autoload" / "output"

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


def get_profile_types(path: Path, endpoint_type: str, operating_mode: str) -> list[str]:
    """Return all profile type names for the given endpoint/mode, excluding 'off'."""
    tree = ET.parse(path)
    root = tree.getroot()
    ep = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if ep is None:
        return []
    return [p.get("type") for p in ep.findall("profile") if p.get("type") != "off"]


def find_speaker_sinks() -> list[dict]:
    """Find internal speaker output sinks from PipeWire.

    Returns a list of dicts with 'name', 'description', and 'profile' keys,
    corresponding to the PipeWire node.name, node.description, and
    device.profile.description properties used by EasyEffects autoload.

    Only returns sinks with the 'audio-speakers' device icon, excluding
    HDMI/DisplayPort outputs.
    """
    try:
        result = subprocess.run(
            ["pw-dump"], capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return []

    sinks = []
    for obj in data:
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Audio/Sink":
            continue
        # Only include sinks with the speaker icon (excludes HDMI, headphones, etc.)
        if props.get("device.icon_name") != "audio-speakers":
            continue
        sinks.append({
            "name": props.get("node.name", ""),
            "description": props.get("node.description", ""),
            "profile": props.get("device.profile.description", ""),
        })
    return sinks


def write_autoload(autoload_dir: Path, device_name: str, device_description: str,
                   device_profile: str, preset_name: str) -> Path:
    """Write an EasyEffects autoload config file for a device/route → preset mapping.

    EasyEffects loads this file when the given PipeWire sink becomes the active
    output, automatically switching to the named preset.

    File is named '{device_name}:{device_profile}.json' (with '/' replaced by '_'),
    matching EasyEffects' AutoloadManager::getFilePath() convention.
    """
    autoload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = device_name.replace("/", "_")
    safe_profile = device_profile.replace("/", "_")
    path = autoload_dir / f"{safe_name}:{safe_profile}.json"
    path.write_text(json.dumps({
        "device": device_name,
        "device-description": device_description,
        "device-profile": device_profile,
        "preset-name": preset_name,
    }, indent=4) + "\n")
    return path


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
    peq_enable = vlldp.find("speaker-peq-enable")
    if peq_enable is None or peq_enable.get("value") != "0":
        for f in vlldp.findall(".//speaker-peq-filters/filter"):
            if f.get("enabled") == "0":
                continue
            ftype = int(f.get("type"))
            if ftype not in (1, 4, 7, 9):
                print(f"  Warning: unknown PEQ filter type {ftype}, skipping")
                continue
            peq_filters.append({
                "speaker": int(f.get("speaker")),
                "type": ftype,
                "f0": float(f.get("f0")),
                "gain": float(f.get("gain", "0")),
                "q": float(f.get("q", "0.707")),
                "s": float(f.get("s", "1.0")),
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

    # Dialog enhancer settings (from tuning-cp)
    dialog_enhancer = None
    if cp is not None:
        de_enable = cp.find("dialog-enhancer-enable")
        if de_enable is not None and de_enable.get("value") == "1":
            de_amount = cp.find("dialog-enhancer-amount")
            dialog_enhancer = {
                "amount": int(de_amount.get("value")) if de_amount is not None else 5,
            }

    # Surround virtualizer settings (from tuning-cp)
    surround = None
    if cp is not None:
        sr_enable = cp.find("surround-decoder-enable")
        if sr_enable is not None and sr_enable.get("value") == "1":
            sr_boost = cp.find("surround-boost")
            surround = {
                "boost": int(sr_boost.get("value")) / 16.0 if sr_boost is not None else 0.0,
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

    # Regulator settings (per-band limiter from tuning-vlldp)
    regulator = None
    reg_dist = vlldp.find("regulator-speaker-dist-enable")
    if reg_dist is not None and reg_dist.get("value") == "1":
        reg_tuning = vlldp.find("regulator-tuning")
        if reg_tuning is not None:
            th_el = reg_tuning.find("threshold_high")
            tl_el = reg_tuning.find("threshold_low")
            # Handle both inline values and presets (array_20_zero = all zeros)
            if th_el is not None and th_el.get("value"):
                th = [x / 16.0 for x in parse_csv_ints(th_el.get("value"))]
            else:
                th = [0.0] * 20
            if tl_el is not None and tl_el.get("value"):
                tl = [x / 16.0 for x in parse_csv_ints(tl_el.get("value"))]
            else:
                tl = [-12.0] * 20
            reg_stress = vlldp.find("regulator-stress-amount")
            stress = parse_csv_ints(reg_stress.get("value")) if reg_stress is not None else [0] * 8
            reg_slope = vlldp.find("regulator-distortion-slope")
            slope = int(reg_slope.get("value")) / 16.0 if reg_slope is not None else 1.0
            reg_timbre = vlldp.find("regulator-timbre-preservation")
            timbre = int(reg_timbre.get("value")) / 16.0 if reg_timbre is not None else 0.75
            regulator = {
                "threshold_high": th,
                "threshold_low": tl,
                "stress": [x / 16.0 for x in stress],
                "distortion_slope": slope,
                "timbre_preservation": timbre,
            }

    return freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, dialog_enhancer, surround, mb_comp, regulator


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
        "autogain": False,
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


def make_shelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """Low-shelf filter band from Dolby PEQ type 4.

    The S (shelf slope) parameter controls the steepness of the transition.
    S=1.0 gives a Butterworth (maximally flat) response with Q≈0.707.
    Convert S to Q using the standard audio shelf formula.
    """
    # S-to-Q conversion for shelving filters:
    # Q = 1/sqrt((A + 1/A) * (1/S - 1) + 2) where A = 10^(gain/40)
    # For S=1.0, this simplifies to Q ≈ 0.707 (Butterworth)
    a = 10 ** (abs(gain) / 40.0) if gain != 0 else 1.0
    denom = (a + 1.0 / a) * (1.0 / s - 1.0) + 2.0
    q = 1.0 / math.sqrt(max(denom, 0.01))
    return {
        "frequency": freq,
        "gain": round(gain, 4),
        "mode": "RLC (BT)",
        "mute": False,
        "q": round(q, 4),
        "slope": "x1",
        "solo": False,
        "type": "Lo-shelf",
        "width": 4.0,
    }


def make_peq_eq(peq_filters):
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Handles filter types: 1 (bell), 4 (low-shelf), 7 and 9 (high-pass).
    The HP protects laptop speakers from sub-bass energy they can't reproduce.
    """
    peq_left_bells = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    peq_right_bells = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    hp_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (7, 9)]
    hp_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (7, 9)]
    shelf_left = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 4]
    shelf_right = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 4]

    num_bells = max(len(peq_left_bells), len(peq_right_bells))
    num_hp = max(len(hp_left), len(hp_right))
    num_shelf = max(len(shelf_left), len(shelf_right))
    num_bands = num_hp + num_shelf + num_bells

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    # HP filters first
    for j, pf in enumerate(hp_left):
        left_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])
    for j, pf in enumerate(hp_right):
        right_bands[f"band{j}"] = make_hp_band(pf["f0"], pf["order"])

    # Shelf filters next
    off = num_hp
    for j, pf in enumerate(shelf_left):
        left_bands[f"band{off + j}"] = make_shelf_band(pf["f0"], pf["gain"], pf["s"])
    for j, pf in enumerate(shelf_right):
        right_bands[f"band{off + j}"] = make_shelf_band(pf["f0"], pf["gain"], pf["s"])

    # Bell filters after
    off = num_hp + num_shelf
    for j, pf in enumerate(peq_left_bells):
        left_bands[f"band{off + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])
    for j, pf in enumerate(peq_right_bells):
        right_bands[f"band{off + j}"] = make_band(pf["f0"], pf["gain"], q=pf["q"])

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

    # Compensate for PEQ boost to prevent clipping. Scale the compensation
    # by bandwidth: a narrow Q=4.6 bell at +4 dB barely raises broadband level,
    # while a wide Q=0.7 bell at +4 dB raises it nearly 4 dB.
    # Effective boost ≈ gain * min(1, 2/Q) for bells, full gain for shelves.
    all_peq = peq_left_bells + peq_right_bells + shelf_left + shelf_right
    effective_boosts = []
    for pf in all_peq:
        if pf["gain"] <= 0:
            continue
        q = pf.get("q", 1.0)
        effective_boosts.append(pf["gain"] * min(1.0, 2.0 / q))
    peak_boost = max(effective_boosts, default=0.0)
    output_gain = -peak_boost

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


def make_stereo_tools(surround):
    """Stereo widening mapped from Dolby surround virtualizer.

    Dolby's surround decoder/virtualizer creates a wider stereo image
    from stereo content. We approximate this using the Calf Stereo
    Tools plugin's stereo-base parameter, which controls the Mid/Side
    balance to widen or narrow the stereo field.

    surround-boost (0-16 in 1/16 dB scale) maps to stereo-base:
      0 dB → 0.0 (no widening)
      6 dB → 0.3 (moderate widening)
    Kept conservative to avoid phase artifacts on laptop speakers.
    """
    if not surround or surround["boost"] <= 0:
        return None

    # Map surround-boost (dB) to stereo-base (0-1 range).
    # 6 dB boost → 0.3 stereo-base (moderate widening).
    # Cap at 0.5 to prevent excessive widening artifacts.
    base = min(surround["boost"] / 20.0, 0.5)

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "balance-in": 0.0,
        "balance-out": 0.0,
        "softclip": False,
        "mutel": False,
        "muter": False,
        "phasel": False,
        "phaser": False,
        "mode": "LR > LR (Stereo Default)",
        "side-level": 0.0,
        "side-balance": 0.0,
        "middle-level": 0.0,
        "middle-panorama": 0.0,
        "stereo-base": round(base, 2),
        "delay": 0.0,
        "sc-level": 1.0,
        "stereo-phase": 0.0,
    }


def make_dialog_enhancer(dialog_enhancer):
    """Dialog enhancer mapped as a broad speech-band EQ boost.

    Dolby's dialog enhancer (DE) isolates speech frequencies and
    selectively boosts them. We approximate this with a broad Bell
    filter centered at 2.5 kHz (speech presence region), with gain
    scaled by the DE amount (0-16 scale).

    The DE amount maps to gain: amount/16 * 6 dB, giving a maximum
    of +6 dB at amount=16. Typical values: dynamic=5 (+1.9 dB),
    voice=3 (+1.1 dB), music=7 (+2.6 dB when enabled).
    """
    if not dialog_enhancer:
        return None

    amount = dialog_enhancer["amount"]
    gain = amount / 16.0 * 6.0
    if gain <= 0:
        return None

    band = make_band(2500.0, round(gain, 2), q=0.7)

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "mode": "IIR",
        "num-bands": 1,
        "split-channels": False,
        "left": {"band0": band},
        "right": {"band0": band},
    }


def make_autogain(vol_leveler):
    """Autogain plugin mapping from Dolby volume leveler.

    The Dolby volume leveler brings quiet passages up to a target loudness.
    EasyEffects' autogain does the same using EBU R 128 loudness measurement.

    Dolby volume-leveler-amount (0-10) maps to aggressiveness:
      0 = gentle (long history window)
      10 = aggressive (short history window)
    """
    if not vol_leveler or not vol_leveler["enable"]:
        return None

    # Map Dolby amount (0-10) to maximum-history (seconds).
    # Higher amount = shorter window = more aggressive leveling.
    # amount 0 → 30s (gentle), amount 4+ → 10s (aggressive, clamped)
    # Using a gentler slope than Dolby because EasyEffects lacks the MI
    # (Media Intelligence) steering that prevents pumping in the real pipeline.
    amount = vol_leveler["amount"]
    max_history = max(30 - amount * 5, 10)

    # Dolby target is in dBFS; subtract 3 dB for the LUFS target to provide
    # headroom (EBU R 128 standard is -23 LUFS vs Dolby's -20 dBFS).
    target = vol_leveler["out_target"] - 3

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
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
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
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
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
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }

    return result


def make_regulator(regulator, freqs):
    """Per-band limiter mapped from Dolby regulator-tuning.

    The Dolby regulator is a 20-band limiter that prevents speaker
    distortion. We approximate it using EasyEffects' multiband compressor
    configured as a limiter.

    The 20 Dolby bands are grouped into zones with similar thresholds
    to fit within EasyEffects' 8-band limit.

    Regulator parameters mapped:
      - distortion_slope: controls limiter ratio. 1.0 = hard limiter
        (infinity:1), lower values = softer limiting. Mapped as
        ratio = 1 / (1 - slope) when slope < 1, else 100:1.
      - timbre_preservation: 0-1, controls knee softness. Higher values
        mean softer knee to preserve spectral shape. Mapped to
        knee = -6 * timbre dB (0 = hard knee, 1 = -6 dB soft knee).
    """
    if not regulator:
        return None

    th = regulator["threshold_high"]
    slope = regulator.get("distortion_slope", 1.0)
    timbre = regulator.get("timbre_preservation", 0.75)

    # Derive ratio from distortion slope:
    # slope=1.0 → hard limiter (use 100:1 as practical maximum)
    # slope=0.5 → ratio=2:1 (moderate compression)
    if slope >= 1.0:
        ratio = 100.0
    elif slope <= 0.0:
        ratio = 1.0  # bypass
    else:
        ratio = 1.0 / (1.0 - slope)

    # Derive knee from timbre preservation:
    # timbre=0 → hard knee (0 dB), timbre=1 → soft knee (-6 dB)
    knee = -6.0 * timbre

    # Group the 20 bands into zones with distinct thresholds.
    # Find runs of identical threshold_high values.
    zones = []  # list of (start_idx, end_idx, threshold)
    i = 0
    while i < len(th):
        j = i + 1
        while j < len(th) and th[j] == th[i]:
            j += 1
        zones.append((i, j - 1, th[i]))
        i = j

    # Merge zones if we have more than 8 (EasyEffects limit)
    # In practice, Dolby regulators typically produce 2-5 zones
    while len(zones) > 8:
        # Merge the two adjacent zones with the smallest threshold difference
        min_diff = float("inf")
        min_idx = 0
        for k in range(len(zones) - 1):
            diff = abs(zones[k][2] - zones[k + 1][2])
            if diff < min_diff:
                min_diff = diff
                min_idx = k
        z1 = zones[min_idx]
        z2 = zones[min_idx + 1]
        merged_thresh = max(z1[2], z2[2])  # use the less aggressive threshold
        zones[min_idx] = (z1[0], z2[1], merged_thresh)
        del zones[min_idx + 1]

    # Build the multiband compressor (used as limiter: ratio=100:1, fast attack)
    result = {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i < len(zones):
            zone_start, zone_end, threshold = zones[i]
            # Crossover at the geometric mean between the last freq of this
            # zone and the first freq of the next zone
            if i > 0:
                prev_end = zones[i - 1][1]
                cross_freq = math.sqrt(freqs[prev_end] * freqs[zone_start])
            else:
                cross_freq = 10.0  # not used for band 0

            # Bands with threshold >= 0 dB never trigger; disable to save CPU
            is_active = threshold < 0
            band = {
                "compressor-enable": is_active,
                "mute": False,
                "solo": False,
                "attack-threshold": round(threshold, 1),
                "attack-time": 1.0,  # very fast for limiting
                "release-threshold": -80.01,
                "release-time": 50.0,
                "ratio": round(ratio, 1),
                "knee": round(knee, 1),
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "Peak",  # peak detection for limiting
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 1.0,  # 1 ms head start for transients
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }
            if i > 0:
                band["enable-band"] = True
                band["split-frequency"] = round(cross_freq, 1)
            result[bandn] = band
        else:
            # Disabled band
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
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }

    return result


def make_limiter():
    """Brickwall output limiter to catch any remaining overshoot.

    Placed at the very end of the chain as a safety net. Uses the LSP
    limiter plugin with a -1 dB threshold and 1 ms lookahead for
    transparent true-peak limiting.
    """
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "mode": "Herm Thin",
        "oversampling": "None",
        "dithering": "None",
        "sidechain-type": "Internal",
        "lookahead": 1.0,
        "attack": 1.0,
        "release": 5.0,
        "threshold": -1.0,
        "gain-boost": False,
        "stereo-link": 100.0,
        "alr": False,
        "sidechain-preamp": 0.0,
    }


def make_preset(kernel_name, peq_filters, vol_leveler=None,
                dialog_enhancer=None, surround=None, mb_comp=None,
                regulator=None, freqs=None):
    preset = {
        "output": {
            "blocklist": [],
            "convolver#0": make_convolver(kernel_name),
            "plugins_order": ["convolver#0"],
        }
    }

    # Stereo widening early in chain (before EQ changes the spectrum)
    st = make_stereo_tools(surround)
    if st:
        preset["output"]["stereo_tools#0"] = st
        preset["output"]["plugins_order"].append("stereo_tools#0")

    peq = make_peq_eq(peq_filters)
    if peq:
        preset["output"]["equalizer#0"] = peq
        preset["output"]["plugins_order"].append("equalizer#0")

    # Dialog enhancer (speech presence boost) before the volume leveler,
    # matching Dolby's CP order: DE → IEQ → Volume Leveler.
    de = make_dialog_enhancer(dialog_enhancer)
    if de:
        preset["output"]["equalizer#1"] = de
        preset["output"]["plugins_order"].append("equalizer#1")

    # Autogain (volume leveler) goes before the compressor/regulator to match
    # Dolby's signal flow: CP (volume leveler) → VLLDP (compressor → regulator).
    # This lets the compressor and regulator catch any overshoot from the leveler.
    autogain = make_autogain(vol_leveler)
    if autogain:
        preset["output"]["autogain#0"] = autogain
        preset["output"]["plugins_order"].append("autogain#0")

    mbc = make_multiband_compressor(mb_comp, freqs)
    if mbc:
        preset["output"]["multiband_compressor#0"] = mbc
        preset["output"]["plugins_order"].append("multiband_compressor#0")

    reg = make_regulator(regulator, freqs)
    if reg:
        preset["output"]["multiband_compressor#1"] = reg
        preset["output"]["plugins_order"].append("multiband_compressor#1")

    # Brickwall limiter at the end as a safety net
    preset["output"]["limiter#0"] = make_limiter()
    preset["output"]["plugins_order"].append("limiter#0")

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
    parser.add_argument(
        "--all-profiles",
        action="store_true",
        help="generate presets for all profiles in the selected endpoint/mode "
             "(profile names are included in the preset names)",
    )
    parser.add_argument(
        "--autoload",
        nargs="?",
        const=True,
        metavar="PRESET",
        help="write EasyEffects autoload config for speaker outputs. "
             "Optionally specify the preset name to autoload; "
             "defaults to the first Balanced preset generated",
    )
    parser.add_argument(
        "--autoload-dir",
        type=Path,
        default=DEFAULT_AUTOLOAD_DIR,
        help=f"EasyEffects autoload directory (default: {DEFAULT_AUTOLOAD_DIR})",
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

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.irs_dir.mkdir(parents=True, exist_ok=True)

    # Determine which profiles to process
    if args.all_profiles:
        profile_types = get_profile_types(xml_path, args.endpoint, args.mode)
        if not profile_types:
            print(f"No profiles found for endpoint={args.endpoint} mode={args.mode}")
            return
        print(f"Generating presets for all {len(profile_types)} profiles: {', '.join(profile_types)}")
    else:
        profile_types = [args.profile]  # None means "first profile"

    all_preset_names = []

    for profile_type in profile_types:
        # Build name base: prefix[-Mode][-Profile]
        # When --all-profiles is used, always include the profile name.
        name_parts = [args.prefix]
        if args.mode != "normal":
            name_parts.append(args.mode.title())
        if profile_type or args.all_profiles:
            name_parts.append((profile_type or "default").title())
        name_base = "-".join(name_parts)

        print(f"\n{'='*60}")
        print(f"Endpoint: {args.endpoint} (mode={args.mode})")
        print(f"Profile: {profile_type or '(first)'}")

        freqs, curves, ieq_amount, ao_left, ao_right, peq_filters, vol_leveler, dialog_enhancer, surround, mb_comp, regulator = parse_xml(
            xml_path,
            endpoint_type=args.endpoint,
            operating_mode=args.mode,
            profile_type=profile_type,
        )

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
            if pf["type"] in (7, 9):
                print(f"  [{spk}] HP @ {pf['f0']} Hz, order {pf['order']} ({pf['order'] * 6} dB/oct)")
            elif pf["type"] == 4:
                print(f"  [{spk}] Lo-shelf @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, S={pf['s']}")
            elif pf["type"] == 1:
                print(f"  [{spk}] Bell @ {pf['f0']} Hz, {pf['gain']:+.1f} dB, Q={pf['q']}")

        if dialog_enhancer:
            gain = dialog_enhancer["amount"] / 16.0 * 6.0
            print(f"\nDialog enhancer: amount={dialog_enhancer['amount']}, "
                  f"mapped to +{gain:.1f} dB @ 2.5 kHz")

        if surround:
            base = min(surround["boost"] / 20.0, 0.5)
            print(f"\nSurround virtualizer: boost={surround['boost']:.1f} dB, "
                  f"mapped to stereo-base={base:.2f}")

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

        if regulator:
            print(f"\nRegulator (per-band limiter):")
            print(f"  threshold_high (dB): {[f'{x:+.1f}' for x in regulator['threshold_high']]}")
            print(f"  threshold_low (dB):  {[f'{x:+.1f}' for x in regulator['threshold_low']]}")
            print(f"  stress (dB):         {[f'{x:+.1f}' for x in regulator['stress']]}")
            print(f"  distortion-slope:    {regulator.get('distortion_slope', 1.0):.2f}")
            print(f"  timbre-preservation: {regulator.get('timbre_preservation', 0.75):.2f}")
        print()

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
            preset = make_preset(preset_name, peq_filters, vol_leveler, dialog_enhancer, surround, mb_comp, regulator, freqs)
            out_path = args.output_dir / f"{preset_name}.json"
            out_path.write_text(json.dumps(preset, indent=4) + "\n")

            all_preset_names.append(preset_name)

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
            print(f"\n  FIR verification (left, normalized to peak=0):")
            for i, f in enumerate(freqs):
                idx = np.argmin(np.abs(fft_freqs - f))
                print(f"  {f:>7} Hz  target: {combined_left[i] - np.max(combined_left):+6.1f}  "
                      f"actual: {mag_db[idx]:+6.1f}  "
                      f"error: {mag_db[idx] - (combined_left[i] - np.max(combined_left)):+5.2f}")
            print()

    # Autoload configuration
    if args.autoload and all_preset_names:
        autoload_preset = args.autoload if isinstance(args.autoload, str) else all_preset_names[0]
        sinks = find_speaker_sinks()
        if not sinks:
            print("Warning: no speaker sinks found via pw-dump; cannot configure autoload.")
            print("  Is PipeWire running? Try running the script while logged into your desktop session.")
        else:
            print(f"\nConfiguring autoload → '{autoload_preset}':")
            for sink in sinks:
                path = write_autoload(
                    args.autoload_dir,
                    sink["name"],
                    sink["description"],
                    sink["profile"],
                    autoload_preset,
                )
                print(f"  Wrote {path}")
                print(f"  Device: {sink['description']} ({sink['profile']})")


if __name__ == "__main__":
    main()
