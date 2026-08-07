"""``--doctor`` for the EasyEffects path: run the probes, assemble, print.

A generated preset can be flawless yet inaudible because of the *environment*
it lands in — EasyEffects 7 (which can't read the v8 preset format), presets
written to the Flatpak path while EE runs native (or vice-versa), a missing
impulse file so the speaker-correction convolver loads nothing, no Dolby
preset selected, or a kernel series so old it mis-configures the speaker path
itself (issue #33). ``--doctor`` surfaces those deterministically (#22), and
``warn_ee_environment`` reuses the same probes to warn at the end of a normal
run.

This is the I/O half of that. Its counterpart `lib/report/environment.py`
holds the verdicts — one pure `*_status` function per check, taking plain
inputs — so the states this machine cannot produce are still unit-testable;
everything here reads the system, and the split follows the comment that used
to sit over both halves in the generator.

The two halves are a strict stack, and that is why this is a module of its own
rather than more of `environment.py`. `lib/report/speaker.py` imports
`environment` (`upgrade_prospect` reads `parse_kernel_series`,
`_print_speaker_info` reads `_kernel_series_age`), and the report assembled
here folds in *both* — `_gather_doctor_report` calls `_gather_speaker_info`
and `speaker_pin_status`, `_print_doctor_report` calls `_print_speaker_info`.
So it sits above them, and putting it back in `environment.py` would close a
loop: environment → speaker → environment.

The EasyEffects-side counterpart of `lib/pipewire/checks.py`, whose
`report_pw_doctor` the converter calls the same way `dolby_to_easyeffects.py`
calls `report_doctor` here. The two doctors share their report vocabulary —
PASS/WARN/FAIL/UNKNOWN, `CheckResult`, the summary counter and the check
printer — through `lib/doctor.py`, so they read as one tool. It arrives under
bare names rather than through the module because that is how these lines read
in the generator and a move commit may not re-point what it carries; they are
constants and a frozen dataclass, so there is nothing here a test would patch.

`findings` and `speaker` keep the aliases the generator gave them
(`report_findings`, `report_speaker`) for the same reason: the moved lines
read through those names. In the generator the first dodges a local named
`findings` in `main()` and the second is one letter from `lib.hardware.speakers`
— neither hazard exists here, and the names are kept anyway because renaming
them would cost the provenance of every line that uses them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib import console, ee_paths, version
from lib.doctor import DOCTOR_FAIL, DOCTOR_PASS, DOCTOR_WARN, CheckResult
from lib.preset import autoload
from lib.report import environment
from lib.report import findings as report_findings
from lib.report import speaker as report_speaker


def parse_ee_version(text: str) -> tuple[int, int, int] | None:
    """Extract (major, minor, patch) from an EasyEffects version string.

    Keys ONLY on the first ``N.N[.N]`` numeric token, so it's robust to the
    ``easyeffects ``/``EasyEffects ``/``Version: `` prefix and to case. Patch
    defaults to 0 when absent. Returns None when there's no version-like token.
    """
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def _flatpak_version_text(info_output: str) -> str:
    """Pull just the ``Version:`` line out of `flatpak info` output. The full
    blob has other numeric tokens (sizes, refs) that would mis-parse, so we
    isolate the one line; absent → "" (→ UNKNOWN, never a wrong version)."""
    for line in info_output.splitlines():
        if line.strip().lower().startswith("version:"):
            return line
    return ""


def easyeffects_is_running() -> bool:
    """Return True if an EasyEffects process is currently running.

    Used to warn the user that easyeffectsrc edits won't take effect until
    EE is restarted — EE reads the file on startup and writes it on exit,
    so mid-run writes get clobbered.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "easyeffects"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        # OSError covers FileNotFoundError (no pgrep) and PermissionError
        # (sandboxed/SELinux hosts) — never crash a caller that only wants a
        # best-effort "is EE up?" (e.g. --doctor's fact-gathering).
        return False


@dataclass
class EEProbe:
    """Outcome of looking for an EasyEffects install.

    ``found`` means a binary *answered*; ``silent`` is set instead when one is
    demonstrably installed but couldn't answer, and carries the short reason.
    All three of found / silent / neither are distinct states — collapsing the
    middle one into "not installed" is what misled issue #46.
    """
    version: tuple[int, int, int] | None = None
    found: bool = False
    source: str = ""
    is_flatpak: bool | None = None
    silent: str | None = None


