#!/usr/bin/env python3
"""Smoke harness for an EasyEffects capture route.

Run this against any candidate PipeWire node ("--target") with the
"Nothing" bypass preset loaded in EE. PASS means the captured signal
is a clean delayed copy of the smoke stimulus (i.e., the route is
post-processing and the bypass really bypasses).

Usage:
    python tools/measure_ee/smoke.py --target ee_capture.monitor
    python tools/measure_ee/smoke.py --target ee_capture.monitor --keep-tmp

Exit code 0 = PASS, 1 = FAIL, 2 = setup error.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, fftconvolve

SR = 48000
SMOKE_DURATION = 1.5
CAPTURE_DURATION = 2.5
SWEEP_F0 = 50.0
SWEEP_F1 = 20000.0
SWEEP_T = 1.0
PRE_SILENCE = 0.1
TAIL_SILENCE = 0.3

# Delay = pw-cat startup + 100 ms stimulus pre-silence + EE processing
# latency, all of which we don't care about for *route validity* — we
# only fail if delay is so large it implies we caught the wrong stream.
PASS_DELAY_MAX_MS = 1000.0
PASS_GAIN_MAX_DB = 0.5
PASS_FLATNESS_MAX_DB = 0.5
PASS_RESIDUAL_MAX_DB = -35.0


def make_smoke_stimulus() -> tuple[np.ndarray, np.ndarray]:
    """Returns (stereo_signal, mono_signal). Stereo is L=R."""
    n_pre = int(round(PRE_SILENCE * SR))
    n_sweep = int(round(SWEEP_T * SR))
    n_tail = int(round(TAIL_SILENCE * SR))

    pre = np.zeros(n_pre, dtype=np.float32)
    pre[-1] = 0.5  # dirac immediately before the sweep starts

    t = np.arange(n_sweep) / SR
    L = SWEEP_T / np.log(SWEEP_F1 / SWEEP_F0)
    K = 2.0 * np.pi * SWEEP_F0 * L
    sweep = np.sin(K * (np.exp(t / L) - 1.0)).astype(np.float32)
    fade = int(round(0.005 * SR))
    sweep[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    sweep[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    sweep *= 10 ** (-18 / 20)  # -18 dBFS peak

    tail = np.zeros(n_tail, dtype=np.float32)
    mono = np.concatenate([pre, sweep, tail])
    stereo = np.column_stack([mono, mono])
    return stereo, mono


def write_wav_f32(path: Path, signal: np.ndarray, sr: int = SR) -> None:
    sig = np.clip(signal, -1.0, 1.0).astype(np.float32)
    wavfile.write(str(path), sr, sig)


def read_wav_f32(path: Path) -> tuple[int, np.ndarray]:
    sr, x = wavfile.read(str(path))
    if x.dtype == np.int16:
        x = x.astype(np.float32) / 32768.0
    elif x.dtype == np.int32:
        x = x.astype(np.float32) / 2147483648.0
    elif x.dtype == np.float32:
        pass
    else:
        x = x.astype(np.float32)
    return sr, x


def _split_target(target: str) -> tuple[str, list[str]]:
    """Split "node[.port_or_monitor]" target into (node_name, [port_FL, port_FR]).

    "ee_capture.monitor" -> ("ee_capture", ["monitor_FL", "monitor_FR"])
    "ee_capture"         -> ("ee_capture", ["monitor_FL", "monitor_FR"])  # sink default
    """
    if "." in target:
        node, suffix = target.rsplit(".", 1)
        if suffix in ("monitor", "output"):
            return node, [f"{suffix}_FL", f"{suffix}_FR"]
    # default: sink monitor
    return target, ["monitor_FL", "monitor_FR"]


def play_and_capture(
    stim_path: Path, target: str, capture_path: Path,
    play_target: str = "easyeffects_sink", verbose: bool = False,
    play_timeout_s: float | None = None,
) -> None:
    """Capture `target` while playing stimulus into `play_target`.

    Critical: pw-record's `--target` is just a hint that WirePlumber's
    policy can override (it'll happily reroute your "capture from
    null-sink monitor" to the system default mic). To actually bind to
    the requested node, we start pw-record with `--target 0` (no
    auto-link) then create the links by hand with pw-link. This is the
    only reliable way; -P node.target=, target.object=, etc. all get
    overridden by the policy.
    """
    src_node, src_ports = _split_target(target)

    rec_cmd = [
        "pw-record",
        "--target", "0",
        "--rate", str(SR),
        "--channels", "2",
        "--format", "f32",
        "--latency", "20ms",
        "-P", "node.name=ee_smoke_recorder",
        str(capture_path),
    ]
    play_cmd = [
        "pw-cat",
        "--playback",
        "--target", play_target,
        "--rate", str(SR),
        "--channels", "2",
        "--format", "f32",
        "--latency", "20ms",
        str(stim_path),
    ]
    if verbose:
        print("rec:", " ".join(rec_cmd))
        print("play:", " ".join(play_cmd))

    rec = subprocess.Popen(rec_cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    # Wait for pw-record to register its node so pw-link can find it.
    # pw-link -i lists each port on its own line; match the exact prefix
    # "<node>:input_FL" rather than the bare node name (which would also
    # match e.g. "ee_smoke_recorder_old").
    rec_node = "ee_smoke_recorder"
    expected_port = f"{rec_node}:input_FL"
    deadline = time.monotonic() + 5.0
    registered = False
    while time.monotonic() < deadline:
        probe = subprocess.run(
            ["pw-link", "-i"], capture_output=True, text=True,
        )
        if any(line.strip() == expected_port
               for line in probe.stdout.splitlines()):
            registered = True
            break
        time.sleep(0.05)
    if not registered:
        rec.terminate()
        try:
            rec.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            rec.kill()
        raise RuntimeError(
            f"pw-record node {rec_node!r} did not register within 5 s — "
            f"capture cannot be linked to {src_node}:{src_ports[0]}"
        )
    # Manually link source ports -> recorder input ports. A failed link
    # means audio won't reach the recorder; surface immediately rather
    # than producing a silent capture that fails the smoke gate later.
    src_chans = ["FL", "FR"]
    for src_port, ch in zip(src_ports, src_chans):
        link_rc = subprocess.run(
            ["pw-link", f"{src_node}:{src_port}", f"{rec_node}:input_{ch}"],
            capture_output=True, text=True,
        )
        if link_rc.returncode != 0:
            rec.terminate()
            try:
                rec.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                rec.kill()
            raise RuntimeError(
                f"pw-link {src_node}:{src_port} -> "
                f"{rec_node}:input_{ch} failed: {link_rc.stderr.strip()}"
            )
    # Brief settle, then play the stimulus
    time.sleep(0.2)
    if play_timeout_s is None:
        # Derive from the stimulus length so 12 s stationary stimuli don't get
        # cut off. Add 5 s slack for pw-cat startup + tail flush.
        try:
            stim_sr, stim_data = wavfile.read(str(stim_path))
            play_timeout_s = stim_data.shape[0] / stim_sr + 5.0
        except Exception:
            play_timeout_s = 60.0
    play = subprocess.Popen(play_cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        play.wait(timeout=play_timeout_s)
    except subprocess.TimeoutExpired:
        play.kill()
        print(f"WARN: pw-cat exceeded {play_timeout_s:.1f}s timeout — "
              f"capture truncated", file=sys.stderr)
    time.sleep(0.3)  # tail
    rec.terminate()
    try:
        rec.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        rec.kill()


def estimate_delay(captured: np.ndarray, stimulus: np.ndarray) -> int:
    """Cross-correlation lag (samples) of captured vs stimulus."""
    # Use the dirac region for a clean lag estimate
    n = min(len(captured), len(stimulus))
    cap = captured[:n]
    stm = stimulus[:n]
    cap = cap - cap.mean()
    stm = stm - stm.mean()
    xc = correlate(cap, stm, mode="full")
    lag = np.argmax(np.abs(xc)) - (n - 1)
    return int(lag)


def magnitude_response(
    captured: np.ndarray, stimulus: np.ndarray, sr: int, lag: int
) -> tuple[np.ndarray, np.ndarray]:
    """Estimate H(f) = FFT(capture) / FFT(stimulus) on the sweep region.

    Returns (freqs Hz, |H| dB)."""
    n_pre = int(round(PRE_SILENCE * sr))
    n_sweep = int(round(SWEEP_T * sr))
    s0 = max(0, n_pre + lag)
    s1 = s0 + n_sweep
    stim_seg = stimulus[n_pre:n_pre + n_sweep]
    cap_seg = captured[s0:s1]
    if len(cap_seg) < n_sweep:
        cap_seg = np.pad(cap_seg, (0, n_sweep - len(cap_seg)))
    win = np.hanning(n_sweep)
    fft_stim = np.fft.rfft(stim_seg * win)
    fft_cap = np.fft.rfft(cap_seg * win)
    eps = 1e-9
    H = fft_cap / (fft_stim + eps)
    freqs = np.fft.rfftfreq(n_sweep, 1.0 / sr)
    mag_db = 20 * np.log10(np.abs(H) + eps)
    return freqs, mag_db


def evaluate(captured_stereo: np.ndarray, stimulus_mono: np.ndarray) -> dict:
    """Run the four metrics on the captured signal (channel 0)."""
    if captured_stereo.ndim == 1:
        cap = captured_stereo
    else:
        cap = captured_stereo[:, 0]
    stim = stimulus_mono

    if len(cap) == 0:
        return {"ok": False, "reason": "capture is empty"}

    lag = estimate_delay(cap, stim)
    if abs(lag) > len(cap):
        return {"ok": False, "reason": f"unreasonable lag {lag}"}

    n_pre = int(round(PRE_SILENCE * SR))
    n_sweep = int(round(SWEEP_T * SR))
    s0 = max(0, n_pre + lag)
    s1 = s0 + n_sweep

    cap_seg = cap[s0:s1]
    stim_seg = stim[n_pre:n_pre + n_sweep]
    if len(cap_seg) < n_sweep // 2:
        return {"ok": False, "reason": "capture too short"}
    if len(cap_seg) < n_sweep:
        cap_seg = np.pad(cap_seg, (0, n_sweep - len(cap_seg)))

    peak_cap = float(np.max(np.abs(cap_seg)))
    peak_stim = float(np.max(np.abs(stim_seg)))
    eps = 1e-12
    peak_gain_db = 20 * np.log10((peak_cap + eps) / (peak_stim + eps))

    # Magnitude flatness in 200 Hz – 18 kHz
    freqs, mag_db = magnitude_response(cap, stim, SR, lag)
    band = (freqs >= 200) & (freqs <= 18000)
    if band.sum() < 32:
        return {"ok": False, "reason": "no usable band data"}
    mag_band = mag_db[band]
    flatness_db = float(mag_band.max() - mag_band.min())

    # Residual: capture - delayed stim
    cap_aligned = cap_seg.copy()
    stim_scaled = stim_seg * (peak_cap / (peak_stim + eps))
    residual = cap_aligned - stim_scaled
    rms_resid = float(np.sqrt(np.mean(residual**2)) + eps)
    rms_stim = float(np.sqrt(np.mean(stim_seg**2)) + eps)
    residual_db = 20 * np.log10(rms_resid / rms_stim)

    delay_ms = (lag / SR) * 1000.0
    pass_delay = abs(delay_ms) < PASS_DELAY_MAX_MS
    pass_gain = abs(peak_gain_db) < PASS_GAIN_MAX_DB
    pass_flat = flatness_db < PASS_FLATNESS_MAX_DB
    pass_resid = residual_db < PASS_RESIDUAL_MAX_DB

    return {
        "ok": all([pass_delay, pass_gain, pass_flat, pass_resid]),
        "delay_ms": delay_ms,
        "peak_gain_db": peak_gain_db,
        "flatness_db": flatness_db,
        "residual_db": residual_db,
        "lag_samples": lag,
        "freqs": freqs,
        "mag_db": mag_db,
        "pass_delay": pass_delay,
        "pass_gain": pass_gain,
        "pass_flat": pass_flat,
        "pass_resid": pass_resid,
    }


def maybe_save_diagnostic_plot(
    res: dict, capture_path: Path, route_label: str
) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    out = capture_path.with_suffix(".png")
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.semilogx(res["freqs"], res["mag_db"], label="|H(f)| capture / stimulus")
    ax.axhline(0, ls="--", lw=0.5, color="k")
    ax.axvspan(200, 18000, color="0.9", alpha=0.5)
    ax.set_xlim(20, 24000)
    ymin = min(-3, res["mag_db"][(res["freqs"] >= 200) & (res["freqs"] <= 18000)].min() - 1)
    ymax = max(3, res["mag_db"][(res["freqs"] >= 200) & (res["freqs"] <= 18000)].max() + 1)
    ax.set_ylim(ymin, ymax)
    ax.set_xlabel("Hz")
    ax.set_ylabel("dB")
    ax.set_title(
        f"{route_label}: delay {res['delay_ms']:.1f} ms, "
        f"gain {res['peak_gain_db']:+.2f} dB, flatness {res['flatness_db']:.2f} dB, "
        f"residual {res['residual_db']:.1f} dB"
    )
    ax.grid(True, which="both", alpha=0.3)
    fig.tight_layout()
    fig.savefig(str(out), dpi=110)
    plt.close(fig)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True,
                    help="PipeWire capture node (e.g. ee_capture.monitor)")
    ap.add_argument("--play-target", default="easyeffects_sink",
                    help="PipeWire playback target (default easyeffects_sink)")
    ap.add_argument("--preset", default="Nothing",
                    help='EE preset to load before measuring (default "Nothing")')
    ap.add_argument("--no-load-preset", action="store_true",
                    help="Don't auto-switch preset (assume caller did)")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="Keep stimulus + capture wavs for inspection")
    ap.add_argument("--out-dir", type=Path,
                    help="Directory to save capture + plot (default tmp dir)")
    ap.add_argument("--label", default="route",
                    help="Label included in output filenames + plot title")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if shutil.which("pw-cat") is None or shutil.which("pw-record") is None:
        print("ERROR: pw-cat / pw-record not in PATH", file=sys.stderr)
        return 2

    if not args.no_load_preset:
        if shutil.which("easyeffects") is None:
            print("ERROR: easyeffects not in PATH", file=sys.stderr)
            return 2
        rc = subprocess.run(
            ["easyeffects", "-l", args.preset], capture_output=True, text=True
        )
        if rc.returncode != 0:
            print(f"ERROR: easyeffects -l {args.preset!r} failed: {rc.stderr}",
                  file=sys.stderr)
            return 2
        if args.verbose:
            print(f"loaded preset {args.preset!r}")
        time.sleep(0.5)

    out_dir = args.out_dir or Path(tempfile.mkdtemp(prefix="ee_smoke_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stim_path = out_dir / f"smoke_stimulus_{args.label}.wav"
    cap_path = out_dir / f"smoke_capture_{args.label}.wav"

    stereo, mono = make_smoke_stimulus()
    write_wav_f32(stim_path, stereo)
    if args.verbose:
        print(f"wrote {stim_path}")

    play_and_capture(stim_path, args.target, cap_path,
                     play_target=args.play_target, verbose=args.verbose)

    if not cap_path.exists() or cap_path.stat().st_size == 0:
        print(f"FAIL [{args.label}]: capture file empty: {cap_path}",
              file=sys.stderr)
        return 1

    sr, captured = read_wav_f32(cap_path)
    if sr != SR:
        print(f"WARN: capture sr={sr} != expected {SR}", file=sys.stderr)
    res = evaluate(captured, mono)

    if not res.get("ok") and "reason" in res:
        print(f"FAIL [{args.label}]: {res['reason']}", file=sys.stderr)
        return 1

    print(json.dumps({
        k: v for k, v in res.items()
        if k not in ("freqs", "mag_db") and isinstance(v, (bool, int, float, str))
    }, indent=2, default=str))

    plot = maybe_save_diagnostic_plot(res, cap_path, args.label)
    if plot is not None:
        print(f"plot: {plot}", file=sys.stderr)

    verdict = "PASS" if res["ok"] else "FAIL"
    print(f"{verdict} [{args.label}] target={args.target}", file=sys.stderr)

    if args.keep_tmp:
        print(f"kept: stim={stim_path} cap={cap_path}", file=sys.stderr)

    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
