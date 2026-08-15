#!/usr/bin/env python3
"""Where does a filter-chain sink's own volume land — before or after the DSP?

Issue #63 asked what it actually costs to select the chain sink as the system
output rather than leaving the speaker selected. The obvious half is arithmetic:
two sinks in series carry two volume controls and the levels multiply. The half
that needs measuring is *where* the chain's control sits. A `module-filter-chain`
sink is a software-volume node, so if PipeWire applies that volume on the way
*into* the node, it lands ahead of the MBC / regulator / limiter — whose
thresholds are absolute dBFS — and turning the chain down then changes which
parts of the tuning engage, not just how loud the result is.

Method — three captures of the same loud stimulus off the *speaker sink's
monitor*, which taps the mix after the chain has run and before the hardware
mixer attenuates it:

  A  chain at unity, speaker untouched          reference
  B  chain turned down, speaker untouched       the question
  C  chain at unity, speaker turned down        control: the tap
  D  chain at unity, stimulus pre-scaled by     control: the discrimination
     the same factor in the file itself

Then:

  B vs A  fitted to a single gain. A clean residual means the volume is applied
          *after* the graph and only the stacking matters. A residual that no
          single gain explains means it is applied *before* the dynamics.
  C vs A  must be identical. The speaker's control is a hardware mixer element
          (`pactl list sinks` reports HW_VOLUME_CTRL), so it cannot show up in a
          monitor capture at all. If it does, the monitor is not the tap this
          method assumes and B's reading means nothing.
  D vs A  must NOT be a single gain. Leg D attenuates inside the content, so it
          reaches the graph quieter by construction; if its residual is clean
          too, the chain is simply operating linearly at this level and the
          method cannot tell the two hypotheses apart. Without this leg a
          dormant compressor reads exactly like "the volume is applied after the
          DSP" — and this repo has measured that dormancy before (design-notes:
          the MBC does not engage on -10 dBFS stimuli). Feed it a stimulus loud
          enough to put the dynamics to work, and check D before trusting B.

Reroutes nothing, but it plays audio and moves volume controls: run it through
the /audio-validate handoff, not ad hoc. Captures stay untracked under
--out-dir; only this harness is committed.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools" / "measure_ee"))

sys.path.insert(0, str(REPO / "tools"))

import smoke as _smoke  # noqa: E402
from _wavio import read as wav_read  # noqa: E402

DEFAULT_OUT_DIR = REPO / "localresearch" / "measure_pw" / "volume_stage"
SR = 48000

# The reading is only meaningful on a stimulus loud enough to put the chain's
# dynamics stages to work: the MBC and limiter thresholds are absolute dBFS, so
# a quiet stimulus passes both hypotheses identically. That is exactly why the
# quiet stimulus is worth running too — as a negative control (--stimulus).
DEFAULT_STIMULUS = REPO / "localresearch" / "measure_ee" / "stimulus_pink.wav"


def _node(node_name: str) -> dict:
    """The pw-dump object for a node.name, or exit saying it isn't there."""
    dump = json.loads(subprocess.run(["pw-dump"], capture_output=True,
                                     text=True, timeout=10).stdout)
    for obj in dump:
        if obj.get("info", {}).get("props", {}).get("node.name") == node_name:
            return obj
    raise SystemExit(f"no node named {node_name!r} — is it loaded?")


def _channel_volumes(node_name: str) -> list[float]:
    """The linear channelVolumes PipeWire actually applied to a node.

    Read back rather than computed from what we asked for: both `pactl`'s
    percentages and `wpctl`'s 0..1 argument go through a cubic mapping on the
    way in, so "half" is about -18 dB, not -6. Every scaling in this harness
    uses the number that came back, which makes the mapping irrelevant.
    """
    for p in _node(node_name).get("info", {}).get("params", {}).get("Props", []):
        vols = p.get("channelVolumes")
        if vols:
            return [float(v) for v in vols]
    raise SystemExit(f"no channelVolumes on node {node_name!r}")