def _probe_ee_version() -> EEProbe:
    """Probe the installed EasyEffects version. Read-only, time-bounded, never
    raises.

    Probes the install the script writes to (per ee_paths.USE_FLATPAK) first, then the
    other, and prefers a *parseable* version over a found-but-unreadable answer
    — so a stale/shim binary on one install can't mask a healthy version on the
    other (issue #22 review). ``found`` means an EE binary actually answered, so
    version=None with found=True means 'installed but version unreadable'."""
    def run(cmd):
        """(output, failure) — exactly one is non-None; failure is a short
        human-readable reason the command produced no answer."""
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except FileNotFoundError:
            return None, None                      # nothing to run: absent, not silent
        except subprocess.TimeoutExpired:
            return None, "timed out after 5s"
        except (subprocess.SubprocessError, OSError) as exc:
            return None, str(exc) or type(exc).__name__
        if r.returncode != 0:
            first = next((ln.strip() for ln in (r.stderr or "").splitlines()
                          if ln.strip()), "")
            return None, first or f"exited with status {r.returncode}"
        return (r.stdout or "") + "\n" + (r.stderr or ""), None

    def native():
        out, failure = run(["easyeffects", "--version"])
        if out is not None:
            return parse_ee_version(out), True, None
        # A binary that's on PATH (or already running) but couldn't answer is
        # installed, not absent. EE 8's Qt build needs a display to handle
        # --version, so from a headless shell (ssh, tmux) it exits non-zero —
        # indistinguishable from "not installed" if we only read the exit code
        # (issue #46, where a healthy 8.2.8 was reported missing).
        installed = shutil.which("easyeffects") or easyeffects_is_running()
        return None, False, (failure or "no output") if installed else None

    def flatpak():
        # `flatpak info` exits non-zero precisely when the app isn't installed,
        # so a failure here is absence — never the silent-but-installed case.
        out, _failure = run(["flatpak", "info", ee_paths.FLATPAK_APP_ID])
        if out is None:
            return None, False, None
        return parse_ee_version(_flatpak_version_text(out)), True, None

    probes = ([(True, flatpak), (False, native)] if ee_paths.USE_FLATPAK
              else [(False, native), (True, flatpak)])
    fallback = EEProbe()                 # best found-but-unparseable, in order
    for is_flatpak, probe in probes:
        version, found, silent = probe()
        src = "flatpak info" if is_flatpak else "easyeffects --version"
        if not found:
            if silent and fallback.silent is None:
                fallback.silent = silent
                fallback.source = src
            continue
        if version is not None:
            return EEProbe(version, True, src, is_flatpak)
        if not fallback.found:           # remember the first install that answered
            fallback = EEProbe(None, True, src, is_flatpak, fallback.silent)
    return fallback


