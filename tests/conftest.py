"""Shared helpers for tests.

Test inputs are constructed in Python — never copied from real Dolby
tuning. The "shape" of the data here is the public DAX3 schema; the
*values* are deliberately synthetic.
"""

from __future__ import annotations

import collections
import math
import os
import re
import struct
import sys
from pathlib import Path

# One BLAS thread per process. xdist already spreads the suite over every core,
# and OpenBLAS would add a pool of one spinning thread per core to each worker
# and to each script a test starts: a converter run cost 1.7 s of CPU for 0.3 s
# of wall with the pool, 0.2 s without (2026-09-23). Set before numpy is first
# imported, which is when OpenBLAS reads it; children inherit it.
for _blas_threads in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                      "MKL_NUM_THREADS"):
    os.environ.setdefault(_blas_threads, "1")

import numpy as np  # noqa: E402 — after the thread cap above
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
    """Both scripts probe for a running EasyEffects process — the PipeWire
    converter at conf-write time (`checks.warn_if_easyeffects_running`) and
    the EasyEffects doctor's fact-gathering. On a dev machine EasyEffects
    often *is* running, so without this the warning joins every real-write
    run's output and the doctor's `running:` row becomes machine-dependent.
    Autouse-forced quiet; tests of the warning itself pass `running=`
    explicitly, and the probe's own test keeps a module-level reference to
    the unpatched function (tests/test_pw_doctor.py)."""
    from lib import ee_socket
    monkeypatch.setattr(ee_socket, "easyeffects_running", lambda: False)


@pytest.fixture(autouse=True)
def fresh_lv2_loader_probe():
    """`session.lv2_loader` answers once per process, and a pytest-xdist
    worker is one process for many tests: without this, the first test's
    answer (usually "pw-cli not found", with the machine off) would be every
    later test's, whatever it stubs."""
    from lib.pipewire import session
    session.forget_lv2_loader()
    yield
    session.forget_lv2_loader()


@pytest.fixture(autouse=True)
def no_live_easyeffects_socket(monkeypatch):
    """The generator now *loads* a preset into a running EasyEffects at the
    end of a real run (lib/preset/reload.py). On a dev machine that is the
    maintainer's own session, so every CLI test here would swap what he is
    listening to. Pinned to "no socket" — the ordinary not-running case;
    tests of the socket itself put one back with `_fake_socket`."""
    from lib import ee_socket
    monkeypatch.setattr(ee_socket, "_socket_path", lambda: None)


# The host locations lib/ reads machine state from (lib/host.py), as the
# audit hook below sees them — `/sys/devices/pci…` is where the sound-class
# symlinks resolve to. Deliberately not all of /proc and /sys: Python and
# xdist read their own (`/proc/self`, the CPU topology) and those aren't ours.
_HOST_STATE = ("/proc/asound", "/proc/mounts", "/proc/uptime",
               "/proc/sys/kernel", "/sys/class/sound", "/sys/class/dmi",
               "/sys/class/firmware-attributes", "/sys/bus/soundwire",
               "/sys/module", "/sys/devices/pci", "/etc/os-release",
               "/lib/firmware", "/var/lib/flatpak")
_host_reads: list[str] = []
_watching_host = False


def _record_host_reads(event, args):
    if not _watching_host or event not in ("open", "os.listdir", "os.scandir"):
        return
    target = args[0] if args else None
    if isinstance(target, os.PathLike):
        target = os.fspath(target)
    if isinstance(target, bytes):
        target = target.decode(errors="replace")
    if isinstance(target, str) and target.startswith(_HOST_STATE):
        _host_reads.append(target)


# Once per process: an audit hook can't be removed, so it stays in and is
# armed per test by the fixture below.
sys.addaudithook(_record_host_reads)


@pytest.fixture(scope="session")
def _empty_host_root(tmp_path_factory):
    return tmp_path_factory.mktemp("host-root")


