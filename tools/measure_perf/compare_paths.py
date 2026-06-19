#!/usr/bin/env python3
"""compare_paths.py — EasyEffects vs PipeWire-filter-chain performance.

Measures the CPU / DSP-load / memory cost of the two delivery paths for the
SAME preset on the SAME machine, controlling live-system measurement noise:

  * Differential vs a same-session `bypass` baseline (cancels shared load).
  * Frequency-invariant cost: the CPU clock can't be pinned on every machine,
    so the HEADLINE metric is CPU CYCLES (via perf) — the cycle count to push a
    fixed DSP workload is the same regardless of clock, whereas time-based CPU%
    is not. quantum/rate are still force-pinned; governor/turbo are recorded.
  * Warm-up discard + windowed sampling; interleaved condition order across
    rounds (thermal-drift control); median/p95/IQR, never a bare mean.
  * Per-window VALIDITY + ACTIVENESS gate: cycles counted, processing nodes
    actually running (BUSY>0), tracked PIDs still alive, captured output
    non-silent — else the window is flagged invalid, not reported as cheap.
  * EXPECTED-RESPONSE gate: every condition renders into the `ee_capture` null
    sink (mute-proof, silent — audio never reaches the speaker); we capture
    each output spectrum and assert pw ≈ ee (both apply the same correction)
    and both differ from bypass. PW reproducing the already-validated EE chain
    IS the expected-response proof, with no offline magnitude prediction.

Routing reuses the proven `tools/measure_ee/setup_null_sink.sh` (loads
`ee_capture`, repoints EE output there; restored by teardown.sh). The PW
condition uses a lean self-contained `pipewire -c filter-chain.conf` child
loader (its PID is the chain's whole CPU; setup_chain.sh is rotted), and EE is
stopped for the bypass and PW conditions so its analyzers don't burn CPU.

Metrics: CPU cycles via perf (headline, frequency-invariant — needs perf and
perf_event_paranoid<=1); per-PID CPU% from /proc (secondary, frequency-noisy);
pw-top BUSY µs + xruns (real-time view); Pss from smaps_rollup (approximate).
AUDIO HANDOFF REQUIRED; a try/finally restores EE config, default sink, and
quantum even on crash.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

HZ = os.sysconf("SC_CLK_TCK")
REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "localresearch" / "measure_perf"
IRS_DIR = Path.home() / ".local/share/easyeffects/irs"
SETUP_NULL = REPO_ROOT / "tools/measure_ee/setup_null_sink.sh"
TEARDOWN_NULL = REPO_ROOT / "tools/measure_ee/teardown.sh"
CAPTURE_SINK = "ee_capture"
EE_SINK = "easyeffects_sink"
PERF_NODE = "Perf_Chain"
DEFAULT_QUANTUM = 1024
DEFAULT_RATE = 48000


# --------------------------------------------------------------------------
# shell + sysfs + pipewire helpers
# --------------------------------------------------------------------------
def sh(cmd, check=False, timeout=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def cpu_governor():
    try:
        return Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()
    except OSError:
        return "?"


def turbo_enabled():
    try:
        return Path("/sys/devices/system/cpu/intel_pstate/no_turbo").read_text().strip() == "0"
    except OSError:
        return None


def pw_settings():
    _, out = sh(["pw-metadata", "-n", "settings"])
    d = {}
    for line in out.splitlines():
        if "key:'" in line and "value:'" in line:
            k = line.split("key:'")[1].split("'")[0]
            v = line.split("value:'")[1].split("'")[0]
            d[k] = v
    return d


def pw_force(q, rate):
    sh(["pw-metadata", "-n", "settings", "0", "clock.force-quantum", str(q)])
    sh(["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate)])


def pw_unforce():
    pw_force(0, 0)


def proc_cpu_ticks(pid):
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return int(f[11]) + int(f[12])         # utime + stime (post-comm index)
    except (OSError, IndexError, ValueError, TypeError):
        return None


def proc_core_freqs_mhz(pids):
    """Frequencies of the cores the given processes' threads are *currently*
    on — follows migration and multi-threading, so the clock-drift gate
    reflects the clock the measured work actually ran at, not a global proxy.
    Reads each thread's `processor` (field 39 of .../task/<tid>/stat) then that
    core's scaling_cur_freq."""
    cores = set()
    for pid in pids:
        try:
            tids = list(Path(f"/proc/{pid}/task").iterdir())
        except OSError:
            continue
        for t in tids:
            try:
                f = (t / "stat").read_text().rsplit(") ", 1)[1].split()
                cores.add(int(f[36]))               # post-comm index 36 == field 39 (processor)
            except (OSError, IndexError, ValueError):
                pass
    out = []
    for c in cores:
        try:
            out.append(int(Path(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq").read_text()) / 1000.0)
        except (OSError, ValueError):
            pass
    return out


def proc_pss_kb(pid):
    try:
        for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError, TypeError):
        return None
    return None


def pgrep1(name):
    _, out = sh(["pgrep", "-x", name])
    pids = [int(x) for x in out.split()]
    return pids[0] if pids else None


def main_pipewire_pid():
    """The main pipewire daemon, NOT a `pipewire -c <conf>` child — two
    pipewire processes coexist (daemon + filter-chain host), and pgrep order
    is by PID, so `pgrep1` is a coin-flip. Pick the one whose cmdline has no
    `-c`, so the baseline always measures the daemon."""
    _, out = sh(["pgrep", "-x", "pipewire"])
    fallback = None
    for tok in out.split():
        pid = int(tok)
        try:
            argv = [a for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00") if a]
        except OSError:
            continue
        fallback = fallback or pid
        if b"-c" not in argv:
            return pid
    return fallback


def default_sink():
    _, out = sh(["pactl", "get-default-sink"])
    return out.strip()


def set_default_sink(name):
    sh(["pactl", "set-default-sink", name])


def node_present(substr):
    _, out = sh(["pw-cli", "ls", "Node"])
    return substr in out


def ee_running():
    return pgrep1("easyeffects") is not None


def stop_ee():
    if ee_running():
        sh(["pkill", "-x", "easyeffects"])
        for _ in range(20):
            if not ee_running():
                break
            time.sleep(0.2)


def start_ee():
    if not ee_running():
        subprocess.Popen(["easyeffects", "--hide-window", "--service-mode"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        for _ in range(30):
            if node_present(EE_SINK):
                break
            time.sleep(0.2)


# --------------------------------------------------------------------------
# pw-top (DSP load + xruns)
# --------------------------------------------------------------------------
def _busy_us(tok):
    if tok in ("---", "???") or not tok:
        return None
    try:
        return float(tok.rstrip("us"))
    except ValueError:
        return None


def pwtop_snapshots(iterations):
    _, out = sh(["pw-top", "-b", "-n", str(iterations)], timeout=iterations * 3 + 20)
    snaps, cur = [], None
    for line in out.splitlines():
        f = line.split()
        if len(f) < 10:
            continue
        if f[0] == "S" and f[1] == "ID":
            if cur is not None:
                snaps.append(cur)
            cur = {}
            continue
        if cur is None:
            cur = {}
        try:
            int(f[1])
            err = int(f[8])
        except ValueError:
            continue
        cur[f[-1]] = (_busy_us(f[5]), err)
    if cur:
        snaps.append(cur)
    return snaps


# --------------------------------------------------------------------------
# stimulus + spectrum
# --------------------------------------------------------------------------
def make_pink(path, seconds, rate=DEFAULT_RATE):
    n = int(seconds * rate)
    white = np.random.default_rng(0).standard_normal((n, 2))
    spec = np.fft.rfft(white, axis=0)
    f = np.fft.rfftfreq(n, 1.0 / rate)
    f[0] = f[1]
    spec /= np.sqrt(f)[:, None]
    pink = np.fft.irfft(spec, n=n, axis=0)
    pink /= np.max(np.abs(pink)) + 1e-9
    data = (pink * 0.25 * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.tobytes())


def band_db(wav_path, rate=DEFAULT_RATE, n_bands=48):
    """Welch-averaged magnitude in log-spaced 50 Hz–18 kHz bands (dB), plus
    the broadband RMS (dBFS). Returns (bands, rms_dbfs) or (None, -inf)."""
    try:
        with wave.open(str(wav_path)) as w:
            ch = w.getnchannels()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError):
        return None, float("-inf")
    data = np.frombuffer(raw, dtype="<i2").astype(float) / 32768.0
    if ch == 2:
        data = data.reshape(-1, 2).mean(axis=1)
    if len(data) < 8192:
        return None, float("-inf")
    rms = 20 * np.log10(np.sqrt(np.mean(data ** 2)) + 1e-12)
    seg, win = 8192, np.hanning(8192)
    acc, cnt = None, 0
    for i in range(0, len(data) - seg, seg // 2):
        p = np.abs(np.fft.rfft(data[i:i + seg] * win)) ** 2
        acc = p if acc is None else acc + p
        cnt += 1
    psd = acc / cnt
    freqs = np.fft.rfftfreq(seg, 1.0 / rate)
    edges = np.geomspace(50, 18000, n_bands + 1)
    bands = []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (freqs >= a) & (freqs < b)
        bands.append(10 * np.log10(psd[m].mean() + 1e-20) if m.any() else np.nan)
    return np.array(bands), rms


def capture_monitor(src_node, src_ports, out_wav, seconds):
    """Capture `src_node`'s monitor ports for `seconds`. pw-record's --target
    is only a hint WirePlumber overrides (reroutes to the default mic), so —
    per tools/measure_ee/smoke.py — start with --target 0 (no auto-link) and
    pw-link the ports by hand. Returns True on success, False if it couldn't
    bind (capture then reads as missing, not as a silent false-cheap result)."""
    rec_name = "perf_recorder"
    rec = subprocess.Popen(
        ["pw-record", "--target", "0", "--rate", str(DEFAULT_RATE),
         "--channels", "2", "--format", "s16",
         "-P", f"node.name={rec_name}", str(out_wav)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _, probe = sh(["pw-link", "-i"])
            if any(l.strip() == f"{rec_name}:input_FL" for l in probe.splitlines()):
                break
            time.sleep(0.05)
        else:
            return False
        for sp, ch in zip(src_ports, ("FL", "FR")):
            sh(["pw-link", f"{src_node}:{sp}", f"{rec_name}:input_{ch}"])
        time.sleep(seconds)
        return True
    finally:
        rec.terminate()
        try:
            rec.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.kill()


def pw_chain_up(preset):
    """Lean, self-contained chain loader (setup_chain.sh is rotted): a v1
    virtual-sink conf (no smart-filter) pinned to ee_capture, in an isolated
    `pipewire -c filter-chain.conf` child whose PID is the chain's whole CPU."""
    conf = Path.home() / ".config/pipewire/filter-chain.conf.d" / f"{PERF_NODE}.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    sh(["python3", str(REPO_ROOT / "ee_to_pipewire.py"), str(preset),
        "--output", str(conf), "--node-name", PERF_NODE,
        "--target-sink", "", "--target-object", CAPTURE_SINK,
        "--no-validate", "--force", "--irs-dir", str(IRS_DIR)], check=True, timeout=60)
    proc = subprocess.Popen(["pipewire", "-c", "filter-chain.conf"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    for _ in range(40):
        if node_present(f"effect_input.{PERF_NODE}"):
            break
        time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("perf chain did not register")
    time.sleep(0.5)
    for ch in ("FL", "FR"):                       # ensure output reaches ee_capture
        sh(["pw-link", f"effect_output.{PERF_NODE}:output_{ch}", f"{CAPTURE_SINK}:playback_{ch}"])
    return proc, conf


def pw_chain_down(proc, conf):
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if conf:
        conf.unlink(missing_ok=True)


def perf_cycles(pids, seconds):
    """Total CPU cycles consumed by `pids` over `seconds`, via perf — the
    frequency-INVARIANT cost of the fixed DSP workload (a wandering clock
    changes wall-time / CPU% but not the cycle count to do the same work).
    `perf stat -- sleep N` is the window timer. None if perf can't count
    (e.g. perf_event_paranoid > 1)."""
    if not pids:
        return None
    pidlist = ",".join(str(p) for p in pids)
    _, out = sh(["perf", "stat", "-x", ",", "-e", "cycles", "-p", pidlist,
                 "--", "sleep", str(seconds)], timeout=seconds + 20)
    # Hybrid CPUs split `cycles` into cpu_core/cycles/ + cpu_atom/cycles/; a
    # process only runs on one core type at a time, so the other reads
    # <not counted>. Sum every counted `.../cycles/` line.
    total = None
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 3 and "cycles" in parts[2]:
            try:
                total = (total or 0) + int(parts[0].strip())
            except ValueError:
                continue                             # <not counted> on an unused PMU
    return total


# --------------------------------------------------------------------------
# one measurement window
# --------------------------------------------------------------------------
def measure(target_sink, pids, node_match, warmup, window, capture_s, noise, cap_wav):
    set_default_sink(target_sink)
    player = subprocess.Popen(["pw-play", "--target", target_sink, str(noise)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        time.sleep(warmup)
        f0 = max(proc_core_freqs_mhz(pids) or [0])   # info only — cycles are frequency-invariant
        t0 = time.monotonic()
        c0 = {pid: proc_cpu_ticks(pid) for pid in pids}
        snaps_box = {}
        th = threading.Thread(target=lambda: snaps_box.__setitem__("s", pwtop_snapshots(window)))
        th_cap = threading.Thread(target=capture_monitor,
                                  args=(CAPTURE_SINK, ("monitor_FL", "monitor_FR"), cap_wav, capture_s))
        th.start()
        th_cap.start()
        cycles = perf_cycles(pids, window)           # perf -- sleep is the window timer
        th.join()
        th_cap.join(timeout=10)
        t1 = time.monotonic()
        c1 = {pid: proc_cpu_ticks(pid) for pid in pids}
        f1 = max(proc_core_freqs_mhz(pids) or [0])
        pss = sum(filter(None, (proc_pss_kb(pid) for pid in pids))) or None
    finally:
        player.terminate()
        try:
            player.wait(timeout=2)
        except subprocess.TimeoutExpired:
            player.kill()

    dt = max(t1 - t0, 1e-6)
    pids_alive = all(c0[p] is not None and c1[p] is not None for p in pids)
    cpu = sum((c1[p] - c0[p]) / HZ / dt * 100 for p in pids if c0[p] is not None and c1[p] is not None)
    gcyc_per_s = round(cycles / dt / 1e9, 3) if cycles else None

    snaps = snaps_box.get("s", [])
    busy, xruns, active_snaps = [], 0, 0
    for snap in snaps:
        tot = sum(b for name, (b, e) in snap.items()
                  if b is not None and any(m in name.lower() for m in node_match))
        busy.append(tot)
        if tot > 0:
            active_snaps += 1
        xruns = max(xruns, max((e for _, (b, e) in snap.items()), default=0))

    bands, rms = band_db(cap_wav)
    held = pw_settings().get("clock.force-quantum")
    return {
        "gcyc_per_s": gcyc_per_s,                   # headline: frequency-invariant CPU cost
        "cycles": cycles,
        "cpu_pct": round(cpu, 2),                   # secondary (frequency-sensitive)
        "dsp_busy_us": busy,
        "xruns": xruns,
        "pss_kb": pss,
        "cap_rms_dbfs": round(rms, 1),
        "cap_bands": None if bands is None else [round(float(x), 2) for x in bands],
        "busy_fraction": round(active_snaps / len(snaps), 2) if snaps else 0.0,
        "freq_mhz": [round(f0), round(f1)],         # info only
        "pids_alive": pids_alive,
        "quantum_held": held,
        "n_snapshots": len(snaps),
    }


# --------------------------------------------------------------------------
# stats
# --------------------------------------------------------------------------
def robust(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    a = np.asarray(vals, float)
    return {"median": round(float(np.percentile(a, 50)), 2),
            "p95": round(float(np.percentile(a, 95)), 2),   # interpolated; ~max at small n
            "iqr": round(float(np.percentile(a, 75) - np.percentile(a, 25)), 2),
            "n": len(vals)}


def spectral_delta(a, b):
    """max |a-b| over bands after removing the common mean (level-invariant)."""
    if a is None or b is None:
        return None
    a, b = np.array(a, float), np.array(b, float)
    d = (a - a[np.isfinite(a)].mean()) - (b - b[np.isfinite(b)].mean())
    d = d[np.isfinite(d)]
    return round(float(np.max(np.abs(d))), 2) if len(d) else None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("preset", type=Path)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--warmup", type=float, default=5.0)
    ap.add_argument("--window", type=int, default=15)
    ap.add_argument("--capture", type=float, default=2.5, help="output-capture seconds")
    ap.add_argument("--quantum", type=int, default=DEFAULT_QUANTUM)
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE)
    ap.add_argument("--out", type=Path, default=OUT_DIR / "perf_summary.json")
    ap.add_argument("--check", action="store_true", help="preview env; no audio")
    args = ap.parse_args(argv)
    if not args.preset.is_file():
        ap.error(f"preset not found: {args.preset}")

    gov, turbo = cpu_governor(), turbo_enabled()
    print(f"env: governor={gov} turbo={'on' if turbo else 'off' if turbo is False else '?'} "
          f"quantum={args.quantum} rate={args.rate}")
    if gov != "performance":
        print("  WARNING: governor != performance — low-confidence numbers.")
    if turbo:
        print("  WARNING: turbo on — disable: sudo sh -c 'echo 1 > /sys/devices/system/cpu/intel_pstate/no_turbo'")
    print(f"plan: {args.rounds} interleaved rounds x (bypass/ee/pw), "
          f"{args.warmup}s warmup + {args.window}s window "
          f"≈ {args.rounds*3*(args.warmup+args.window+4)/60:.1f} min")
    if args.check:
        print("--check: EE running:", ee_running(), "| default sink:", default_sink())
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    noise = OUT_DIR / "pink_30s.wav"
    make_pink(noise, max(args.warmup + args.window + 3, 30), args.rate)

    prev_sink = default_sink()
    prev_settings = pw_settings()
    results = {"bypass": [], "ee": [], "pw": []}
    setup_null_done = False
    chain = [None, None]                            # (proc, conf) for finally cleanup

    try:
        pw_force(args.quantum, args.rate)
        # one-time: ee_capture null sink + EE output repoint (restored at end)
        rc, out = sh(["bash", str(SETUP_NULL)], check=True, timeout=60)
        setup_null_done = True
        print("ee_capture route up")

        for rnd in range(args.rounds):
            order = ["bypass", "ee", "pw"]
            order = order[rnd % 3:] + order[:rnd % 3]
            print(f"\n--- round {rnd+1}/{args.rounds}: {order} ---")
            for cond in order:
                cap_wav = OUT_DIR / f"cap_{cond}_{rnd}.wav"
                main_pw = main_pipewire_pid()       # daemon load is common to every path
                if cond == "bypass":
                    stop_ee()
                    tgt, pids, match = CAPTURE_SINK, [main_pw], ("__none__",)
                elif cond == "ee":
                    start_ee()
                    sh(["easyeffects", "-l", args.preset.stem])   # apply the SAME preset PW uses
                    time.sleep(1.5)
                    tgt, pids, match = EE_SINK, [main_pw, pgrep1("easyeffects")], ("easyeffects_sink", "ee_soe_")
                else:
                    stop_ee()                       # EE off for PW too — else its analyzers burn CPU
                    chain[0], chain[1] = pw_chain_up(args.preset)
                    tgt, pids, match = f"effect_input.{PERF_NODE}", [main_pw, chain[0].pid], (PERF_NODE.lower(),)
                pids = [p for p in pids if p]        # drop any failed lookup
                try:
                    m = measure(tgt, pids, match, args.warmup, args.window,
                                args.capture, noise, cap_wav)
                finally:
                    if cond == "pw":
                        pw_chain_down(*chain)
                        chain[:] = [None, None]
                expect_busy = cond != "bypass"
                reasons = []
                if not m["pids_alive"]:
                    reasons.append("pid-died")
                if m["gcyc_per_s"] is None:
                    reasons.append("no-cycles")
                if m["cap_rms_dbfs"] <= -60:
                    reasons.append("silent")
                if expect_busy and m["busy_fraction"] < 0.5:
                    reasons.append("inactive")
                m["valid"] = not reasons
                results[cond].append(m)
                tag = "ok" if m["valid"] else "INVALID(" + ",".join(reasons) + ")"
                print(f"  {cond:6s} {m['gcyc_per_s']} Gcyc/s  cpu={m['cpu_pct']:5.1f}% "
                      f"busy_frac={m['busy_fraction']} xruns={m['xruns']} "
                      f"cap_rms={m['cap_rms_dbfs']}dBFS freq={m['freq_mhz']}MHz [{tag}]")
    finally:
        pw_chain_down(*chain)
        if setup_null_done:
            sh(["bash", str(TEARDOWN_NULL)], timeout=60)
        pw_unforce()
        if prev_settings.get("clock.force-quantum", "0") not in ("0", "", None):
            pw_force(prev_settings["clock.force-quantum"], prev_settings.get("clock.force-rate", "0"))
        set_default_sink(prev_sink)
        print("\nrestored: EE config (teardown.sh), default sink, quantum")

    # --- aggregate + expected-response check (per-round pw vs ee vs bypass) ---
    summary = {"preset": str(args.preset), "quantum": args.quantum, "governor": gov,
               "turbo": turbo, "rounds": args.rounds, "window_s": args.window,
               "conditions": {}, "response_check": {}}
    for cond, runs in results.items():
        valid = [r for r in runs if r["valid"]]
        # Totals include the main pipewire daemon + the path's process(es), so
        # the bypass-subtracted marginals below are like-for-like.
        summary["conditions"][cond] = {
            "valid_windows": f"{len(valid)}/{len(runs)}",
            "gcyc_per_s_total": robust([r["gcyc_per_s"] for r in valid]),
            "cpu_pct_total": robust([r["cpu_pct"] for r in valid]),   # secondary, frequency-sensitive
            "dsp_busy_us": robust([b for r in valid for b in r["dsp_busy_us"]]),
            "pss_kb": robust([r["pss_kb"] for r in valid]),
            "xruns_max": max((r["xruns"] for r in runs), default=0),
            "busy_fraction_min": min((r["busy_fraction"] for r in valid), default=0),
            "cap_rms_dbfs_med": robust([r["cap_rms_dbfs"] for r in valid]),
        }
    base = summary["conditions"]["bypass"]["gcyc_per_s_total"]
    for cond in ("ee", "pw"):
        c = summary["conditions"][cond]["gcyc_per_s_total"]
        if base and c:
            summary["conditions"][cond]["gcyc_per_s_marginal"] = round(c["median"] - base["median"], 3)

    pw_vs_ee, ee_vs_byp, pw_vs_byp = [], [], []
    for rnd in range(args.rounds):
        by = results["bypass"][rnd]["cap_bands"] if rnd < len(results["bypass"]) else None
        ee = results["ee"][rnd]["cap_bands"] if rnd < len(results["ee"]) else None
        pw = results["pw"][rnd]["cap_bands"] if rnd < len(results["pw"]) else None
        for store, a, b in ((pw_vs_ee, pw, ee), (ee_vs_byp, ee, by), (pw_vs_byp, pw, by)):
            d = spectral_delta(a, b)
            if d is not None:
                store.append(d)
    summary["response_check"] = {
        "pw_vs_ee_maxdb": robust(pw_vs_ee),       # want SMALL (pw reproduces ee)
        "ee_vs_bypass_maxdb": robust(ee_vs_byp),   # want LARGE (processing happens)
        "pw_vs_bypass_maxdb": robust(pw_vs_byp),
        "verdict": _response_verdict(pw_vs_ee, ee_vs_byp),
    }

    args.out.write_text(json.dumps(summary, indent=2))
    print(f"\nwrote {args.out}")
    print(json.dumps({"conditions": summary["conditions"],
                      "response_check": summary["response_check"]}, indent=2))
    return 0


def _response_verdict(pw_vs_ee, ee_vs_byp):
    if not pw_vs_ee or not ee_vs_byp:
        return "insufficient-data"
    pe = sorted(pw_vs_ee)[len(pw_vs_ee) // 2]
    eb = sorted(ee_vs_byp)[len(ee_vs_byp) // 2]
    if eb < 2.0:
        return f"SUSPECT: ee≈bypass ({eb:.1f}dB) — processing may not be active"
    if pe > 3.0:
        return f"SUSPECT: pw≠ee ({pe:.1f}dB) — PW not reproducing EE response"
    return f"OK: pw≈ee ({pe:.1f}dB), both differ from bypass ({eb:.1f}dB)"


if __name__ == "__main__":
    sys.exit(main())