def _gather_doctor_report(output_dir: Path, irs_dir: Path, rc_path: Path,
                          custom_dirs: bool = False) -> environment.DoctorReport:
    """Run every probe and assemble a DoctorReport. All I/O is wrapped so a
    missing binary / unreadable file degrades to a soft line, never a crash."""
    report = environment.DoctorReport()

    # 1. EasyEffects version / compatibility
    probe = _probe_ee_version()
    version, found, source, ee_is_flatpak = (
        probe.version, probe.found, probe.source, probe.is_flatpak)
    report.checks.append(environment.ee_version_status(version, found, probe.silent))

    # 2. Install location (skip the EE-location verdict for custom dirs)
    if custom_dirs:
        report.checks.append(CheckResult(DOCTOR_PASS, "Install location",
            f"custom output dir ({environment._tilde(output_dir)}) — skipping EasyEffects "
            "location checks."))
    else:
        report.checks.append(environment.install_status(
            ee_paths.FLATPAK_BASE.exists(), ee_paths.NATIVE_BASE.exists(), ee_paths.USE_FLATPAK,
            environment._tilde(ee_paths.EASYEFFECTS_BASE), ee_is_flatpak))

    # 3. Preset + impulse-file integrity
    try:
        irs_stems = {p.stem for p in irs_dir.glob("*.irs")}
    except OSError:
        irs_stems = set()
    try:
        preset_paths = sorted(output_dir.glob("*.json"))
    except OSError:
        preset_paths = []
    generated_names = [p.stem for p in preset_paths]
    dolby_presets = [p for p in preset_paths if p.stem != autoload.BYPASS_PRESET_NAME]
    if not dolby_presets:
        report.checks.append(CheckResult(DOCTOR_WARN, "Generated presets",
            f"no presets found in {environment._tilde(output_dir)} — run the script on your "
            "tuning XML first."))
    else:
        for p in dolby_presets:
            try:
                data = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                report.checks.append(CheckResult(DOCTOR_FAIL, f"Preset {p.stem}",
                    "could not be read / not valid JSON."))
                continue
            report.checks.append(environment.check_preset_kernel(data, irs_stems, p.stem))

    # 4. EasyEffects runtime state (loaded preset, sink, chain)
    try:
        rc_text = rc_path.read_text(encoding="utf-8")
    except OSError:
        rc_text = ""
    rc = autoload.read_ee_rc(rc_text)
    # The selected-preset check compares against presets in output_dir; that's
    # only meaningful when output_dir is where EE actually loads from (default
    # dirs). Under custom dirs, surface the loaded preset as a fact instead.
    if rc_text and not custom_dirs:
        report.checks.append(environment.loaded_preset_status(rc, generated_names))
    # Background-service / autostart is install-global, not output-dir-specific,
    # so it runs even under custom dirs (unlike the selected-preset check).
    if rc_text:
        report.checks.append(environment.autostart_status(rc))

    # 5. Hardware / codec context (folds in --speaker-info)
    report.speaker_info = report_speaker._gather_speaker_info()

    # 6. Smart-amp firmware gate — upstream of the whole preset (issue #17)
    gate_check = environment.firmware_gate_status(report.speaker_info.firmware_gates)
    if gate_check is not None:
        report.checks.append(gate_check)

    # 7. A woofer pin the firmware hides, so half the speakers go unused
    #    upstream of the whole preset (issue #53)
    pin_check = report_speaker.speaker_pin_status(report.speaker_info)
    if pin_check is not None:
        report.checks.append(pin_check)

    # 8. Kernel age — speaker-amp fixes land kernel-side (issue #33)
    report.checks.append(environment.kernel_age_status(report.speaker_info.kernel))

    report.facts = {
        "ee_version": (".".join(map(str, version)) if version else "unknown")
                      + (f" (via {source})" if source else ""),
        "ee_running": easyeffects_is_running(),
        "install": "Flatpak" if ee_paths.USE_FLATPAK else "native",
        "output_dir": environment._tilde(output_dir),
        "irs_dir": environment._tilde(irs_dir),
        "preset_count": len(generated_names),
        "irs_count": len(irs_stems),
        "rc_path": environment._tilde(rc_path),
        "rc_present": bool(rc_text),
        "selected_preset": rc.get("last_output_preset", "")
                           or rc.get("fallback_preset", ""),
        "autostart_on_login": rc.get("autostart_on_login", False),
        "service_mode": rc.get("service_mode", True),
        "output_device": rc.get("output_device", ""),
        "output_plugins": rc.get("output_plugins", []),
    }
    return report


