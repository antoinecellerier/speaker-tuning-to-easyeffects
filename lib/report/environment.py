"""--doctor's verdicts, and the end-of-run hints that reuse them.

A generated preset can be flawless yet inaudible because of the *environment*
it lands in, and this is where each of those conditions gets its verdict: one
`*_status` function per check, taking plain inputs and returning a
`CheckResult`, so every one of them is unit-testable without touching the
system. The probing and assembly that feeds them — `_probe_ee_version`,
`_gather_doctor_report`, `_print_doctor_report` — are in
`lib/report/doctor_run.py`, which imports this module. The edge only goes that
way: this one reaches `lib/hardware/speakers.py` for a type and never the
report, so `doctor_run.py` can sit above both halves and fold in
`lib/report/speaker.py` as well.

Why the checks are the half worth extracting: they are almost entirely *copy*.
Each carries the sentence a user acts on, and several are the single source
that both `--doctor` and a normal run's end-of-run warning render, which is
what stops the two from drifting (`e3a7ee4` fixed a pair that already had).

The report vocabulary — PASS/WARN/FAIL/UNKNOWN, `CheckResult`, the summary
counter and the check printer — comes from `lib/doctor.py`, shared with
`ee_to_pipewire.py`'s PipeWire-side doctor so the two read as one tool. Not
the `~`-collapsing path renderer, though: these functions take plain inputs,
so the one verdict that names a path (`install_status`) is handed it already
collapsed, as `base_display`. The constants are imported under bare names
rather than through the module because that is how these lines read in
`dolby_to_easyeffects.py`, and a move commit may not re-point what it carries;
they are string constants and a record type, so there is nothing here a test
would want to patch. `BYPASS_PRESET_NAME` arrives the same way, from
`lib/preset/autoload.py` — the empty preset written there is
one this doctor has to recognise, because having it selected is itself a
"sounds like nothing" cause.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass, field
from datetime import date

from lib import console, doctor
from lib.data import kernel_releases
from lib.doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from lib.hardware import speakers
from lib.preset.autoload import BYPASS_PRESET_NAME


# EE names stacked instances of a plugin "convolver#0", "equalizer#1", … —
# match the speaker-correction convolver regardless of its index. Keep the
# "kernel-name" literal in step with make_convolver().
_CONVOLVER_KEY_RE = re.compile(r"^convolver#\d+$")


@dataclass
class DoctorReport:
    checks: list[CheckResult] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # raw probed values, always shown
    speaker_info: "speakers.SpeakerInfo | None" = None


def ee_silent_message(reason: str, tail: str) -> str:
    """The 'installed but --version didn't answer' explanation, shared by
    --doctor and the end-of-run warning so the two can't drift. ``tail``
    finishes the sentence with what it means where it's being said."""
    return (f"EasyEffects is installed but `easyeffects --version` didn't "
            f"answer ({reason}), so its version wasn't checked. EasyEffects 8 "
            f"needs a display to answer --version, so this is expected from a "
            f"headless shell (ssh, tmux){tail}")


def ee_v7_message(vstr: str) -> str:
    """Why an EasyEffects before 8 can't use these presets, shared by --doctor
    and the end-of-run warning so the two can't drift. Callers supply their own
    headline and install instructions — one inline sentence for the report,
    copy-paste commands for the warning."""
    return (f"EasyEffects 8 changed the preset (filter-chain) format, and these "
            f"presets use the new one. On {vstr} they don't load correctly — the "
            "speaker-correction filter loads nothing, so you'll hear little or "
            "no difference.")