@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A host root of the test's own, for `lib/host.py` to read (child
    processes too): `fake_host("/etc/os-release", "ID=debian\\n")` puts a file
    where the code will look for that host path."""
    from lib import host
    root = tmp_path / "host-root"
    root.mkdir()
    monkeypatch.setenv(host.HOST_ROOT, str(root))

    def put(location: str, text: str) -> Path:
        target = root / location.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    return put


@pytest.fixture(autouse=True)
def no_live_machine(request, monkeypatch, _empty_host_root):
    """No test reads this machine's tools or hardware unless it says so.

    Before this, ~1,800 in-process tool calls per fast run reached the real
    `amixer`, `pw-dump`, `pw-top`, `easyeffects --version`…, and 680 reads
    went to the real `/proc/asound/card*/codec#*` (2026-09-23). A rendered
    report depended on the audio stack of whoever ran the suite, a doctor
    render sat out a live five-second `pw-top` window, and the codec reads —
    which the kernel serialises — left the workers queued for 456 of the 518
    seconds they spent waiting. Both switches are environment variables, not
    patches, so the scripts a test starts as child processes are covered too:
    `lib/tool_env.py` then reports every tool uninstalled (CI's state), and
    `lib/host.py` reads `/proc`, `/sys`, `/etc` under an empty root — a machine
    with no sound hardware and nothing identifying.

    A test that needs a tool's answer replaces `tool_env.run` / `tool_env.which`
    (children: `ATMOS_FAKE_TOOLS_DIR`); one that needs hardware passes its own
    tree as a parameter, patches the module's path constant, or points
    `ATMOS_HOST_ROOT` at a fake root. A test that exists to exercise the real
    machine takes `@pytest.mark.live_machine`, and skips where it can't.

    Anything that still opens a real host location during a test fails that
    test, naming the path: that read has gone around `lib/host.py`."""
    global _watching_host
    from lib import host, tool_env
    if request.node.get_closest_marker("live_machine") is not None:
        monkeypatch.delenv(tool_env.NO_LIVE_TOOLS, raising=False)
        monkeypatch.delenv(host.HOST_ROOT, raising=False)
        yield
        return
    monkeypatch.setenv(tool_env.NO_LIVE_TOOLS, "1")
    monkeypatch.setenv(host.HOST_ROOT, str(_empty_host_root))
    # The user's config directory is machine state too: the --doctor probes
    # scan its autostart entries and session startup files. Constants derived
    # from it at import time keep their value in-process.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_empty_host_root / "config-home"))
    monkeypatch.delenv("XDG_CONFIG_DIRS", raising=False)
    # Which desktop this is decides which startup files count.
    monkeypatch.delenv("XDG_CURRENT_DESKTOP", raising=False)
    _host_reads.clear()
    _watching_host = True
    try:
        yield
    finally:
        _watching_host = False
    assert not _host_reads, (
        "this test read the real machine, around lib/host.py: "
        f"{', '.join(sorted(set(_host_reads))[:5])} — route the read through "
        "host.path(), or mark the test live_machine")


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
    channels different audio-optimizer peaks — the case (19.1% of corpus
    files on 2026-08-04) that --enable level-restore re-references against.
    Defaults to matching ch_00, which is what most tunings do.
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


def assert_summary_counts_the_printed_lines(out):
    """The `Summary:` totals must equal the tagged check lines above them.

    Both doctors close their check block with a summary, and a reader who
    cannot make the two agree reads it as the tool failing to count. The
    EasyEffects report did exactly that: its summary totalled the checks
    *behind* a collapsed line rather than the lines on screen, so six [PASS]
    lines sat under "8 PASS" (/user-review, CRITICAL in two rounds). Nothing
    pinned that wiring in either direction — the fix would have been as
    invisible as the bug. This is the pin, and it is shared because the two
    reports print through one function and two copies drift.

    Returns the counted statuses, so a caller can assert the report it
    rendered was worth counting.
    """
    head, sep, tail = out.partition("Summary:")
    assert sep, "no summary line in this report"
    # Count only the check block: the inventory above it is free text, and
    # `0 [sofhdadsp     ]: sof-hda-dsp` is one grep away from looking tagged.
    lines = head.splitlines()
    start = max((i for i, ln in enumerate(lines) if ln.startswith("=== ")),
                default=0)
    counts = collections.Counter()
    for line in lines[start:]:
        # `[{status:^4}]`, so UNKNOWN is `[ ?  ]` — one space before and two
        # after. Match the width, never the literal `[ ? ]`.
        m = re.match(r"  \[(.{4})\] ", line)
        if m:
            counts[m.group(1).strip()] += 1
    summary = tail.splitlines()[0]
    stated = {word: int(n) for n, word in re.findall(r"(\d+) (\w+)", summary)}
    # UNKNOWN is omitted from the line when zero, hence the .get default.
    for status, word in (("FAIL", "FAIL"), ("WARN", "WARN"),
                         ("PASS", "PASS"), ("?", "UNKNOWN")):
        assert counts[status] == stated.get(word, 0), (
            f"{word}: {counts[status]} line(s) printed, "
            f"{stated.get(word, 0)} in 'Summary:{summary}'")
    return counts


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


# --- Synthetic Microsoft Surface APO package --------------------------------
# The shape follows a shipped SurfaceAPOExtension.inf and SurfaceAPO_<id>.json.
# Every value is invented: a 2nd-order 60 Hz high-pass and an RBJ bell for the
# EQ, round numbers for the dynamics.

def synthetic_surface_eq(fs=48000):
    """Per-channel (b0,b1,b2,a1,a2) sections: a 60 Hz HPF and a 3 kHz dip."""
    from scipy.signal import butter
    hb, ha = butter(2, 60.0, btype="highpass", fs=fs)
    bb, ba = rbj_bell(3000.0, -6.0, 1.2, fs=fs)
    return [tuple(float(x) for x in (*hb, *ha[1:])),
            tuple(float(x) for x in (*bb, *ba[1:]))]


def _apo_block(name, fs, **params):
    fmt = [{"sample_rate": fs, "channel_count": 2, "container_size": 32,
            "bit_depth": 32, "bit_offset": 0, "data_type": "float",
            "interleave_type": "channelsinterleaved"}]
    children = [{"type": "primitive", "id": 9, "name": "InputFormats",
                 "value": fmt},
                {"type": "primitive", "id": 9, "name": "OutputFormats",
                 "value": fmt}]
    children += [{"type": "primitive", "id": 9, "name": k, "value": v}
                 for k, v in params.items()]
    return {"type": "complex", "id": 3, "name": name, "children": children}


def surface_apo_json(fs=48000, sections=None, identity_pad=1):
    """A SurfaceAPO config dict with the shipped R/EFX block set."""
    sections = synthetic_surface_eq(fs) if sections is None else sections
    coeffs = []
    for s in list(sections) + [(1.0, 0.0, 0.0, 0.0, 0.0)] * identity_pad:
        coeffs += list(s) * 2  # left section, then the same for right
    states = 10
    efx = [
        _apo_block("VolumeControl", fs, Enabled=[True], GainDb=[-128.0, -128.0]),
        _apo_block("MainEQ", fs, Enabled=[True], Coefficients=coeffs),
        # Identity at state 0 only, as shipped: lower states boost.
        _apo_block("VolumeDepLS", fs, Enabled=[True],
                   Coefficients=[1.0, 0.0, 0.0, 0.0, 0.0] * 2
                   + [1.1, 0.0, 0.0, 0.0, 0.0] * 2 * (states - 1)),
        _apo_block("VolumeDepMBDRC4", fs, Enabled=[True],
                   CrossoverFreqs=[120.0, 800.0, 3000.0],
                   ThresholdDb=[-10.0, -8.0, 0.0, 0.0] * states,
                   Ratio=[3.0, 2.0, 1.0, 1.0] * states,
                   PreGainDb=[2.0, 0.0, 0.0, 0.0] * states,
                   OutputLimit=[0.0] * 4 * states,
                   AttackTimeMs=[2.0, 2.0, 2.0, 2.0],
                   HoldTimeMs=[50.0, 50.0, 50.0, 50.0],
                   ReleaseTimeMs=[30.0, 30.0, 30.0, 30.0],
                   LookaheadTimeMs=[0.0] * 4),
        _apo_block("Crystal", fs, Enabled=[True], F0=[200.0, 500.0],
                   Bandwidth=[100.0, 200.0], Limit=[-10.0, -20.0],
                   AttackTimeMs=[1.0, 1.0], HoldTimeMs=[0.0, 0.0],
                   ReleaseTimeMs=[40.0, 40.0]),
        _apo_block("OutputLimiter", fs, Enabled=[True], ThresholdDb=[0.0],
                   LookaheadTimeMs=[5.0]),
    ]
    return {"metadata": {"name": "synthetic", "version": "0.0"},
            "entities": [
                {"type": "complex", "id": 1, "name": "InitialValueStore",
                 "children": [{"type": "complex", "id": 2, "name": "R/EFX",
                               "children": efx}]},
                {"type": "complex", "id": 4,
                 "name": "DependentParameterStore", "children": []}]}


def surface_apo_inf(subsys="10EC1284", dev="0274", config="SurfaceAPO_TEST.json",
                    driver_ver="1.0.0.0", hwid=None, via_interfaces=False):
    """A SurfaceAPOExtension.inf binding *config* to DEV/SUBSYS, or to
    *hwid* when given.

    *via_interfaces* sets the config through an AddInterface section, as
    the Surface Pro 11 and 12 packages do, instead of the install section.
    """
    hwid = hwid or f"HDAUDIO\\FUNC_01&VEN_10EC&DEV_{dev}&SUBSYS_{subsys}"
    install = ("""[Install_Test.Interfaces]
AddInterface=%KSCATEGORY_RENDER%, %KSNAME_Speaker%, Iface_Speaker

[Install_Test]
CopyFiles = Config_CopyFiles

[Iface_Speaker]
AddReg = ApoAddReg_Test""" if via_interfaces else """[Install_Test.NT]
AddReg = PresetAddReg, ApoAddReg_Test""")
    return f"""\
; synthetic
[Version]
Signature   = "$WINDOWS NT$"
Class       = Extension
DriverVer = 01/01/2026,{driver_ver}

[SourceDisksFiles]
{config} = 1

[Manufacturer]
%MfgName% = DeviceExtensions,NTamd64

[DeviceExtensions.NTamd64]
%Desc% = Install_Test, {hwid}

{install}

[PresetAddReg]
HKR,InterfaceSetting,PrimaryLineOutTopo,%REG_MULTI_SZ%,"ApoPreset1"

[ApoAddReg_Test]
HKR,InterfaceSetting\\ApoPreset1\\FX\\0,%PKEY_SurfaceApoConfigFilename%,%REG_SZ%,%13%\\{config} ; the binding

[Strings]
MfgName = "Surface"
Desc = "Microsoft Surface APO"
PKEY_SurfaceApoConfigFilename = "{{c1f75c4c-3243-11ea-850d-2e728ce88125}},0"
REG_SZ = 0x00000000
REG_MULTI_SZ = 0x00010000
KSCATEGORY_RENDER = "{{65E8773E-8F56-11D0-A3B9-00A0C9223196}}"
KSNAME_Speaker = "Speaker0"
"""


def write_surface_package(root: Path, *, layout="msi", inf_subsys="10EC1284",
                          dev="0274", config_json=None, utf16=False,
                          driver_ver="1.0.0.0", config="SurfaceAPO_TEST.json",
                          package_dir=None, xml_name=None,
                          hwid=None, via_interfaces=False) -> Path:
    """Write a DAX3 XML beside a Surface APO package; return the XML path.

    The XML tunes SUBSYS 10EC1284 unless *xml_name* names another;
    *inf_subsys*, or a whole *hwid*, is the device the package's .inf binds,
    so a different value models a sibling package for another model.

    `layout="msi"` mirrors an extracted MSI (`SurfaceUpdate/dax3extrtk/` +
    `SurfaceUpdate/surfaceapoextension/`). `"driverstore"` mirrors
    `FileRepository/<inf>.inf_amd64_<hash>/` wrappers.
    """
    import json
    if layout == "msi":
        xml_dir, apo_dir = root / "dax3extrtk", root / "surfaceapoextension"
    else:
        xml_dir = root / "dax3_ext_rtk.inf_amd64_0123456789abcdef"
        apo_dir = root / (package_dir or
                          "surfaceapoextension.inf_amd64_fedcba9876543210")
    if package_dir and layout == "msi":
        apo_dir = root / package_dir
    xml_dir.mkdir(parents=True, exist_ok=True)
    apo_dir.mkdir(parents=True, exist_ok=True)
    xml = xml_dir / (xml_name or
                     f"DEV_{dev}_SUBSYS_10EC1284_PCI_SUBSYS_72708086.xml")
    xml.write_text("<device_data/>")
    inf_text = surface_apo_inf(subsys=inf_subsys, dev=dev, config=config,
                               driver_ver=driver_ver, hwid=hwid,
                               via_interfaces=via_interfaces)
    inf = apo_dir / "SurfaceAPOExtension.inf"
    if utf16:
        inf.write_bytes(inf_text.encode("utf-16"))
    else:
        inf.write_text(inf_text)
    (apo_dir / config).write_text(
        json.dumps(config_json if config_json is not None
                   else surface_apo_json()), encoding="utf-8")
    return xml


def synthetic_apo_layer(default_on=False):
    """An `ApoLayer` shaped like the one `surface_apo_json` parses into,
    built directly so preset tests need no package on disk."""
    from lib.apo.layer import ApoLayer, BandDynamics, DynBand
    eq = tuple(synthetic_surface_eq())
    drc = BandDynamics(
        name="drc", crossovers_hz=(120.0, 800.0), detection="RMS",
        knee_db=0.0, bands=(
            DynBand(True, -10.0, 3.0, 2.0, 30.0, pregain_db=2.0),
            DynBand(True, -8.0, 2.0, 2.0, 30.0),
            # Parked above full scale, as the shipped file parks its unused
            # bands: the builder must keep it inside LSP's port range.
            DynBand(False, 48.0, 1.0, 2.0, 30.0)))
    crystal = BandDynamics(
        name="crystal", crossovers_hz=(150.0, 316.2, 600.0), detection="Peak",
        knee_db=0.0, bands=(
            DynBand(False, 0.0, 1.0, 1.0, 40.0),
            DynBand(True, -10.0, 100.0, 1.0, 40.0,
                    sidechain_hz=(150.0, 250.0)),
            DynBand(True, -20.0, 100.0, 1.0, 40.0,
                    sidechain_hz=(400.0, 600.0)),
            DynBand(False, 0.0, 1.0, 1.0, 40.0)))
    return ApoLayer(label="Synthetic APO",
                    config_path=Path("/synthetic/SurfaceAPO_TEST.json"),
                    inf_path=Path("/synthetic/SurfaceAPOExtension.inf"),
                    hardware_id="HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274"
                                "&SUBSYS_10EC1284",
                    sample_rate=48000, eq_left=eq, eq_right=eq,
                    dynamics=(drc, crystal),
                    notes=("VolumeDepLS: volume-dependent shelf, not "
                           "reproduced",),
                    default_on=default_on)