def _set_volume(node_name: str, linear: float) -> list[float]:
    """Set a node's volume *by linear amplitude* and return what landed.

    Two traps, both of which bit this harness:

    `wpctl` addresses nodes by object id, not by name, so the name is resolved
    through the same dump the read-back uses — one source of truth for both.

    And `wpctl set-volume` takes a PulseAudio-style value, which PipeWire cubes
    on the way to `channelVolumes`: asking for 0.5 lands 0.125. Feeding a linear
    amplitude straight in therefore cubes it — and feeding a *read-back* value
    straight back in, as a restore path naturally does, cubes it again on every
    round trip. Two restores took a speaker from 0.064 to silence. Convert here,
    once, so callers only ever speak in linear amplitude.
    """
    node_id = _node(node_name)["id"]
    subprocess.run(["wpctl", "set-volume", str(node_id),
                    f"{max(linear, 0.0) ** (1.0 / 3.0):.6f}"],
                   check=False, capture_output=True, text=True)
    return _channel_volumes(node_name)


def _align(ref: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Shift `other` onto `ref` by the integer lag that maximises correlation.

    Two separate playbacks never start on the same sample; without this the
    residual measures the offset rather than the processing.
    """
    n = min(len(ref), len(other))
    ref, other = ref[:n], other[:n]
    # FFT correlation, not np.correlate(mode="full"): that one is a direct O(n²)
    # convolution, and a 13 s capture at 48 kHz is 6e5 samples — minutes per leg.
    corr = signal.correlate(ref - ref.mean(), other - other.mean(),
                            mode="full", method="fft")
    lag = int(np.argmax(corr)) - (n - 1)
    if lag > 0:
        other = np.concatenate([np.zeros(lag), other])[:n]
    elif lag < 0:
        other = other[-lag:]
        other = np.concatenate([other, np.zeros(n - len(other))])
    return other


def _residual_db(ref: np.ndarray, test: np.ndarray) -> tuple[float, float]:
    """(signal-to-residual dB, best-fit gain) between two aligned captures.

    The gain is fitted rather than assumed so the verdict does not hinge on the
    volume read-back being exact: what matters is whether *any* single scalar
    explains the difference. One that does means the volume acted as a plain
    gain outside the graph; one that does not means the graph saw a different
    signal.
    """
    test = _align(ref, test)
    n = min(len(ref), len(test))
    ref, test = ref[:n], test[:n]
    denom = float(np.dot(test, test))
    gain = float(np.dot(ref, test) / denom) if denom else float("nan")
    resid = ref - gain * test
    rms_ref = float(np.sqrt(np.mean(ref ** 2)))
    rms_res = float(np.sqrt(np.mean(resid ** 2)))
    sr_db = 20 * np.log10(rms_ref / rms_res) if rms_res > 0 else float("inf")
    return sr_db, gain


def _capture(stim: Path, chain_node: str, monitor: str, dest: Path) -> None:
    _smoke.play_and_capture(stim, monitor, dest, play_target=chain_node)


def _write_scaled(src: Path, dest: Path, gain: float) -> None:
    """Copy a stimulus with every sample multiplied by `gain`.

    Kept in the stimulus's own dtype so the only difference between legs A and D
    is amplitude — a format change would show up in the residual as if it were
    processing.
    """
    sr, data = wav_read(src)
    arr = np.asarray(data)
    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        scaled = np.clip(np.rint(arr.astype(np.float64) * gain),
                         info.min, info.max).astype(arr.dtype)
    else:
        scaled = (arr.astype(np.float64) * gain).astype(arr.dtype)
    wavfile.write(dest, sr, scaled)


def _mono(path: Path) -> np.ndarray:
    # scipy's (rate, data) order, which `_wavio.read` is a drop-in for.
    sr, data = wav_read(path)
    if sr != SR:
        raise SystemExit(f"{path.name}: {sr} Hz, expected {SR}")
    arr = np.asarray(data, dtype=np.float64)
    return arr.mean(axis=1) if arr.ndim > 1 else arr


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain-node", required=True,
                    help="the chain's capture sink, e.g. effect_input.Dolby_Balanced")
    ap.add_argument("--monitor", required=True,
                    help="sink monitor to capture, e.g. alsa_output.<...>__sink.monitor")
    ap.add_argument("--speaker-node", required=True,
                    help="hardware sink node.name (control leg C)")
    ap.add_argument("--stimulus", type=Path, default=DEFAULT_STIMULUS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--test-volume", type=float, default=0.125,
                    help="linear amplitude for legs B and C (default 0.125)")
    ap.add_argument("--analyze-only", action="store_true",
                    help="re-read the captures already in --out-dir instead of "
                         "playing anything (no audio, no volume changes)")
    args = ap.parse_args()

    if args.analyze_only:
        caps = {k: args.out_dir / n for k, n in
                (("A", "cap_a_chain_unity.wav"), ("B", "cap_b_chain_half.wav"),
                 ("C", "cap_c_speaker_half.wav"), ("D", "cap_d_content_scaled.wav"))}
        missing = [p.name for p in caps.values() if not p.is_file()]
        if missing:
            raise SystemExit(f"--analyze-only: no captures for {missing}")
        return _report(caps)

    if not args.stimulus.is_file():
        raise SystemExit(f"stimulus not found: {args.stimulus}")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    chain_was = _channel_volumes(args.chain_node)
    speaker_was = _channel_volumes(args.speaker_node)
    print(f"chain   {args.chain_node}: {chain_was}")
    print(f"speaker {args.speaker_node}: {speaker_was}")

    caps = {}
    try:
        _set_volume(args.chain_node, 1.0)
        _set_volume(args.speaker_node, speaker_was[0])
        caps["A"] = args.out_dir / "cap_a_chain_unity.wav"
        _capture(args.stimulus, args.chain_node, args.monitor, caps["A"])

        applied = _set_volume(args.chain_node, args.test_volume)
        print(f"leg B: chain volume applied = {applied}")
        caps["B"] = args.out_dir / "cap_b_chain_half.wav"
        _capture(args.stimulus, args.chain_node, args.monitor, caps["B"])

        _set_volume(args.chain_node, 1.0)
        spk_applied = _set_volume(args.speaker_node, args.test_volume)
        print(f"leg C: speaker volume applied = {spk_applied}")
        caps["C"] = args.out_dir / "cap_c_speaker_half.wav"
        _capture(args.stimulus, args.chain_node, args.monitor, caps["C"])

        _set_volume(args.speaker_node, speaker_was[0])
        # Attenuate by the same factor leg B ended up with, so D and B differ
        # only in *where* the attenuation happened.
        scaled = args.out_dir / "stim_prescaled.wav"
        _write_scaled(args.stimulus, scaled, applied[0])
        print(f"leg D: stimulus pre-scaled by {applied[0]:.6f} in the file")
        caps["D"] = args.out_dir / "cap_d_content_scaled.wav"
        _capture(scaled, args.chain_node, args.monitor, caps["D"])
    finally:
        _set_volume(args.chain_node, chain_was[0])
        _set_volume(args.speaker_node, speaker_was[0])
        print("volumes restored")

    return _report(caps)


def _report(caps: dict) -> int:
    a, b, c, d = (_mono(caps[k]) for k in "ABCD")
    sr_b, gain_b = _residual_db(a, b)
    sr_c, gain_c = _residual_db(a, c)
    sr_d, gain_d = _residual_db(a, d)

    for label, sr_db, gain in (("B (chain volume)", sr_b, gain_b),
                               ("C (speaker volume)", sr_c, gain_c),
                               ("D (content scaled)", sr_d, gain_d)):
        print(f"\n{label:<20} vs A: S/R {sr_db:7.1f} dB   best-fit gain "
              f"{gain:.4f} ({20 * np.log10(abs(gain)):+.2f} dB)")

    print("\nVerdict")
    if sr_c < 30:
        print("  INVALID — the control leg moved. The speaker's volume is "
              "visible in this monitor capture, so the tap is not after the "
              "hardware mixer and leg B proves nothing.")
        return 2
    print("  tap OK — the speaker's volume does not reach the monitor, so it "
          "is applied after everything measured here.")
    if sr_d >= 30:
        print("  INCONCLUSIVE — attenuating inside the content produced a pure "
              "gain too, so the chain is running linearly at this level and "
              "nothing here can tell the two hypotheses apart. Re-run with a "
              "louder stimulus.")
        return 2
    print("  discrimination OK — attenuating before the graph does change the "
          "output shape, so this stimulus does exercise the dynamics.")
    if sr_b >= 30:
        print("  chain volume behaves as a plain gain OUTSIDE the graph: one "
              "scalar explains the whole difference. Selecting the chain costs "
              "the extra stage, not a different tuning.")
    else:
        print("  chain volume is applied BEFORE the graph: no single gain "
              "explains the difference, so the dynamics stages saw a quieter "
              "signal and engaged differently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