def _print_doctor_report(report: environment.DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    emit = environment.emit_check

    console.cprint("head", f"speaker-tuning-to-easyeffects {version.get_version()}")
    console.cprint("head", "=== EasyEffects doctor ===")
    print()
    # Per-preset checks collapse to one line when they all pass (a machine can
    # have dozens of profiles); any problem preset is still listed individually.
    preset_checks = [c for c in report.checks if c.label.startswith("Preset ")]
    preset_problems = [c for c in preset_checks if c.status != DOCTOR_PASS]
    shown_presets = False
    for c in report.checks:
        if c.label.startswith("Preset "):
            if not shown_presets:
                shown_presets = True
                ok_n = len(preset_checks) - len(preset_problems)
                if ok_n:
                    console.cprint("ok", f"  [{DOCTOR_PASS:^4}] Presets "
                                 f"({ok_n}/{len(preset_checks)} load their impulse file)")
                for pc in preset_problems:
                    emit(pc)
            continue
        emit(c)
    print()
    environment.print_doctor_summary(report.checks)
    print()

    # Raw probed facts — always shown so an issue can be diagnosed remotely even
    # when a heuristic verdict is UNKNOWN or wrong.
    f = report.facts
    console.cprint("head", "=== Environment (paste this into your issue) ===")
    print(f"  Tool:         speaker-tuning-to-easyeffects {version.get_version()}")
    print(f"  EasyEffects:  {f.get('ee_version', '?')}; "
          f"running: {'yes' if f.get('ee_running') else 'no'}")
    print(f"  Install:      {f.get('install')} (writes to {f.get('output_dir')})")
    print(f"  Presets/IRs:  {f.get('preset_count', 0)} presets, "
          f"{f.get('irs_count', 0)} impulse files")
    print(f"  Config:       {f.get('rc_path')} "
          f"({'present' if f.get('rc_present') else 'absent'})")
    print(f"  Background:   service mode "
          f"{'on' if f.get('service_mode') else 'off'}, autostart "
          f"{'on' if f.get('autostart_on_login') else 'off'}")
    if f.get("selected_preset"):
        print(f"  Selected:     {f['selected_preset']}")
    if f.get("output_device"):
        print(f"  Output sink:  {f['output_device']}")
    if f.get("output_plugins"):
        print(f"  Active chain: {', '.join(f['output_plugins'])}")
    print()

    # What the doctor can't see — guide the user through the manual checks.
    environment.print_doctor_verdict(report.checks)
    console.cprint("dim", "If you still hear no difference between the preset and bypass:")
    console.cprint("dim", "  • In EasyEffects, toggle the preset off/on to A/B it.")
    console.cprint("dim", "  • Make sure global bypass (the power-button icon, top bar) is OFF.")
    console.cprint("dim", "  • Confirm system output is the speaker sink and volume is up.")
    print()

    if report.speaker_info is not None:
        report_speaker._print_speaker_info(report.speaker_info)

    console.cprint("cta", "Still stuck? Paste everything above into an issue:")
    console.cprint("cta", f"  {report_findings._REPORT_FORM_URL}")


def report_doctor(args) -> None:
    """--doctor entry point: run environment self-diagnostics and print them."""
    custom_dirs = (args.output_dir != ee_paths.DEFAULT_OUTPUT_DIR
                   or args.irs_dir != ee_paths.DEFAULT_IRS_DIR)
    report = _gather_doctor_report(args.output_dir, args.irs_dir,
                                   ee_paths.DEFAULT_EASYEFFECTS_RC, custom_dirs=custom_dirs)
    _print_doctor_report(report)


def warn_ee_environment(args) -> None:
    """End-of-run check for a normal generation run: loudly warn if the
    installed EasyEffects can't use the presets we just wrote. Silent on the
    happy path. Reuses --doctor's probes; mirrors warn_speaker_firmware_gate."""
    probe = _probe_ee_version()
    version, found, ee_is_flatpak = probe.version, probe.found, probe.is_flatpak
    ver = environment.ee_version_status(version, found, probe.silent)

    if ver.status == DOCTOR_FAIL:
        vstr = ".".join(str(x) for x in version)
        console.cprint("err", f"\n{'=' * 60}")
        console.cprint("err", f"⚠  EasyEffects {vstr} detected — these presets need EasyEffects 8.")
        print()
        console._cprint_wrapped("dim", environment.ee_v7_message(vstr))
        print()
        console.cprint("dim", "To fix, install EasyEffects 8:")
        console.cprint("cta", "  • Easiest on any distro — the Flathub Flatpak:")
        console.cprint("cta", "      flatpak install flathub com.github.wwmm.easyeffects")
        console.cprint("dim", "  • Or your distro's own package if it already ships 8.x")
        console.cprint("dim", "    (Debian trixie, Ubuntu 24.04+ and Fedora ≤43 still ship 7.x).")
        return

    if not found and probe.silent:
        # Installed but unreachable — say so, rather than sending someone off to
        # install what they already have (issue #46).
        # "written above" only holds on a run that wrote something: this check
        # is gated on --skip-ee-check alone, so on a dry run it referred to
        # presets the same output twice says were not written.
        console.cprint("warn", "\n⚠  " + environment.ee_silent_message(
            probe.silent,
            " and doesn't affect what this run would write." if args.dry_run
            else " and doesn't affect the presets written above."))
    elif not found:
        console.cprint("warn", "\n⚠  Couldn't find EasyEffects — install version 8 to use these "
                       "presets (e.g. the Flathub Flatpak). Ignore if you're "
                       "generating for another machine.")

    # Install-location mismatch (only meaningful for the default EE dirs): the
    # detected EE build differs from where we wrote. Warn so the user can point
    # --output-dir/--irs-dir at the install they actually run.
    if (args.output_dir == ee_paths.DEFAULT_OUTPUT_DIR and args.irs_dir == ee_paths.DEFAULT_IRS_DIR
            and ee_is_flatpak is not None and ee_is_flatpak != ee_paths.USE_FLATPAK):
        run_where = "Flatpak" if ee_is_flatpak else "native"
        where = "Flatpak" if ee_paths.USE_FLATPAK else "native"
        console.cprint("warn", f"\n⚠  Presets were written to the {where} EasyEffects "
                       f"location, but the {run_where} install was detected — if "
                       "that's the one you use, it won't see them (run --doctor).")
