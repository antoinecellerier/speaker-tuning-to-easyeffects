"""Shared helpers for tests.

Test inputs are constructed in Python — never copied from real Dolby
tuning. The "shape" of the data here is the public DAX3 schema; the
*values* are deliberately synthetic.
"""

from __future__ import annotations

import math
import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import freqz

# Make the converter importable from any test module.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="run tests marked `slow` (e.g. the ee_to_pipewire corpus tier, "
             "which validates every discovered XML's PW conf through lv2info "
             "— minutes on a large corpus). ATMOS_RUN_SLOW=1 does the same.",
    )


def pytest_collection_modifyitems(config, items):
    """Skip `slow`-marked tests unless opted in via --run-slow / ATMOS_RUN_SLOW."""
    if config.getoption("--run-slow") or os.environ.get("ATMOS_RUN_SLOW"):
        return
    skip_slow = pytest.mark.skip(
        reason="slow: pass --run-slow or set ATMOS_RUN_SLOW=1 to run"
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture
def silence_console(monkeypatch):
    """Drop the rich console, so `cprint()` takes its plain-`print` path
    for the rest of the test.

    Every user-facing line goes through `cprint`, which hands the text to a
    module-global `_CONSOLE` whenever rich is installed. What `capsys` then
    sees is styled and console-width-dependent, so an `assert "..." in out`
    on a phrase passes or fails depending on the machine the suite ran on
    and on whether an optional dependency happens to be present. With
    `_CONSOLE` set to None the string arrives verbatim, which is what these
    assertions are written against — the point is capture fidelity, not
    quiet output.

    The module is named at the call site rather than baked in here, so this
    file stays ignorant of the layout around it. Today there is one console
    to name — `lib.console`, which both scripts print through — and passing
    it is also the reminder that silencing it silences the pair.

        def test_x(silence_console, capsys):
            silence_console(console)

    Deliberately not autouse — a few tests are *about* `_CONSOLE` (that the
    fallback prints at all, that `--no-color` clears the global) and set it
    themselves. `monkeypatch.setattr` raises on a missing attribute, which
    is the wanted behaviour: if the console is renamed or moves out of the
    script, every caller fails loudly instead of silently asserting on the
    styled text it meant to avoid.
    """
    def silence(*modules):
        for module in modules:
            monkeypatch.setattr(module, "_CONSOLE", None)

    return silence


@pytest.fixture(autouse=True)
def no_live_easyeffects_probe(monkeypatch):
    """The PipeWire converter probes for a running EasyEffects process at
    conf-write time (`checks.warn_if_easyeffects_running`). On a dev
    machine EasyEffects often *is* running, so without this the warning
    joins every real-write run's output and any closing-output assertion
    becomes machine-dependent. Autouse-forced quiet; tests of the warning
    itself pass `running=` explicitly, and the probe's own test keeps a
    module-level reference to the unpatched function
    (tests/test_pw_doctor.py)."""
    from lib.pipewire import checks
    monkeypatch.setattr(checks, "easyeffects_running", lambda: False)


@pytest.fixture(autouse=True)
def no_live_easyeffects_socket(monkeypatch):
    """The generator now *loads* a preset into a running EasyEffects at the
    end of a real run (lib/preset/reload.py). On a dev machine that is the
    maintainer's own session, so every CLI test here would swap what he is
    listening to. Pinned to "no socket" — the ordinary not-running case;
    tests of the socket itself put one back with `_fake_socket`."""
    from lib import ee_socket
    monkeypatch.setattr(ee_socket, "_socket_path", lambda: None)


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


def lsp_rlc_bell(f0, gain_db, q, fs=48000):
    """LSP para-equalizer RLC (BT) peaking-EQ biquad — what EasyEffects
    actually realizes for a Bell band (mode "RLC (BT)"), NOT the RBJ
    cookbook. Per lsp-dsp-units Filter.cpp FLT_BT_RLC_BELL (slope 1): an
    analog prototype H(s) = (s² + kt·s + 1)/(s² + kb·s + 1) — kt/kb set by
    a gain-angle — bilinear-transformed with a prewarp at f0. Peak gain is
    exactly gain_db at f0, but the bell is ~25% wider than the RBJ bell at
    the same numeric q for q>1 (the q-mode convention difference; see
    docs/design-notes.md audit table)."""
    g = 10 ** (gain_db / 20.0)
    angle = math.atan(g)
    k = 2.0 * (1.0 / g + g) / (1.0 + 2.0 * q)
    kt = k * math.sin(angle)
    kb = k * math.cos(angle)
    c = 1.0 / math.tan(math.pi * f0 / fs)   # prewarped bilinear (cotangent)
    c2 = c * c
    a0 = c2 + kb * c + 1.0
    b = np.array([c2 + kt * c + 1.0, 2.0 - 2.0 * c2, c2 - kt * c + 1.0]) / a0
    a = np.array([1.0, (2.0 - 2.0 * c2) / a0, (c2 - kb * c + 1.0) / a0])
    return b, a


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


def synthetic_mb_comp(group_count: int, bands, target_power: float = -5.0):
    """Build the mb_comp dict parse_xml produces.

    `bands` is a list of (xover_idx, threshold_q4, gain_q15, attack_q15,
    release_q15, makeup_q4) tuples — Q-format raw integers, exactly as
    parse_xml produces from the XML.

    `target_power` is read-only: no emitted parameter uses it, but the run
    report prints it, so a fixture without it can't drive the report.
    """
    return {
        "group_count": group_count,
        "band_groups": list(bands),
        "target_power": target_power,
    }


def synthetic_regulator(threshold_high, distortion_slope=1.0,
                       timbre_preservation=0.75, isolated_band=None):
    """Build a regulator dict consumed by make_regulator.

    `threshold_high` is a 20-element list (one per band) in dB.
    `isolated_band` is the optional 0/1-per-band list parse_xml stores
    (None when the XML lacks the field).
    """
    return {
        "threshold_high": list(threshold_high),
        "threshold_low": [-12.0] * 20,
        "stress": [0.0] * 8,
        "distortion_slope": distortion_slope,
        "timbre_preservation": timbre_preservation,
        "overdrive": 0,
        "relaxation": 96,
        "isolated_band": list(isolated_band) if isolated_band else None,
    }


def synthetic_virtual_bass():
    """The virtual_bass dict parse_xml produces: raw schema values (freqs in
    Hz, gains in 1/16 dB). The values are the corpus-frozen ones every XML
    carries — also what write_synthetic_tuning_xml embeds, so the end-to-end
    and unit fixtures agree.
    """
    return {
        "mode": 0,
        "src_freqs": [35, 160],
        "mix_freqs": [94, 469],
        "subgains": [-32, -144, -192],
        "overall_gain": 0,
        "slope_gain": 0,
    }


def write_synthetic_tuning_xml(path: Path, default_profile: str | None = None,
                               ao_right: str | None = None) -> Path:
    """Write a minimal-but-complete DAX3 playback XML that parse_xml()
    accepts end-to-end: the 20-band grid plus the three ieq_* curves in
    <constant>, and one internal_speaker/normal endpoint whose dynamic
    profile carries tuning-cp (IEQ enabled) and the two audio-optimizer
    channels. All values are synthetic 1/16-dB integers.

    ``default_profile`` adds the optional <setting><default_profile> element
    (Dolby's declared shipping profile); omitted by default, as on most XMLs.

    ``ao_right`` overrides ch_01 with its own 1/16-dB CSV, giving the two
    channels different audio-optimizer peaks — the 7.2%-of-corpus case that
    --enable level-restore re-references against. Defaults to matching
    ch_00, which is what most tunings do.
    """
    freqs = ",".join(str(f) for f in SYNTHETIC_FREQS_20)
    curves = {
        "ieq_balanced": ",".join(str(16 * (i % 5 - 2)) for i in range(20)),
        "ieq_detailed": ",".join(str(16 * (i % 7 - 3)) for i in range(20)),
        "ieq_warm": ",".join(str(16 * (2 - i % 4)) for i in range(20)),
    }
    curve_els = "\n    ".join(
        f'<{name} target="{vals}"/>' for name, vals in curves.items())
    ao = ",".join(str(8 * (i % 3 - 1)) for i in range(20))
    # A regulator that limits the bottom half and leaves the top half at full
    # scale, with isolated_band marking that top half non-isolated — i.e. the
    # shape the coupled-bands mapping acts on. Present so the end-to-end case
    # walks parse_xml's isolated_band read into make_regulator's default
    # path; without a regulator here nothing in the fast tier did, and the
    # corpus tier that would is `slow` and skips with no corpus.
    reg_th = ",".join(str(-96 if i < 10 else 0) for i in range(20))   # 1/16 dB
    reg_iso = ",".join(str(1 if i < 10 else 0) for i in range(20))
    setting = (f'\n  <setting><default_profile value="{default_profile}"/></setting>'
               if default_profile else "")
    path.write_text(f"""<dax3>
  <constant>
    <band_20_freq fs_48000="{freqs}"/>
    {curve_els}
  </constant>{setting}
  <endpoint type="internal_speaker" operating_mode="normal">
    <profile type="dynamic">
      <tuning-cp>
        <ieq-enable value="1"/>
        <ieq-amount value="10"/>
        <virtual-bass-mode value="0"/>
        <virtual-bass-src-freqs value="35,160"/>
        <virtual-bass-mix-freqs value="94,469"/>
        <virtual-bass-subgains value="-32,-144,-192"/>
        <virtual-bass-overall-gain value="0"/>
        <virtual-bass-slope-gain value="0"/>
      </tuning-cp>
      <tuning-vlldp>
        <audio-optimizer-bands>
          <ch_00 value="{ao}"/>
          <ch_01 value="{ao_right or ao}"/>
        </audio-optimizer-bands>
        <regulator-speaker-dist-enable value="1"/>
        <regulator-tuning>
          <threshold_high value="{reg_th}"/>
          <isolated_band value="{reg_iso}"/>
        </regulator-tuning>
      </tuning-vlldp>
    </profile>
  </endpoint>
</dax3>
""", encoding="utf-8")
    return path


def assert_rows_line_up(lines, gutter):
    """Every labelled row pads its value to *gutter*; continuations and group
    breaks sit on or past it. One helper for both doctors' inventory blocks,
    for the reason the code shares `doctor_layout.row`: two copies drift."""
    for line in lines:
        if not line or line.startswith(" " * gutter):
            continue  # a group break, or a continuation already on the gutter
        label, _, _rest = line.partition(":")
        assert len(label) + 1 <= gutter - 1, line   # room for one space after
        assert line[gutter] != " ", line
        assert line[gutter - 1] == " ", line
