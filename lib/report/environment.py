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
collapsed, as `base_display`. The constants come in under bare names because
string constants and a record type hold no state a patch would have to reach.
`BYPASS_PRESET_NAME` arrives the same way, from `lib/preset/autoload.py` —
the empty preset written there is
one this doctor has to recognise, because having it selected is itself a
"sounds like nothing" cause.
"""

from __future__ import annotations

import math
import platform
import re
from dataclasses import dataclass, field
from datetime import date

from lib import console, doctor, packages
from lib.data import kernel_releases
from lib.doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from lib.hardware import speakers
from lib.preset.autoload import (
    BYPASS_PRESET_NAME,
    GENERATOR_PREFIX,
    kernel_belongs_to,
)
from lib.preset.bands import SAMPLE_RATE
from lib.report.findings import Finding


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
                      found: bool, silent: str | None = None,
                      install_steps: tuple[tuple[str, str], ...] = ()
                      ) -> CheckResult:
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
        # The prose stops at "install 8"; which command that is depends on
        # what this machine's package manager would actually give, so the
        # caller supplies it — and it rides in `steps`, where a command is
        # printed as written instead of being wrapped into unusability.
        return CheckResult(DOCTOR_FAIL, "EasyEffects version",
            f"{vstr} detected — these presets need EasyEffects 8. "
            + ee_v7_message(vstr) + " Install EasyEffects 8:",
            steps=install_steps)
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


def _parse_graph_rate(text: str) -> int | None:
    """`clock.rate` as a positive int, or None when it isn't one.

    Not defensive noise: `session.read_settings` reports `ok` when *any* key
    parsed and fills the rest with `""`, so a readable probe can still carry no
    rate. An unreadable rate has to skip the check — the standing rule in this
    report is that an unknown value renders its reason and never reads as zero.
    """
    try:
        rate = int(text)
    except (TypeError, ValueError):
        return None
    return rate if rate > 0 else None


# Below this the sentence would round to "0 dB more" and still call it a
# problem. It is the same judgement the gate already makes for 44.1 kHz
# (-0.74 dB, negligible and in the quieter direction), applied upward: 1 dB is
# reached at ~53.9 kHz, below every rate real hardware offers (88.2, 96, 176.4,
# 192, 384) and above every rounding artefact. Load-bearing — without it
# `clock.force-rate 50000` prints "about 0 dB more" as a fault.
_GRAPH_RATE_MIN_DB = 1.0


def graph_rate_gain_db(rate: int) -> float:
    """dB the preset runs hot on a graph running at *rate*.

    EasyEffects resamples the convolver kernel to the server rate and
    compensates no gain for the longer filter, so the error is the rate ratio:
    +6.02 dB at 96 kHz, +12.04 dB at 192 kHz. Measured on the dev box at both
    (+5.9 and +11.8), and isolated to the convolver — bypassing it drops the
    rate-dependence to -0.4 dB (docs/design-notes.md). Computed rather than
    tabulated so the sentence stays true at any rate a user can reach.
    """
    return 20.0 * math.log10(rate / SAMPLE_RATE)


def _effective_graph_rate(running_rate: int, settings_rate: str,
                          forced_rate: str = "0") -> tuple[int, str] | None:
    """(rate, verb) for the graph, or None when no rate could be read.

    Three sources in falling order of authority, because no one of them is
    always there:

    1. the rate the driver actually ran at — issue #84 asked for 384000 and
       ran 192000, and the error follows what ran;
    2. `clock.force-rate`, which **pins the graph without changing
       `clock.rate`** — so a machine with a forced rate and nothing playing
       during the probe would otherwise read its untouched session default and
       be told nothing, while the graph runs forced the moment audio starts;
    3. the session default, for the ordinary unforced case.

    The verb travels with the rate because the three are not interchangeable
    and the sentence must not claim the graph *ran* at a rate only read from
    settings.
    """
    if running_rate > 0:
        return running_rate, "runs at"
    forced = _parse_graph_rate(forced_rate)
    if forced is not None:
        return forced, "is pinned to"
    parsed = _parse_graph_rate(settings_rate)
    return (parsed, "is set to") if parsed is not None else None


def graph_rate_message(rate: int, verb: str = "runs at") -> str:
    """Why a graph above `SAMPLE_RATE` makes the preset wrong, shared by
    --doctor, the end-of-run detail and the closing ask so the three can't
    drift.

    Deliberately carries **no command**. The rate is a session-wide setting
    this tool doesn't own, and we cannot know why it is set — someone running
    an external DAC chose it on purpose, and a one-liner here would talk them
    out of their own configuration. So the sentence says the change is
    testable for one session, which is what makes it safe to try, and leaves
    where-to-make-it-permanent to the reader, who knows how it got set.
    """
    # "about" only on the arm that read the rate the driver actually ran at.
    # A *requested* rate is an upper bound: issue #84 asked for 384000 and its
    # codec capped at 192000, where the error is 11.8 dB, not the 18 dB the
    # request implies — quoting "about" there would be wrong by 6 dB on the
    # very device this check was written for. /user-review then found the
    # hedge unusable on its own ("I'd take the number as gospel anyway"), so
    # the unmeasured arms name the one command that settles it: --doctor pays
    # the pw-top window this path skips, and reports what the driver reached.
    ran = verb == "runs at"
    hedge = ("" if ran else
             " — that is what the session asks for, and your hardware may run "
             "slower; --doctor, with audio playing, reports the rate it "
             "actually reaches")
    return (f"your PipeWire graph {verb} {rate} Hz and these presets are "
            f"built at {SAMPLE_RATE} Hz. EasyEffects stretches the correction "
            "filter to match the graph without compensating its gain, so the "
            f"stages after it see {'about' if ran else 'up to'} "
            f"{graph_rate_gain_db(rate):.0f} dB more than the tuning "
            f"intends{hedge}. If it sounds distorted, that is the first thing "
            "to rule out. Making the change permanent depends on where the "
            "rate was set in the first place; if you chose it for other "
            "hardware, this tool's other script (ee_to_pipewire.py) builds a "
            "version of the same tuning that isn't affected.")


def graph_rate_steps(forced_rate: str = "0") -> tuple[tuple[str, str], ...]:
    """The session-only test, as `steps` — printed verbatim, because a command
    folded across two lines is not runnable (`lib/doctor.py`, `emit_check`).

    Safe to hand over precisely because it is temporary: `clock.force-rate`
    lives in PipeWire's runtime metadata, not in a config file, so it is gone
    on the next daemon restart and cannot overwrite a rate someone chose on
    purpose. That is what makes naming it consistent with giving no permanent
    fix — the permanent one depends on how the rate got set, which only the
    reader knows.

    The undo restores what was there rather than clearing to 0: on a machine
    whose rate was *already* pinned, `0` would silently drop the reader's own
    setting instead of putting it back.
    """
    previous = _parse_graph_rate(forced_rate)
    undo = str(previous) if previous else "0"
    return (("dim", "Try it for this session — this adds a temporary "
                    "override, gone when PipeWire restarts, and changes "
                    "nothing saved:"),
            ("cta", f"  pw-metadata -n settings 0 clock.force-rate {SAMPLE_RATE}"),
            ("dim", "Undo without waiting for a restart:"),
            ("cta", f"  pw-metadata -n settings 0 clock.force-rate {undo}"))


def graph_rate_status(running_rate: int, settings_rate: str,
                      forced_rate: str = "0") -> CheckResult | None:
    """WARN when the graph runs above the rate the preset is built at.

    No PASS arm: a graph at the right rate is the ordinary case and saying so
    is noise (the same reason `firmware_gate_status` returns None). WARN rather
    than FAIL because the audio does reach the user — wrong, but audible, which
    is the line `lib/doctor.py` draws.

    The gate is `>`, not `!=`: 44.1 kHz lands at -0.74 dB, negligible and in
    the quieter direction, and a user playing 44.1 kHz material on a 44.1 kHz
    graph is doing the right thing.

    Three sources, in falling order of authority, because no one of them is
    always present:

    1. the rate the driver actually ran at — issue #84 asked for 384000 and
       ran 192000, and the error follows what ran;
    2. `clock.force-rate`, which **pins the graph without changing
       `clock.rate`** — so a machine with a forced rate and nothing playing
       during the probe would otherwise read its untouched session default and
       be told nothing, while the graph runs forced the moment audio starts;
    3. the session default, for the ordinary unforced case.
    """
    resolved = _effective_graph_rate(running_rate, settings_rate, forced_rate)
    if resolved is None:
        return None
    rate, verb = resolved
    if graph_rate_gain_db(rate) < _GRAPH_RATE_MIN_DB:
        return None
    return CheckResult(DOCTOR_WARN, "Graph sample rate",
                       graph_rate_message(rate, verb),
                       steps=graph_rate_steps(forced_rate))


def graph_rate_finding(running_rate: int, settings_rate: str,
                       forced_rate: str = "0") -> "Finding | None":
    """The same condition as a `Finding`, for a normal run.

    A `Finding` rather than a bare print, unlike `warn_old_kernel` next door,
    and the difference is what the two are claiming: an old kernel *may* be
    mis-configuring the speaker path, while this is a measured error in the
    preset the run just wrote. /user-review round 12 caught the cost of
    getting that wrong — printed inline only, it had scrolled off by the time
    the run finished, so the reader's last screen was a clean "Done" and the
    12 dB went unmentioned. The ask puts one line in the closing block, which
    also lands it above the `--disable` menu whose "loud parts distort" entry
    would otherwise be the only thing a distorting user is offered.
    """
    resolved = _effective_graph_rate(running_rate, settings_rate, forced_rate)
    if resolved is None or graph_rate_gain_db(resolved[0]) < _GRAPH_RATE_MIN_DB:
        return None
    rate, verb = resolved
    return Finding(
        slug="preset-plays-hot", kind="hint",
        detail=graph_rate_message(rate, verb),
        # "up to" on the same arms as the detail: an unmeasured rate is an
        # upper bound, and this is the half that survives to the closing block.
        ask=f"Put your PipeWire graph back to {SAMPLE_RATE} Hz — at {rate} Hz "
            f"this preset is fed {'' if verb == 'runs at' else 'up to '}"
            f"{graph_rate_gain_db(rate):.0f} dB hotter than it is built for.")


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


def is_generated_preset(preset_json, preset_name: str) -> bool:
    """Whether a preset file in EasyEffects' folder is one this tool wrote.

    Two signals, either of which is enough: the ``_generator`` stamp every
    preset this tool writes carries at top level, or a speaker-correction
    convolver whose impulse is named after the preset the way
    ``lib.preset.emit.kernel_name`` names it. Each alone has a blind spot —
    a preset EasyEffects re-saved from its own window may not keep the
    stamp, and a preset whose impulse file has gone still has to be
    recognised so the check that reports the missing file can run — so it
    is an OR, and the impulse name is matched without asking whether the
    file exists. A preset EasyEffects re-saved under another name matches
    neither and counts as theirs (a plain file copy keeps the stamp and
    counts as ours); so does anything that isn't a dict.

    The user's own presets (an AutoEq headphone preset, say) live in the same
    folder, and judging them by this tool's standards is how issue #84's
    report told its reader to fix two files it never wrote.
    """
    if not isinstance(preset_json, dict):
        return False
    if str(preset_json.get("_generator", "")).startswith(GENERATOR_PREFIX):
        return True
    output = preset_json.get("output")
    if not isinstance(output, dict):
        return False
    return any(
        _CONVOLVER_KEY_RE.match(key) and isinstance(block, dict)
        and kernel_belongs_to(preset_name, str(block.get("kernel-name", "")))
        for key, block in output.items())


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


def loaded_preset_status(rc_data: dict, generated_names,
                         live_preset: str | None = None,
                         output_kind: str = "unknown",
                         speaker_preset: str = "") -> CheckResult:
    """Whether EasyEffects' selected output preset is one this script generated.
    Reports last-loaded / fallback without over-claiming which is *active*
    (per-device autoloading lives elsewhere in EE's config). The empty
    ``Nothing`` bypass preset is excluded from "generated": having it selected
    is itself a "sounds like nothing" cause, not a healthy state.

    ``live_preset`` is the running daemon's own answer when we got one, and
    then it is authoritative: the fallback key is skipped, because what EE
    reports *is* the outcome autoloading already arrived at. Without it this
    check reads a file EE may not have written for hours — which is how it
    came to report the silent bypass preset while a Dolby one was loaded.

    ``output_kind`` is `lib.hardware.sinks.sink_kind` on the output EasyEffects
    is using, and ``speaker_preset`` the preset an autoload entry maps the
    internal speakers to (both resolved by the caller, so this stays a pure
    verdict). Together they separate the one case where the bypass preset is
    the *designed* state from the one where it is the fault it looks like."""
    dolby = {n for n in generated_names if n != BYPASS_PRESET_NAME}
    # A real name to point at, never "Dolby-*": glob shorthand asks a
    # non-terminal reader to type an asterisk, and under --prefix the
    # presets aren't named Dolby-* at all (/user-review 2026-08-30).
    example = f"'{sorted(dolby)[0]}'" if dolby else "one of this tool's presets"
    loaded = rc_data.get("last_output_preset", "") if live_preset is None \
        else live_preset
    fallback = "" if live_preset is not None \
        else rc_data.get("fallback_preset", "")
    if not loaded and not fallback:
        return CheckResult(DOCTOR_WARN, "Selected preset",
            f"EasyEffects has no output preset recorded yet — open it and "
            f"load {example} for the speakers.")
    if loaded == BYPASS_PRESET_NAME:
        # On a non-speaker output this is the state --autoload deliberately
        # installs: it writes this empty preset and points EasyEffects' global
        # fallback at it so HDMI/Bluetooth/USB stop applying a speaker tuning.
        # Warning there would flag our own design as a fault and send the
        # reader to put a speaker tuning on their headset — which the run
        # itself refuses to do (lib/preset/reload.py).
        #
        # `== "other"`, not `!= "speaker"`: only a *confident* non-speaker
        # softens this. sink_kind's "unknown" covers a failed probe, a
        # disconnected sink and a virtual one, and none of those are evidence
        # the speakers are fine. The qualifier is load-bearing — widening it
        # to "not a speaker" is how this check would go quiet on the very
        # machines it exists for.
        if output_kind == "other":
            if speaker_preset in dolby:
                return CheckResult(DOCTOR_PASS, "Selected preset",
                    f"'{BYPASS_PRESET_NAME}' is the expected bypass while the "
                    "output isn't the internal speakers. The speakers autoload "
                    f"'{speaker_preset}'.")
            # Never open a sentence with the bare word "Nothing" here: it is
            # this project's preset name, quoted three words earlier, and a
            # first-time reader parsed "Nothing autoloads a Dolby-* preset"
            # as a claim about that preset (/user-review 2026-08-29).
            return CheckResult(DOCTOR_UNKNOWN, "Selected preset",
                "the output isn't the internal speakers, so the silent "
                f"'{BYPASS_PRESET_NAME}' bypass preset is expected here. None "
                "of this tool's presets is set to load on the speakers either "
                "(--autoload sets that up), so what they would play couldn't "
                "be checked — switch the system output back to the speakers "
                "and re-run this to check it.")
        return CheckResult(DOCTOR_WARN, "Selected preset",
            f"the silent '{BYPASS_PRESET_NAME}' bypass preset is selected — that's "
            f"no processing by design. Load {example} in EasyEffects.")
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


def ee_unanswered_status(names) -> CheckResult:
    """EasyEffects is listening but didn't answer what we asked it.

    Its local socket is a documented interface (upstream's "Local Server"
    page), but the page promises nothing about compatibility, the shape has
    already changed twice (the pipeline argument in 8.0.7, the socket path
    in 8.0.9), and one of our two requests — get_global_bypass — is only in
    EE's source, not on the page. So a request it stops recognising is a real
    possibility. Saying so is the whole point: the alternative is falling back
    to its config file in silence and reporting hours-old values as current
    for as long as nobody notices. The values are still shown, marked as
    coming from that file. Provenance: docs/design-notes.md, "Rejected
    approaches"."""
    # "a usable answer": a reply we can't parse lands here too, and that is
    # not silence. And only the rows this list names fall back — the sink row
    # is read from PipeWire, so "Values below" called the whole block stale.
    joined = " and ".join(names)
    tail = "values below come" if len(names) > 1 else "value below comes"
    return CheckResult(DOCTOR_UNKNOWN, "EasyEffects state",
        f"EasyEffects is running but didn't give a usable answer when asked "
        f"its {joined} — this tool may be out of step with your EasyEffects "
        f"version. The {joined} {tail} from its config file, which it "
        "rewrites only on quit or while its window is open.")


def global_bypass_status() -> CheckResult:
    """Global bypass is on, so every preset is passthrough.

    FAIL, not WARN: none of this tool's output is reaching the speakers, which
    is the whole thing the reader came to check. As a WARN it sat under a
    "Nothing failed outright" verdict and a "0 FAIL" summary — reassuring
    headlines above the one line saying the audio is untouched.

    Raised only on a live reading from the running daemon — the config file's
    copy of this key is written on save, so a stale one would accuse a user
    whose audio is fine. No 'off' counterpart: a check that passes for the
    overwhelming majority is noise, and the Environment block states it."""
    return CheckResult(DOCTOR_FAIL, "Global bypass",
        "EasyEffects' global bypass is ON — every preset is passthrough, so "
        "nothing you load will change the sound. Turn it off with the "
        "power-button icon in EasyEffects' top bar.")


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


def _alsa_utils_step() -> tuple[tuple[str, str], ...]:
    """One line, not the full per-family hint.

    `alsa-utils` carries that name on every family the table knows bar
    Gentoo's category prefix, so a reader this run could not place loses
    almost nothing by being told the package instead of the command — where
    the seven-command fallback would cost them seven lines inside a
    diagnostic they are reading because something else is already wrong.
    And there is no README section about `amixer` to point at.
    """
    command = packages.install_command([packages.ALSA_UTILS],
                                       packages.family())
    return (("cta", command if command
             else "install your distribution's alsa-utils"),)


def firmware_gate_status(gates: list[speakers.FirmwareGate],
                        checked: bool = True) -> CheckResult | None:
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

    ``checked`` is False when amixer is absent, and then an empty ``gates``
    stops meaning "no such control". Returning None there would let the
    report's verdict say "no blocking problems" about a control nothing
    looked for — the one this section exists to catch.
    """
    if not gates:
        if checked:
            return None
        return CheckResult(
            DOCTOR_UNKNOWN, "Speaker firmware gate",
            "amixer isn't installed, so whether this machine has a smart-amp "
            "firmware gate — and whether it is switched on — couldn't be "
            "checked. Only some smart-amp machines have such a control; "
            "those may sound thin or silent until it is on.",
            steps=_alsa_utils_step())
    off = [g for g in gates if not g.on]
    if not off:
        return CheckResult(DOCTOR_PASS, "Speaker firmware gate",
                           "the amplifier is allowed to load its firmware.")
    names = ", ".join(g.name for g in off)
    return CheckResult(
        DOCTOR_WARN, "Speaker firmware gate",
        f"{names} is off, so the amplifier runs untuned after the preset "
        "and your speakers may be silent, thin or crackly whatever the preset "
        "does. Switch it on:",
        steps=tuple(("cta", speakers.amixer_enable_cmd(g)) for g in off))


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
