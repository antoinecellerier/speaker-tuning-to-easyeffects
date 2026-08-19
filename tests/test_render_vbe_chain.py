"""The issue-#14 virtual-bass chain generator (tools/measure_ee/).

The argv lock-down keeps the lv2info-verified port symbols and the
PoC-faithful defaults (BWC BT, level_out=1.0 — the values that reproduce
the 2026-05-06 renders bit-for-bit) from drifting under a refactor. The
smoke render runs only where the LV2 plugins are installed.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from tools.measure_ee.render_vbe_chain import (
    FILTER_URI, SATURATOR_URI, build_parser, stage_commands,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
STIMULUS = Path("/x/stim.wav")
OUT_DIR = Path("/x/out")


def default_commands():
    args = build_parser().parse_args([])
    return stage_commands(args, STIMULUS, OUT_DIR)


def test_stage_commands_locks_ports_and_defaults():
    lsp = lambda ft, s, f: ["-c", "enabled", "1", "-c", "mode", "0",
                            "-c", "ft", ft, "-c", "fm", "2",
                            "-c", "s", s, "-c", "f", f]
    expected = [
        ("hp1", lsp("1", "7", "35.0"), FILTER_URI),
        ("bp", lsp("0", "7", "160.0"), FILTER_URI),
        ("sat", ["-c", "bypass", "0", "-c", "level_in", "1.0",
                 "-c", "level_out", "1.0", "-c", "mix", "1.0",
                 "-c", "drive", "4.0", "-c", "blend", "0.0",
                 "-c", "pre", "0", "-c", "post", "0"], SATURATOR_URI),
        ("hp2", lsp("1", "5", "180.0"), FILTER_URI),
        ("final", lsp("0", "5", "800.0"), FILTER_URI),
    ]
    commands = default_commands()
    assert [name for name, _, _ in commands] == \
        [name for name, _, _ in expected]
    src = STIMULUS
    for (name, out, argv), (_, controls, uri) in zip(commands, expected):
        assert out == OUT_DIR / f"vbe_{name}.wav"
        assert argv == ["lv2apply", "-i", str(src), "-o", str(out)] \
            + controls + [uri]
        src = out


def test_stages_chain_outputs():
    commands = default_commands()
    for prev, cur in zip(commands, commands[1:]):
        assert cur[2][2] == str(prev[1])  # each -i is the previous -o


def _plugins_missing() -> bool:
    if shutil.which("lv2apply") is None or shutil.which("lv2ls") is None:
        return True
    uris = subprocess.run(["lv2ls"], capture_output=True, text=True).stdout
    return FILTER_URI not in uris or SATURATOR_URI not in uris


@pytest.mark.skipif(_plugins_missing(),
                    reason="lv2apply or the LV2 plugins not installed")
def test_smoke_render(tmp_path):
    from scipy.io import wavfile
    sr = 48000
    t = np.arange(sr) / sr
    tone = (0.5 * np.sin(2 * np.pi * 120.0 * t)).astype(np.float32)
    stimulus = tmp_path / "burst.wav"
    wavfile.write(stimulus, sr, np.column_stack([tone, tone]))

    proc = subprocess.run(
        [sys.executable,
         str(REPO_ROOT / "tools/measure_ee/render_vbe_chain.py"),
         "--stimulus", str(stimulus), "--out-dir", str(tmp_path)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    sys.path.insert(0, str(REPO_ROOT / "tools"))
    from _wavio import read
    _, final = read(str(tmp_path / "vbe_final.wav"))
    mono = final[:, 0].astype(np.float64)
    assert np.sqrt((mono ** 2).mean()) > 1e-4  # non-silent

    spec = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
    freqs = np.fft.rfftfreq(len(mono), 1 / sr)
    level = lambda f: spec[(freqs > f - 5) & (freqs < f + 5)].max()
    # In-band 120 Hz drives the saturator; the post-band HP@180 then kills
    # the fundamental while the 3rd harmonic at 360 Hz passes.
    assert 20 * np.log10(level(360.0) / level(120.0)) > 20
