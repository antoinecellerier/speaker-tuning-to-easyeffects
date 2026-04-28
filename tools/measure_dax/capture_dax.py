#!/usr/bin/env python3
"""Windows-side runner: play stimulus.wav and record WASAPI loopback of the
post-DAX3 endpoint signal.

Usage on Windows (after `pip install sounddevice numpy scipy soundfile pycaw
comtypes`):

    python capture_dax.py --label off
    python capture_dax.py --label dynamic
    python capture_dax.py --label movie
    ...

Toggle the active Dolby Access profile manually between runs (no public
API for that).

Pre-capture validation:
  1. Resolve the speaker render endpoint.
  2. Verify shared-mode format is 48 kHz / float32 stereo.
  3. Verify the endpoint's FX chain contains a Dolby APO CLSID.
  4. Best-effort: compare the user's --label against Dolby Access app state.

Capture:
  Plays stimulus.wav, simultaneously records WASAPI loopback of the same
  endpoint, saves loopback_<label>.wav (32-bit float, stereo).

Post-capture validation:
  Level checks (peak, RMS, clip count) and a similarity check against
  loopback_off.wav (if it exists) to catch the "forgot to switch profile"
  mistake.

Self-contained: only depends on numpy + scipy + sounddevice + pycaw +
comtypes + soundfile, all pip-installable.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import sys
import threading
import time
import warnings
from pathlib import Path

import numpy as np

# pycaw enumerates every property on every endpoint and prints a UserWarning
# for each COMError it gets back. They're harmless and overwhelm the log.
warnings.filterwarnings(
    "ignore", message="COMError attempting to get property", module="pycaw.utils"
)


METADATA_SCHEMA_VERSION = 1

# Per-endpoint property-store format IDs of interest. Discovered empirically
# from HKLM\...\MMDevices\Audio\Render\<id>\Properties on Win11 24H2.
PKEY_FMTID_SPATIAL_MODE_NAMES = "{a45429a4-aa63-4480-b7f8-3f2552daee93}"
PKEY_ACTIVE_SPATIAL_MODE_CLSID = "{9637b4b9-11ee-4c35-b43c-7b2452c993cc},1"


VALID_LABELS = ("off", "dynamic", "movie", "music", "game", "voice")
EXPECTED_SR = 48000
EXPECTED_CHANNELS = 2
LOOPBACK_PADDING_SEC = 0.5  # extra recording past stimulus end

# Dolby APO CLSIDs observed in the wild. We log unrecognized FX CLSIDs so
# the user can extend this list if they're on a newer Dolby driver.
KNOWN_DOLBY_CLSIDS = {
    "{6C4E7DA4-30D5-44B7-A6CD-C0F08F5DEC0E}": "Dolby DAX2/DAX3 PostMix APO",
    "{F4250F44-5F92-4F03-A19F-0F2BB2B08C04}": "Dolby DAX3 EFX",
    "{D3CFD9B8-F61E-4D34-9F4D-FE9E6F89B69D}": "Dolby Audio Driver APO",
    "{ABA6F8F2-7A39-4DBE-9F08-DAB8D5DD3FE6}": "Dolby DAX3 (alt)",
}

FX_PROPERTY_KEYS = {
    "PreMixEffectClsid":      "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},2",
    "PostMixEffectClsid":     "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},3",
    "StreamEffectClsid":      "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},4",
    "ModeEffectClsid":        "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},5",
    "EndpointEffectClsid":    "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},6",
}


def _abort(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _info(msg: str) -> None:
    print(f"  {msg}")


def _warn(msg: str) -> None:
    print(f"WARN:  {msg}", file=sys.stderr)


def resolve_endpoint(device_substring: str | None):
    """Return (sd_device_index, sd_device_info, mmdevice). mmdevice is the
    pycaw IMMDevice for the same endpoint, used for FX property reads."""
    import sounddevice as sd

    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    wasapi_idx = next(
        (i for i, h in enumerate(hostapis) if "WASAPI" in h["name"]), None
    )
    if wasapi_idx is None:
        _abort("Windows WASAPI host API not available — is this Windows?")

    candidates = [
        (i, d) for i, d in enumerate(devices)
        if d["hostapi"] == wasapi_idx and d["max_output_channels"] >= 2
        and "loopback" not in d["name"].lower()
    ]
    if device_substring:
        candidates = [
            (i, d) for (i, d) in candidates
            if device_substring.lower() in d["name"].lower()
        ]
    if not candidates:
        _abort(f"No matching WASAPI output device (substring={device_substring!r}).")

    if device_substring is None:
        default_out_idx = sd.default.device[1]
        default_match = next(
            ((i, d) for (i, d) in candidates if i == default_out_idx), None
        )
        if default_match is not None:
            sd_idx, sd_info = default_match
        else:
            sd_idx, sd_info = candidates[0]
    else:
        sd_idx, sd_info = candidates[0]

    _info(f"Resolved endpoint: {sd_info['name']!r} (sounddevice index {sd_idx})")
    _info(f"  default samplerate: {sd_info['default_samplerate']:.0f} Hz")
    _info(f"  max output channels: {sd_info['max_output_channels']}")

    mmdev = _open_mmdevice_by_name(sd_info["name"])
    return sd_idx, sd_info, mmdev


def _open_mmdevice_by_name(name: str):
    """Find the pycaw AudioDevice that matches the given friendly name.

    Filters on AudioDeviceState.Active first — many machines have several
    duplicate entries (e.g. four 'Speakers (Realtek(R) Audio)' rows where
    only one is live). Returning a NotPresent duplicate makes downstream
    EndpointVolume / FX reads fail opaquely."""
    try:
        from pycaw.pycaw import AudioUtilities
    except Exception as e:
        _warn(f"pycaw not available, FX-chain validation will be skipped: {e}")
        return None

    def _is_active(dev) -> bool:
        try:
            return str(dev.state).endswith(".Active")
        except Exception:
            return False

    primary = name.split(" (")[0]
    try:
        all_devs = list(AudioUtilities.GetAllDevices())
    except Exception as e:
        _warn(f"Could not enumerate IMMDevices: {e}")
        return None

    # Prefer active devices with a strong (prefix-token) name match, then
    # any active device with a weaker (substring) name match, then fall
    # back to inactive matches as a last resort.
    matchers = [
        (lambda d: _is_active(d) and primary in (d.FriendlyName or "")),
        (lambda d: _is_active(d) and any(
            tok.lower() in (d.FriendlyName or "").lower()
            for tok in name.replace("(", " ").replace(")", " ").split()
            if len(tok) >= 4
        )),
        (lambda d: primary in (d.FriendlyName or "")),
    ]
    for pred in matchers:
        for dev in all_devs:
            if dev.FriendlyName and pred(dev):
                return dev
    return None


def verify_endpoint_format(sd_info, sr_target: int = EXPECTED_SR) -> None:
    actual_sr = int(round(sd_info["default_samplerate"]))
    if actual_sr != sr_target:
        _abort(
            f"Endpoint default samplerate is {actual_sr} Hz, need {sr_target} Hz.\n"
            f"  Fix: Settings → System → Sound → All sound devices → "
            f"[your speakers] → Output settings → Format → any "
            f"{sr_target} Hz option (16- or 24-bit both fine; loopback taps "
            f"the float32 mix bus regardless of endpoint bit depth)."
        )
    _info(f"Endpoint format OK ({actual_sr} Hz)")


def _endpoint_registry_id(mmdev) -> str | None:
    """Extract the bare endpoint GUID (registry-key form) from a pycaw device.

    pycaw exposes the full id like '{0.0.0.00000000}.{621c4c4c-...}' but the
    MMDevices registry key uses just the trailing GUID."""
    try:
        full_id = mmdev.id
    except Exception:
        return None
    if not full_id or "." not in full_id:
        return None
    return full_id.rsplit(".", 1)[-1]


def _decode_propstore_string(blob: bytes) -> str:
    """Decode the Windows property-store binary string format used in
    HKLM\\...\\MMDevices\\Audio\\Render\\<id>\\Properties REG_BINARY values.

    Layout: 8-byte header, then UTF-16-LE bytes terminated by NUL."""
    if not blob or len(blob) < 10:
        return ""
    body = blob[8:]
    s = body.decode("utf-16-le", errors="replace")
    nul = s.find("\x00")
    return s if nul < 0 else s[:nul]


def detect_dolby(mmdev, label: str, force_no_check: bool) -> dict:
    """Look for Dolby on this endpoint via three independent signals: the
    per-endpoint MMDevices property store (Win11), the system-wide APO
    registry (older Win10), and the Dolby Access app package.

    Returns a dict suitable for the metadata sidecar. Aborts on no detection
    when label != 'off' unless force_no_check is True."""
    result = {
        "detected": False,
        "spatial_modes_available": [],
        "active_spatial_mode_clsid": None,
        "system_apos": [],
        "dolby_access_package": None,
    }

    # 1. Per-endpoint property store: spatial mode display names + active CLSID.
    if mmdev is not None:
        ep_id = _endpoint_registry_id(mmdev)
        if ep_id:
            try:
                import winreg
                path = (
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices"
                    rf"\Audio\Render\{ep_id}\Properties"
                )
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as h:
                    i = 0
                    while True:
                        try:
                            name, val, _vtype = winreg.EnumValue(h, i)
                        except OSError:
                            break
                        i += 1
                        lname = name.lower()
                        if (lname.startswith(PKEY_FMTID_SPATIAL_MODE_NAMES)
                                and isinstance(val, (bytes, bytearray))):
                            decoded = _decode_propstore_string(bytes(val))
                            if decoded:
                                result["spatial_modes_available"].append(decoded)
                        elif lname == PKEY_ACTIVE_SPATIAL_MODE_CLSID:
                            result["active_spatial_mode_clsid"] = str(val)
            except OSError as e:
                _warn(f"Could not open per-endpoint property store: {e}")

    if any("dolby" in m.lower() for m in result["spatial_modes_available"]):
        result["detected"] = True

    # 2. System-wide APO registry — present on older Win10 builds, absent on
    # modern Win11 with spatial APOs. Missing key is fine, not an error.
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\AudioEngine\AudioProcessingObjects",
        ) as apos:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(apos, i)
                except OSError:
                    break
                with winreg.OpenKey(apos, sub) as h:
                    try:
                        friendly, _ = winreg.QueryValueEx(h, "FriendlyName")
                    except OSError:
                        friendly = ""
                if "dolby" in friendly.lower() or "atmos" in friendly.lower():
                    result["system_apos"].append(
                        {"clsid": sub, "friendly_name": friendly}
                    )
                i += 1
        if result["system_apos"]:
            result["detected"] = True
    except OSError:
        pass

    # 3. Dolby Access UWP package directory.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = Path(local) / "Packages"
        if base.is_dir():
            pkgs = list(base.glob("DolbyLaboratories.DolbyAccess_*"))
            if pkgs:
                result["dolby_access_package"] = str(pkgs[0])
                result["detected"] = True

    if result["spatial_modes_available"]:
        _info("endpoint spatial modes: " + ", ".join(result["spatial_modes_available"]))
    if result["active_spatial_mode_clsid"]:
        _info(f"active spatial mode CLSID: {result['active_spatial_mode_clsid']}")
    if result["system_apos"]:
        _info(f"system-registered Dolby APOs: {len(result['system_apos'])}")
        for a in result["system_apos"][:5]:
            _info(f"  {a['clsid']} {a['friendly_name']}")
    if result["dolby_access_package"]:
        _info(f"Dolby Access package: {result['dolby_access_package']}")

    if not result["detected"]:
        if force_no_check:
            _warn("No Dolby installation detected — bypassed by --no-apo-check.")
        elif label == "off":
            _warn("No Dolby installation detected — proceeding because --label off.")
        else:
            _abort(
                "No Dolby installation detected.\n"
                "  Tried: per-endpoint spatial modes, system APO registry, "
                "Dolby Access package.\n"
                "  If Dolby Access is running and Atmos is the active spatial "
                "mode, pass --no-apo-check to bypass this guard."
            )
    return result


def best_effort_dolby_state(label: str) -> dict | None:
    local = os.environ.get("LOCALAPPDATA", "")
    if not local:
        return None
    base = Path(local) / "Packages"
    if not base.is_dir():
        return None
    candidates = list(base.glob("DolbyLaboratories.DolbyAccess_*"))
    if not candidates:
        return None
    for c in candidates:
        ls = c / "LocalState"
        if not ls.is_dir():
            continue
        state_files = list(ls.glob("*.json")) + list(ls.glob("*.dat"))
        for sf in state_files:
            try:
                txt = sf.read_text(errors="ignore")
            except Exception:
                continue
            for prof in VALID_LABELS:
                if prof in txt.lower() and prof != "off":
                    _info(f"(advisory) Dolby Access state file mentions profile: {prof}")
                    if label not in (prof, "off") and label != "off":
                        _warn(
                            f"Local state mentions {prof!r} but --label is {label!r}. "
                            "Make sure Dolby Access is set to the right profile."
                        )
                    return {
                        "package_dir": str(c),
                        "state_file": str(sf),
                        "profile_mention": prof,
                    }
    return None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def read_endpoint_volume(mmdev) -> dict | None:
    """Master volume + mute on the resolved endpoint, falling back to the
    default render endpoint if the resolved one's EndpointVolume isn't
    activatable. The pycaw AudioDevice wrappers from GetAllDevices() can
    fail to lazily activate IAudioEndpointVolume on some Realtek installs
    (HRESULT 0x80070002) even when the registry/spatial reads on the same
    object work fine — GetSpeakers() returns a fresh wrapper that does."""
    candidates = []
    if mmdev is not None:
        candidates.append(("resolved", mmdev))
    try:
        from pycaw.pycaw import AudioUtilities
        candidates.append(("default_speakers", AudioUtilities.GetSpeakers()))
    except Exception as e:
        if not candidates:
            _warn(f"Could not access pycaw AudioUtilities: {e}")
            return None

    last_err = None
    for source, dev in candidates:
        try:
            ev = dev.EndpointVolume
            return {
                "scalar": float(ev.GetMasterVolumeLevelScalar()),
                "level_db": float(ev.GetMasterVolumeLevel()),
                "muted": bool(ev.GetMute()),
                "source": source,
            }
        except Exception as e:
            last_err = e
            continue
    _warn(f"Could not read endpoint volume/mute: {last_err}")
    return None


def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version, PackageNotFoundError
    except Exception:
        return None
    try:
        return version(name)
    except Exception:
        return None


def _sd_wasapi_supports_loopback() -> bool:
    import inspect
    import sounddevice as sd
    try:
        return "loopback" in inspect.signature(sd.WasapiSettings.__init__).parameters
    except Exception:
        return False


def _read_stimulus(stimulus_path: Path):
    import soundfile as sf
    stim, sr = sf.read(str(stimulus_path), dtype="float32", always_2d=True)
    if sr != EXPECTED_SR:
        _abort(f"Stimulus sample rate {sr} != {EXPECTED_SR}")
    if stim.shape[1] != EXPECTED_CHANNELS:
        _abort(f"Stimulus channel count {stim.shape[1]} != {EXPECTED_CHANNELS}")
    return stim, sr


def _play_and_record_sd(sd_idx: int, stim, sr: int, total_seconds: float):
    import sounddevice as sd

    n_total = int(round(total_seconds * sr))
    record_buf = np.zeros((n_total, EXPECTED_CHANNELS), dtype=np.float32)
    write_idx = {"i": 0}
    rec_done = threading.Event()

    def in_callback(indata, frames, time_info, status):
        if status:
            _warn(f"input status: {status}")
        i = write_idx["i"]
        end = min(i + frames, n_total)
        if end > i:
            record_buf[i:end] = indata[: end - i]
            write_idx["i"] = end
        if write_idx["i"] >= n_total:
            rec_done.set()
            raise sd.CallbackStop

    wasapi_settings_in = sd.WasapiSettings(loopback=True)

    in_stream = sd.InputStream(
        samplerate=sr,
        channels=EXPECTED_CHANNELS,
        dtype="float32",
        device=sd_idx,
        callback=in_callback,
        extra_settings=wasapi_settings_in,
        latency="low",
    )

    play_idx = {"i": 0}
    play_done = threading.Event()

    def out_callback(outdata, frames, time_info, status):
        if status:
            _warn(f"output status: {status}")
        i = play_idx["i"]
        end = min(i + frames, stim.shape[0])
        outdata[: end - i] = stim[i:end]
        if end - i < frames:
            outdata[end - i :] = 0
        play_idx["i"] = end
        if play_idx["i"] >= stim.shape[0]:
            play_done.set()
            raise sd.CallbackStop

    out_stream = sd.OutputStream(
        samplerate=sr,
        channels=EXPECTED_CHANNELS,
        dtype="float32",
        device=sd_idx,
        callback=out_callback,
        latency="low",
    )

    print("  starting input loopback…")
    in_stream.start()
    time.sleep(0.2)  # pre-roll
    print("  starting playback…")
    out_stream.start()

    play_done.wait(timeout=total_seconds + 5)
    rec_done.wait(timeout=total_seconds + 5)

    out_stream.close()
    in_stream.close()
    return record_buf


def _play_and_record_pyaudio(sd_info, stim, sr: int, total_seconds: float):
    try:
        import pyaudiowpatch as pa
    except ImportError:
        _abort(
            "sounddevice's WasapiSettings does not support loopback on this "
            "install, and pyaudiowpatch is not available.\n"
            "  Fix: pip install pyaudiowpatch"
        )

    n_total = int(round(total_seconds * sr))
    record_buf = np.zeros((n_total, EXPECTED_CHANNELS), dtype=np.float32)
    write_idx = {"i": 0}

    p = pa.PyAudio()
    try:
        target_name = sd_info["name"]
        loopback_dev = next(
            (d for d in p.get_loopback_device_info_generator()
             if d["name"].startswith(target_name)),
            None,
        )
        if loopback_dev is None:
            available = "\n    ".join(
                d["name"] for d in p.get_loopback_device_info_generator()
            ) or "(none)"
            _abort(
                f"pyaudiowpatch: no WASAPI loopback companion for "
                f"{target_name!r}.\n  Available loopback devices:\n    {available}"
            )

        out_idx = next(
            (i for i in range(p.get_device_count())
             if (info := p.get_device_info_by_index(i))["name"] == target_name
             and info["maxOutputChannels"] >= EXPECTED_CHANNELS),
            p.get_default_output_device_info()["index"],
        )

        chunk = 1024

        def in_callback(in_data, frame_count, time_info, status):
            arr = np.frombuffer(in_data, dtype=np.float32).reshape(
                -1, EXPECTED_CHANNELS
            )
            i = write_idx["i"]
            end = min(i + arr.shape[0], n_total)
            if end > i:
                record_buf[i:end] = arr[: end - i]
                write_idx["i"] = end
            if write_idx["i"] >= n_total:
                return (None, pa.paComplete)
            return (None, pa.paContinue)

        in_stream = p.open(
            format=pa.paFloat32,
            channels=EXPECTED_CHANNELS,
            rate=sr,
            frames_per_buffer=chunk,
            input=True,
            input_device_index=loopback_dev["index"],
            stream_callback=in_callback,
        )
        out_stream = p.open(
            format=pa.paFloat32,
            channels=EXPECTED_CHANNELS,
            rate=sr,
            frames_per_buffer=chunk,
            output=True,
            output_device_index=out_idx,
        )

        print("  starting input loopback…")
        in_stream.start_stream()
        time.sleep(0.2)
        print("  starting playback…")
        out_stream.start_stream()

        nstim = stim.shape[0]
        silence = np.zeros((chunk, EXPECTED_CHANNELS), dtype=np.float32)
        play_idx = 0
        while play_idx < nstim:
            end = min(play_idx + chunk, nstim)
            block = stim[play_idx:end]
            if block.shape[0] < chunk:
                pad = np.zeros(
                    (chunk - block.shape[0], EXPECTED_CHANNELS), dtype=np.float32
                )
                block = np.concatenate([block, pad], axis=0)
            out_stream.write(block.tobytes())
            play_idx = end

        # Keep the output stream fed with silence until the loopback callback
        # has captured the full tail, so the device doesn't underrun.
        deadline = time.monotonic() + total_seconds + 5
        while write_idx["i"] < n_total and time.monotonic() < deadline:
            out_stream.write(silence.tobytes())

        out_stream.stop_stream(); out_stream.close()
        in_stream.stop_stream(); in_stream.close()
    finally:
        p.terminate()

    return record_buf


def play_and_record(sd_idx: int, sd_info, stimulus_path: Path, total_seconds: float):
    stim, sr = _read_stimulus(stimulus_path)
    if _sd_wasapi_supports_loopback():
        return _play_and_record_sd(sd_idx, stim, sr, total_seconds)
    _info("sounddevice WasapiSettings lacks loopback; using pyaudiowpatch backend.")
    return _play_and_record_pyaudio(sd_info, stim, sr, total_seconds)


def _stimulus_tag(stim_path: Path, meta: dict) -> str:
    """Unique-per-variant tag derived from the stimulus filename.

    'stimulus_sweep_quiet.wav' -> 'sweep_quiet'. Falls back to the JSON
    'kind' field if the filename doesn't match the expected pattern, so
    custom --stimulus paths still produce a sensible tag."""
    stem = stim_path.stem
    prefix = "stimulus_"
    if stem.startswith(prefix) and len(stem) > len(prefix):
        return stem[len(prefix):]
    return str(meta.get("kind", "stimulus"))


def post_capture_checks(
    label: str, stim_tag: str, stim_kind: str, capture: np.ndarray,
    sr: int, out_dir: Path, skip_baseline: bool,
) -> dict:
    peak = float(np.max(np.abs(capture)))
    rms = float(np.sqrt(np.mean(capture ** 2)))
    clip_count = int(np.sum(np.abs(capture) >= 1.0))
    peak_db = 20.0 * np.log10(peak + 1e-12)
    rms_db = 20.0 * np.log10(rms + 1e-12)
    _info(f"capture peak {peak_db:+.1f} dBFS, RMS {rms_db:+.1f} dBFS, "
          f"clip samples: {clip_count}")
    if clip_count > 0:
        _warn("clipping detected — lower system volume and re-capture.")
    if peak_db < -60.0:
        _warn(f"capture peak unusually quiet ({peak_db:.1f} dBFS); raise volume.")

    metrics = {
        "samples": int(capture.shape[0]),
        "channels": int(capture.shape[1]),
        "peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
        "clip_samples": clip_count,
        "similarity_to_off": None,
    }

    # Per-stimulus baseline lookup, in priority order:
    #   1. exact-tag match (loopback_<tag>_off.wav) — best, same level + content
    #   2. kind-only fallback (loopback_<kind>_off.wav) — cross-correlation is
    #      level-invariant so this still works for _quiet variants
    #   3. legacy single-baseline layout (loopback_off.wav)
    baseline = out_dir / f"loopback_{stim_tag}_off.wav"
    if not baseline.exists():
        kind_baseline = out_dir / f"loopback_{stim_kind}_off.wav"
        if kind_baseline.exists():
            baseline = kind_baseline
        else:
            legacy = out_dir / "loopback_off.wav"
            if legacy.exists():
                baseline = legacy
    if skip_baseline or label == "off" or not baseline.exists():
        return metrics
    try:
        import soundfile as sf
        bl, _ = sf.read(str(baseline), dtype="float32", always_2d=True)
        n = min(bl.shape[0], capture.shape[0])
        bl = bl[:n]
        cap = capture[:n]
        # normalized cross-correlation peak per channel
        from scipy.signal import correlate
        sims = []
        for ch in range(EXPECTED_CHANNELS):
            a = bl[:, ch] - bl[:, ch].mean()
            b = cap[:, ch] - cap[:, ch].mean()
            denom = np.sqrt(np.sum(a * a) * np.sum(b * b)) + 1e-30
            xc = correlate(a, b, mode="valid") / denom
            sims.append(float(np.max(np.abs(xc))))
        sim = max(sims)
        metrics["similarity_to_off"] = sim
        _info(f"similarity to loopback_off.wav: {sim:.4f}")
        if sim > 0.98:
            _warn(
                "capture is essentially identical to the OFF baseline — "
                "DAX3 may not have been active. Verify the profile in "
                "Dolby Access and re-run, or pass --skip-baseline-check."
            )
    except Exception as e:
        _warn(f"baseline-similarity check failed: {e}")
    return metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, choices=VALID_LABELS,
                    help="profile label, drives the output filename")
    ap.add_argument("--device",
                    help="substring of WASAPI output device name (default: system default)")
    ap.add_argument("--stimulus",
                    default=str(Path(__file__).resolve().parent / "stimulus.wav"))
    ap.add_argument("--out",
                    default=str(Path(__file__).resolve().parent / "captures"))
    ap.add_argument("--skip-baseline-check", action="store_true")
    ap.add_argument(
        "--no-apo-check",
        action="store_true",
        help="bypass the Dolby-presence guard (use when you can verify "
             "manually that Atmos is the active spatial mode)",
    )
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stim_path = Path(args.stimulus)
    if not stim_path.is_file():
        _abort(f"stimulus not found: {stim_path}")
    # Per-stimulus meta: <stimulus_basename>.json (e.g. stimulus_pink.json).
    # Fall back to the legacy stimulus_meta.json next to the stimulus for
    # backwards compatibility with the original single-stimulus layout.
    meta_path = stim_path.with_suffix(".json")
    if not meta_path.is_file():
        legacy = stim_path.with_name("stimulus_meta.json")
        if legacy.is_file():
            meta_path = legacy
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text())
    else:
        _warn("stimulus meta JSON missing; assuming legacy sweep defaults")
        meta = {"kind": "sweep", "duration_seconds": 11.0,
                "active_seconds": 10.0, "tail_seconds": 1.0,
                "sample_rate": EXPECTED_SR}
    stim_kind = str(meta.get("kind", "sweep"))
    stim_tag = _stimulus_tag(stim_path, meta)

    print("=" * 60)
    print(f"DAX3 capture — tag: {stim_tag} (kind: {stim_kind}), "
          f"label: {args.label}")
    print("=" * 60)

    sd_idx, sd_info, mmdev = resolve_endpoint(args.device)
    verify_endpoint_format(sd_info)
    dolby_detection = detect_dolby(mmdev, args.label, args.no_apo_check)
    dolby_state = best_effort_dolby_state(args.label)
    endpoint_volume = read_endpoint_volume(mmdev)
    if endpoint_volume is not None:
        muted = "MUTED" if endpoint_volume["muted"] else "unmuted"
        _info(
            f"endpoint volume: {endpoint_volume['scalar']*100:.0f}% "
            f"({endpoint_volume['level_db']:+.1f} dB), {muted}"
        )
        if endpoint_volume["muted"]:
            _warn(
                "endpoint is muted — capture will be silent. "
                "Unmute and re-run."
            )

    duration_seconds = meta.get("duration_seconds")
    if duration_seconds is None:
        duration_seconds = (
            meta.get("active_seconds", meta.get("sweep_seconds", 10.0))
            + meta.get("tail_seconds", 1.0)
        )
    total_seconds = duration_seconds + LOOPBACK_PADDING_SEC
    capture = play_and_record(sd_idx, sd_info, stim_path, total_seconds)

    out_path = out_dir / f"loopback_{stim_tag}_{args.label}.wav"
    import soundfile as sf
    sf.write(str(out_path), capture, EXPECTED_SR, subtype="FLOAT")
    _info(f"wrote {out_path} ({capture.shape[0]} samples)")

    capture_metrics = post_capture_checks(
        args.label, stim_tag, stim_kind, capture, EXPECTED_SR, out_dir,
        args.skip_baseline_check,
    )

    metadata = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "label": args.label,
        "stimulus_tag": stim_tag,
        "stimulus_kind": stim_kind,
        "wav_filename": out_path.name,
        "endpoint": {
            "name": sd_info["name"],
            "sounddevice_index": sd_idx,
            "default_samplerate_hz": int(round(sd_info["default_samplerate"])),
            "max_output_channels": int(sd_info["max_output_channels"]),
            "mmdevice_friendly_name": (
                getattr(mmdev, "FriendlyName", None) if mmdev is not None else None
            ),
        },
        "endpoint_volume": endpoint_volume,
        "audio_backend": (
            "sounddevice (WasapiSettings loopback)"
            if _sd_wasapi_supports_loopback()
            else "pyaudiowpatch"
        ),
        "dolby_detection": dolby_detection,
        "dolby_access_state": dolby_state,
        "stimulus": {
            "path": str(stim_path),
            "sample_rate_hz": EXPECTED_SR,
            "channels": EXPECTED_CHANNELS,
            "sha256": _sha256_file(stim_path),
            "sweep_seconds": meta.get("sweep_seconds"),
            "tail_seconds": meta.get("tail_seconds"),
            "stimulus_meta": meta,
        },
        "capture": capture_metrics,
        "system": {
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "sounddevice_version": _pkg_version("sounddevice"),
            "pyaudiowpatch_version": _pkg_version("PyAudioWPatch"),
            "soundfile_version": _pkg_version("soundfile"),
            "pycaw_version": _pkg_version("pycaw"),
            "numpy_version": _pkg_version("numpy"),
            "scipy_version": _pkg_version("scipy"),
        },
    }
    json_path = out_path.with_suffix(".json")
    json_path.write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    _info(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
