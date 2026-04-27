"""Shared helpers for tests.

Test inputs are constructed in Python — never copied from real Dolby
tuning. The "shape" of the data here is the public DAX3 schema; the
*values* are deliberately synthetic.
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np
from scipy.signal import freqz

# Make the converter importable from any test module.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Representative 20-band frequency table. Real DAX3 XMLs ship their own
# `band_20_freq` element; this is a typical log-spaced set in the same
# range, used purely as a non-proprietary stand-in.
SYNTHETIC_FREQS_20 = [
    50, 80, 125, 160, 200, 250, 315, 400, 500, 630,
    800, 1000, 1600, 2500, 4000, 6300, 8000, 10000, 12500, 16000,
]


def biquad_response_db(b, a, freqs, fs=48000):
    """|H(z)| in dB at arbitrary frequencies via scipy.signal.freqz."""
    w = 2 * math.pi * np.asarray(freqs, dtype=float) / fs
    _, h = freqz(b, a, worN=w)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-10))


def rbj_bell(f0, gain_db, q, fs=48000):
    """RBJ audio cookbook peaking-EQ biquad."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    cos_w = math.cos(w0)
    b0 = 1 + alpha * a
    b1 = -2 * cos_w
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * cos_w
    a2 = 1 - alpha / a
    return (np.array([b0, b1, b2]) / a0,
            np.array([1.0, a1 / a0, a2 / a0]))


def rbj_hishelf(f0, gain_db, q, fs=48000):
    """RBJ audio cookbook high-shelf biquad."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    cos_w = math.cos(w0)
    sqa = 2 * math.sqrt(a) * alpha
    b0 = a * ((a + 1) + (a - 1) * cos_w + sqa)
    b1 = -2 * a * ((a - 1) + (a + 1) * cos_w)
    b2 = a * ((a + 1) + (a - 1) * cos_w - sqa)
    a0 = (a + 1) - (a - 1) * cos_w + sqa
    a1 = 2 * ((a - 1) - (a + 1) * cos_w)
    a2 = (a + 1) - (a - 1) * cos_w - sqa
    return (np.array([b0, b1, b2]) / a0,
            np.array([1.0, a1 / a0, a2 / a0]))


def rbj_loshelf(f0, gain_db, q, fs=48000):
    """RBJ audio cookbook low-shelf biquad."""
    a = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    cos_w = math.cos(w0)
    sqa = 2 * math.sqrt(a) * alpha
    b0 = a * ((a + 1) - (a - 1) * cos_w + sqa)
    b1 = 2 * a * ((a - 1) - (a + 1) * cos_w)
    b2 = a * ((a + 1) - (a - 1) * cos_w - sqa)
    a0 = (a + 1) + (a - 1) * cos_w + sqa
    a1 = -2 * ((a - 1) + (a + 1) * cos_w)
    a2 = (a + 1) + (a - 1) * cos_w - sqa
    return (np.array([b0, b1, b2]) / a0,
            np.array([1.0, a1 / a0, a2 / a0]))


def fir_freq_response_db(fir, fs=48000, n_fft=None):
    """FFT-magnitude (dB) of an FIR, returned with its frequency axis."""
    fir = np.asarray(fir, dtype=float)
    if n_fft is None:
        n_fft = len(fir)
    spectrum = np.fft.rfft(fir, n=n_fft)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)
    mag_db = 20 * np.log10(np.maximum(np.abs(spectrum), 1e-12))
    return freqs, mag_db


def is_minimum_phase(fir, tol=1e-6):
    """A FIR is minimum-phase iff its complex cepstrum is causal — the
    negative-time samples are ~0. Defined for symmetric (length-N) IRs.
    """
    fir = np.asarray(fir, dtype=float)
    n = len(fir)
    spectrum = np.fft.fft(fir)
    log_spec = np.log(np.maximum(np.abs(spectrum), 1e-12)) + 1j * np.unwrap(np.angle(spectrum))
    cepstrum = np.fft.ifft(log_spec).real
    # negative-time half of a length-N cepstrum is indices n//2+1 .. n-1
    neg_energy = np.sum(np.abs(cepstrum[n // 2 + 1:]))
    pos_energy = np.sum(np.abs(cepstrum[:n // 2 + 1])) + 1e-12
    return neg_energy / pos_energy < tol


def read_irs_file(path: Path):
    """Read an EasyEffects .irs file (RIFF/WAVE float32 stereo).

    Returns (sample_rate, n_samples, n_channels, samples_left, samples_right).
    Uses the wave module fallback for header validation, then numpy for
    the float32 payload.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AssertionError(f"{path}: not a RIFF/WAVE file")
    # Walk chunks to find fmt and data
    i = 12
    fmt = None
    payload = None
    while i + 8 <= len(data):
        chunk_id = data[i:i + 4]
        chunk_size = struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + chunk_size]
        if chunk_id == b"fmt ":
            fmt = body
        elif chunk_id == b"data":
            payload = body
        i += 8 + chunk_size + (chunk_size & 1)
    if fmt is None or payload is None:
        raise AssertionError(f"{path}: missing fmt or data chunk")
    audio_format, n_channels, sample_rate = struct.unpack("<HHI", fmt[:8])
    bits_per_sample = struct.unpack("<H", fmt[14:16])[0]
    # 3 = WAVE_FORMAT_IEEE_FLOAT
    if audio_format != 3 or bits_per_sample != 32:
        raise AssertionError(
            f"{path}: expected float32 WAVE, got format={audio_format} "
            f"bps={bits_per_sample}"
        )
    samples = np.frombuffer(payload, dtype="<f4").reshape(-1, n_channels)
    left = samples[:, 0]
    right = samples[:, 1] if n_channels > 1 else samples[:, 0]
    return sample_rate, samples.shape[0], n_channels, left, right


def synthetic_peq_filters(types_and_params):
    """Build a peq_filters list matching parse_xml's output shape.

    Each entry in `types_and_params` is a tuple
        (speaker, filter_type, f0, gain, q, order, s)
    matching the dict keys consumed by make_peq_eq.
    """
    return [
        {
            "speaker": speaker,
            "type": ftype,
            "f0": f0,
            "gain": gain,
            "q": q,
            "order": order,
            "s": s,
        }
        for (speaker, ftype, f0, gain, q, order, s) in types_and_params
    ]


def synthetic_mb_comp(group_count: int, bands):
    """Build the mb_comp dict consumed by make_multiband_compressor.

    `bands` is a list of (xover_idx, threshold_q4, gain_q15, attack_q15,
    release_q15, makeup_q4) tuples — Q-format raw integers, exactly as
    parse_xml produces from the XML.
    """
    return {
        "group_count": group_count,
        "band_groups": list(bands),
    }


def synthetic_regulator(threshold_high, distortion_slope=1.0,
                       timbre_preservation=0.75):
    """Build a regulator dict consumed by make_regulator.

    `threshold_high` is a 20-element list (one per band) in dB.
    """
    return {
        "threshold_high": list(threshold_high),
        "threshold_low": [-12.0] * 20,
        "stress": [0.0] * 8,
        "distortion_slope": distortion_slope,
        "timbre_preservation": timbre_preservation,
    }