def ee_version_status(version: tuple[int, int, int] | None,
                      found: bool, silent: str | None = None) -> CheckResult:
    """Verdict for the EasyEffects version. FAIL — the only loud error — is
    reserved for a *cleanly parsed* major < 8, so an EE-8 user is never told
    they're on 7. ``found`` distinguishes "no EE at all" (a valid
    generating-for-another-machine case → WARN) from "installed but version
    unreadable" (→ UNKNOWN); ``silent`` names the reason when EE is installed
    but never answered at all (→ UNKNOWN, never "not found")."""
    if version is None:
        if not found and silent:
            return CheckResult(DOCTOR_UNKNOWN, "EasyEffects version",
                ee_silent_message(silent, " — re-run this from your desktop "
                                          "session to check the version."))
        if not found:
            return CheckResult(DOCTOR_WARN, "EasyEffects version",
                "not found on PATH or via Flatpak. If you're generating presets "
                "to copy to another machine, ignore this — otherwise install "
                "EasyEffects 8 (e.g. the Flathub Flatpak).")
        return CheckResult(DOCTOR_UNKNOWN, "EasyEffects version",
            "EasyEffects is installed but its version couldn't be read — make "
            "sure it's version 8 or newer.")
    vstr = ".".join(str(x) for x in version)
    if version[0] < 8:
        return CheckResult(DOCTOR_FAIL, "EasyEffects version",
            f"{vstr} detected — these presets need EasyEffects 8. "
            + ee_v7_message(vstr) +
            " Install EasyEffects 8 (the Flathub Flatpak, or your distro's "
            "package if it ships 8.x).")
    return CheckResult(DOCTOR_PASS, "EasyEffects version", f"{vstr} (compatible).")


# A stable distro's kernel is at most ~9 months old on the distro's release day
# (Debian 13 shipped 6.12 at 9 months; Ubuntu LTS GA kernels at ~1 month), so
# 18 months keeps every fresh install quiet for 9+ months and never flags
# HWE/Fedora/Arch users — while still catching the real case we have (#33
# fired at 6.12 + 20 months; LTS point releases backport one-line quirks but
# not the driver rework / power-management fixes of that class).
_KERNEL_OLD_MONTHS = 18


