"""WAV reader that drops the optional PEAK chunk before handing off to scipy.

pw-record (and other libsndfile-backed capture tools) embeds a PEAK chunk
holding peak amplitudes per channel. scipy.io.wavfile doesn't recognise
it and prints `WavFileWarning: Chunk (non-data) not understood, skipping
it.` on every read. We don't use the PEAK metadata, so strip it from the
RIFF and the warning never fires.
"""

import io
import struct
from pathlib import Path

from scipy.io import wavfile


def read(path):
    """Drop-in replacement for scipy.io.wavfile.read that tolerates PEAK."""
    raw = Path(path).read_bytes()
    return wavfile.read(io.BytesIO(_strip_peak(raw)))


def _strip_peak(raw: bytes) -> bytes:
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    out = bytearray(raw[:12])
    i = 12
    n = len(raw)
    while i + 8 <= n:
        cid = raw[i:i + 4]
        sz = struct.unpack("<I", raw[i + 4:i + 8])[0]
        total = 8 + sz + (sz & 1)  # RIFF chunks pad to even length
        if cid != b"PEAK":
            out += raw[i:i + total]
        i += total
    out[4:8] = struct.pack("<I", len(out) - 8)
    return bytes(out)