def parse_kernel_series(release: str) -> tuple[int, int] | None:
    """(major, minor) from a ``platform.release()`` string, e.g.
    ``"6.12.74+deb13+1-amd64"`` → ``(6, 12)``. None when unparseable."""
    m = re.match(r"(\d+)\.(\d+)", (release or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _kernel_series_age(series: tuple[int, int],
                       today: date) -> tuple[str, int] | None:
    """(release "YYYY-MM", age in whole months) for an in-table series."""
    released = kernel_releases._KERNEL_SERIES_RELEASES.get(series)
    if not released:
        return None
    y, mo = (int(x) for x in released.split("-"))
    return released, (today.year - y) * 12 + (today.month - mo)


def kernel_age_status(release: str, today: date | None = None) -> CheckResult:
    """Verdict for the running kernel's age. WARN is a hint, not an error: an
    old series *can* be the whole problem on laptop speakers (issue #33), but
    only the user can tell — the detail says what symptom would confirm it."""
    today = today or date.today()
    label = "Kernel age"
    series = parse_kernel_series(release)
    if series is None:
        return CheckResult(DOCTOR_UNKNOWN, label,
            f"couldn't parse a kernel version from {release!r}.")
    sstr = f"{series[0]}.{series[1]}"
    if series > max(kernel_releases._KERNEL_SERIES_RELEASES):
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} — newer than any series this tool knows about.")
    aged = _kernel_series_age(series, today)
    if aged is None:
        if series < min(kernel_releases._KERNEL_SERIES_RELEASES):
            return CheckResult(DOCTOR_WARN, label,
                f"{sstr} is very old (pre-2021). Laptop speaker support "
                "lands kernel-side; strongly consider a newer kernel.")
        return CheckResult(DOCTOR_UNKNOWN, label, f"{sstr} — unknown series.")
    released, months = aged
    if months <= _KERNEL_OLD_MONTHS:
        plural = "" if months == 1 else "s"
        return CheckResult(DOCTOR_PASS, label,
            f"{sstr} (released {released}, ~{months} month{plural} old).")
    return CheckResult(DOCTOR_WARN, label,
        f"{sstr} was released {released} (~{months} months ago). "
        + kernel_old_message())


def kernel_old_message() -> str:
    """Why an old kernel series matters for laptop speakers, shared by --doctor
    and the end-of-run hint so the two can't drift. Callers supply the headline
    naming the series and its age."""
    return ("Laptop speaker fixes (amp drivers, codec setup, power-management "
            "quirks) land kernel-side and are not always backported to older "
            "series — if your speakers sound thin, muffled or garbled even with "
            "EasyEffects off, a newer kernel (your distro's backports or "
            "hardware-enablement/HWE kernel) may fix that.")


def install_status(flatpak_exists: bool, native_exists: bool,
                   base_is_flatpak: bool, base_display: str,
                   ee_is_flatpak: bool | None) -> CheckResult:
    """Verdict for where presets are written vs where EE actually runs.
    ``ee_is_flatpak`` is which install answered the version probe (None if
    unknown)."""
    where = "Flatpak" if base_is_flatpak else "native"
    if not flatpak_exists and not native_exists:
        return CheckResult(DOCTOR_WARN, "Install location",
            f"no EasyEffects data dir found yet; presets go to the {where} "
            f"location ({base_display}). Launch EasyEffects once, or pass "
            "--output-dir/--irs-dir.")
    if ee_is_flatpak is not None:
        run_where = "Flatpak" if ee_is_flatpak else "native"
        if run_where != where:
            # WARN, not FAIL: the install that answered the probe isn't
            # necessarily the one the user launches (dual-install systems), so
            # we can't assert "EE never sees them" with certainty.
            return CheckResult(DOCTOR_WARN, "Install location",
                f"presets were written to the {where} location ({base_display}), "
                f"but the EasyEffects detected is the {run_where} build — if "
                "that's the one you run, it won't see them. Re-run with "
                "--output-dir/--irs-dir for the install you use.")
        return CheckResult(DOCTOR_PASS, "Install location",
            f"{run_where} install; presets written to {base_display}.")
    if flatpak_exists and native_exists:
        return CheckResult(DOCTOR_WARN, "Install location",
            f"both Flatpak and native data dirs exist; the script writes to the "
            f"{where} one ({base_display}) — make sure that's the EasyEffects "
            "you run.")
    return CheckResult(DOCTOR_PASS, "Install location",
        f"{where} install; presets written to {base_display}.")


def check_preset_kernel(preset_json: dict, irs_stems: set,
                        preset_name: str) -> CheckResult:
    """Verify a generated output preset's speaker-correction filter (convolver)
    references an impulse file (.irs) that actually exists. A missing or
    misnamed impulse = silent passthrough: the dominant audible block does
    nothing. ``irs_stems`` are the .irs filename stems present in the irs dir."""
    label = f"Preset {preset_name}"
    if not isinstance(preset_json, dict) or not isinstance(
            preset_json.get("output"), dict):
        return CheckResult(DOCTOR_FAIL, label,
            "not a valid EasyEffects output preset (no 'output' section).")
    conv_keys = [k for k in preset_json["output"] if _CONVOLVER_KEY_RE.match(k)]
    if not conv_keys:
        return CheckResult(DOCTOR_WARN, label,
            "no speaker-correction filter (convolver) in this preset.")
    conv = preset_json["output"][conv_keys[0]]
    conv = conv if isinstance(conv, dict) else {}
    if "kernel-path" in conv and "kernel-name" not in conv:
        return CheckResult(DOCTOR_FAIL, label,
            "uses the old EasyEffects 7 'kernel-path' format — these presets "
            "need EasyEffects 8.")
    name = conv.get("kernel-name", "")
    if not name:
        return CheckResult(DOCTOR_FAIL, label,
            "the speaker-correction filter has no impulse file set — it's silent.")
    if name not in irs_stems:
        return CheckResult(DOCTOR_FAIL, label,
            f"impulse file '{name}.irs' is missing from the irs dir — the "
            "speaker-correction filter loads nothing (silent). Re-run the "
            "script so the .irs is written next to the preset.")
    if conv.get("bypass") is True:
        return CheckResult(DOCTOR_WARN, label,
            f"loads {name}.irs but the speaker-correction filter is bypassed in "
            "the preset.")
    return CheckResult(DOCTOR_PASS, label,
        f"speaker-correction filter loads {name}.irs.")


def loaded_preset_status(rc_data: dict, generated_names) -> CheckResult:
    """Whether EasyEffects' selected output preset is one this script generated.
    Reports last-loaded / fallback without over-claiming which is *active*
    (per-device autoloading lives elsewhere in EE's config). The empty
    ``Nothing`` bypass preset is excluded from "generated": having it selected
    is itself a "sounds like nothing" cause, not a healthy state."""
    dolby = {n for n in generated_names if n != BYPASS_PRESET_NAME}
    loaded = rc_data.get("last_output_preset", "")
    fallback = rc_data.get("fallback_preset", "")
    if not loaded and not fallback:
        return CheckResult(DOCTOR_WARN, "Selected preset",
            "EasyEffects has no output preset recorded yet — open it and load a "
            "Dolby-* preset for the speakers.")
    if loaded == BYPASS_PRESET_NAME:
        return CheckResult(DOCTOR_WARN, "Selected preset",
            f"the silent '{BYPASS_PRESET_NAME}' bypass preset is selected — that's "
            "no processing by design. Load a Dolby-* preset in EasyEffects.")
    if loaded in dolby:
        matched = loaded
    elif rc_data.get("uses_fallback") and fallback in dolby:
        matched = fallback
    else:
        matched = ""
    if matched:
        return CheckResult(DOCTOR_PASS, "Selected preset",
            f"EasyEffects last loaded '{matched}'.")
    return CheckResult(DOCTOR_WARN, "Selected preset",
        f"EasyEffects' selected output preset is '{loaded or fallback}', which "
        "this script didn't generate — load a Dolby-* preset in EasyEffects.")


def autostart_status(rc_data: dict) -> CheckResult:
    """Whether EasyEffects is set to keep running in the background so the
    preset stays applied. Two Background-Service toggles matter, both persisted
    in ``[Window]``: ``autostartOnLogin`` (launch at login — default off) and
    ``enableServiceMode`` (stay active when the window is closed — default on).
    The preset only processes audio while EasyEffects runs, so if EITHER is off
    it silently stops applying after a window-close or reboot — a common "it was
    working, now it sounds like nothing" cause. Both off and a single one off
    are all problem states, so we name exactly the toggle(s) that are off."""
    autostart = rc_data.get("autostart_on_login")
    service = rc_data.get("service_mode")
    if autostart and service:
        return CheckResult(DOCTOR_PASS, "Background service",
            "EasyEffects autostarts as a background service at login — the "
            "preset applies automatically and survives reboots.")
    # Name the toggle(s) up front and adjacent, then group the explanations —
    # inline parentheticals buried the second toggle so it read as one warning.
    off, why = [], []
    if not service:
        off.append("'Enable service mode'")
        why.append("service mode keeps it running once the window is closed")
    if not autostart:
        off.append("'Autostart on login'")
        why.append("autostart relaunches it after a reboot")
    return CheckResult(DOCTOR_WARN, "Background service",
        "EasyEffects won't reliably keep processing in the background, so the "
        "preset applies only while it's open. In EasyEffects > Preferences > "
        "Background Service, turn on " + " and ".join(off)
        + " (" + "; ".join(why) + ").")


def firmware_gate_status(gates: list[speakers.FirmwareGate]) -> CheckResult | None:
    """Verdict line for the smart-amp firmware gates, or None when the machine
    exposes no such control (most don't — there is nothing to report either
    way, and a PASS for an absent control is noise).

    The gate sits *upstream* of everything EasyEffects does, which is why it
    belongs among the checks and not only in the raw hardware dump: a report
    that says "no blocking problems" beside a gate that is off is wrong about
    the one thing most likely to explain silence.

    WARN, not FAIL: an off gate mutes the woofers on most laptops, but on some
    the firmware auto-loads anyway and flipping it is an audible no-op (#39),
    so it is a strong suspect rather than a proven fault.

    The command rides in ``steps``, unwrapped. It also prints in the amp
    section further down, which --speaker-info reaches and this check does
    not, so the repeat within a --doctor run is deliberate: the check is where
    a reader acts, the section is raw evidence.
    """
    if not gates:
        return None
    off = [g for g in gates if not g.on]
    if not off:
        return CheckResult(DOCTOR_PASS, "Speaker firmware gate",
                           "the amplifier is allowed to load its firmware.")
    names = ", ".join(g.name for g in off)
    return CheckResult(
        DOCTOR_WARN, "Speaker firmware gate",
        f"{names} is off, so the amplifier runs untuned ahead of the preset "
        "and your speakers may be silent, thin or crackly whatever the preset "
        "does. Switch it on:",
        steps=tuple(("cta", speakers.amixer_enable_cmd(g)) for g in off))


def emit_check(check: CheckResult) -> None:
    """Print one diagnostic line: status box, label, wrapped detail, steps.

    Hands off to the shared printer so this report and ee_to_pipewire.py's
    PipeWire-side one read as one tool. This used to be a second
    implementation of it, which is how a check's ``steps`` reached the
    PipeWire doctor and not this one — the same duplication the steps
    themselves exist to end.
    """
    doctor.emit_check(check, console.cprint, console._wrap_width())


def print_doctor_summary(checks: list[CheckResult]) -> None:
    """Print the counted one-line summary. Split from the verdict below it
    because the two surfaces put different things between them — the
    EasyEffects report interleaves its paste block."""
    fail, warn, ok, unknown = doctor.summarize(checks)
    parts = [f"{fail} FAIL", f"{warn} WARN", f"{ok} PASS"]
    if unknown:
        parts.append(f"{unknown} UNKNOWN")
    console.cprint("err" if fail else ("warn" if (warn or unknown) else "ok"),
           "Summary: " + ", ".join(parts))


def print_doctor_verdict(checks: list[CheckResult]) -> None:
    """Print the one-line verdict, shared so both doctors conclude the same way.

    A WARN suppresses the all-clear: every warning either report can raise
    names something that plausibly explains "I hear no difference", so
    "no blocking problems" printed beside one contradicts the lines above it.
    """
    fail, warn, ok, unknown = doctor.summarize(checks)
    if not (fail or warn or unknown):
        console.cprint("ok", "No blocking problems detected.")
    elif warn and not fail:
        console.cprint("warn", "Nothing failed outright — the ⚠ lines above are what "
                       "to fix first.")
    elif unknown and not fail:
        console.cprint("warn", "Some checks couldn't be verified (the [ ? ] lines "
                       "above); the rest look OK.")


def warn_old_kernel(release: str | None = None) -> None:
    """End-of-run hint: an old kernel series can mis-configure the speaker
    path no matter how good the preset is — issue #33 was fixed by a
    kernel upgrade, not a preset change. Silent unless the running series is
    older than _KERNEL_OLD_MONTHS. Mirrors warn_ee_environment."""
    if release is None:
        release = platform.release()
    if kernel_age_status(release).status != DOCTOR_WARN:
        return
    series = parse_kernel_series(release)
    aged = _kernel_series_age(series, date.today()) if series else None
    sstr = f"{series[0]}.{series[1]}" if series else release
    when = f" (released {aged[0]}, ~{aged[1]} months ago)" if aged else ""

    console.cprint("warn", f"\n⚠  Your kernel series {sstr} is old{when}.")
    console._cprint_wrapped("dim", kernel_old_message())
