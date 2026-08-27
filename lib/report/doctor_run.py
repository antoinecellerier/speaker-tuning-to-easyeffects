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
PASS/WARN/FAIL/UNKNOWN, `CheckResult`, the summary counter, the check printer
and the `~`-collapsing path renderer — through `lib/doctor.py`, so they read as
one tool. The constants and `CheckResult` arrive under bare names because
string constants and a record type hold no state a patch would have to reach.

`speaker` keeps the alias the generator gave it (`report_speaker`) for the same
reason: the moved lines read through that name. In the generator it is one
letter from `lib.hardware.speakers` — a hazard that does not exist here, and the
name is kept anyway because renaming it would cost the provenance of every line
that uses it.

The report's frame — the order of its sections and the text around them — is
`lib/report/doctor_layout.py`, shared with the PipeWire doctor so the two cannot
drift. What is left here is this side's own two builders and its probes.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from lib import console, doctor, ee_paths, ee_socket, packages, version
from lib.doctor import (
    DOCTOR_FAIL,
    DOCTOR_PASS,
    DOCTOR_UNKNOWN,
    DOCTOR_WARN,
    CheckResult,
)
from lib.preset import autoload
from lib.report import doctor_layout as layout
from lib.report import environment
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
    EE is restarted — EE reads the file on startup and rewrites it from
    ``saveAll()``, which runs on quit *and* on a 30 s autosave timer that
    only ticks while its window is open, so mid-run writes get clobbered.
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


def _live_default_sink() -> str:
    """node.name of the sink PipeWire is sending output to now, or "".

    Imported inside the function so the EasyEffects path doesn't drag in the
    PipeWire checks module (which imports back into lib/report/) on the runs
    that never need it.
    """
    from lib.pipewire import checks
    return checks.default_sinks(checks._pw_dump()).effective


def _sink_is_internal_speaker(name: str) -> bool:
    """Does PipeWire call this sink one of the machine's own speakers?

    Reuses the generator's own classifier rather than matching on the node
    name, so the doctor and `--autoload` agree about what a speaker is —
    including the relaxed tier for laptops whose UCM2 profile omits the
    speaker icon (issue #18). Only asked to decide whether to keep a closing
    bullet, so any failure answers "don't know" and the bullet stays.
    """
    from lib.hardware import sinks
    try:
        return any(s.get("name") == name
                   for s in sinks.select_speaker_sinks()["selected"])
    except (OSError, KeyError, TypeError):
        return False


# EasyEffects' daemon listens on a QLocalServer of this name and answers
# newline-terminated ASCII requests — its documented "Local Server"
# (https://wwmm.github.io/easyeffects/user_interface/local_server.html, since
# EE 8.0.7; the tags are upstream's src/tags_local_server.hpp). Of our two
# requests get_last_loaded_preset is on that page; get_global_bypass is
# source-only — it is what `easyeffects -b 3` itself sends. Only these two are
# ever sent: the same socket also takes quit_app, hide_window, show_window,
# load_preset, global_bypass, toggle_global_bypass and set_property, none of
# which a diagnostic may send. Naming the allowed set means a later edit
# cannot reach a mutating request by passing a different string. Why the
# socket and not the CLI, and the version history: docs/design-notes.md,
# "Rejected approaches".
_EE_PRESET_REQUEST = "get_last_loaded_preset:output\n"
_EE_BYPASS_REQUEST = "get_global_bypass\n"
_EE_READ_REQUESTS = frozenset({_EE_PRESET_REQUEST, _EE_BYPASS_REQUEST})


def _ee_query(request: str) -> ee_socket.EEReply:
    """Ask the running EasyEffects daemon over its local socket.

    The allowlist is the point of this wrapper: the transport itself is
    `lib/ee_socket.py`, and this is the one place a diagnostic reaches it.

    Deliberately NOT the `easyeffects` CLI, which looks like the obvious way
    to ask and has two side effects a diagnostic must not have:

    * It **hides the running instance's window.** Through EE 8.2.8 its
      argument parser emits `onHideWindow()` for these very queries, which
      the secondary instance forwards to the daemon as a `hide_window`
      message — asking what preset is loaded would close the window out from
      under whoever is reading it. Upstream 8942fbc39 (after 8.2.8) keeps
      that to `-a`'s failure branch, but `-b 3` still hides unconditionally.
    * With no daemon it becomes the *primary* instance and starts a whole
      second EasyEffects — upstream picks that branch purely on whether a lock
      file is held, and under Flatpak that lock lives in the sandbox's temp
      dir where a host-side client cannot see it.

    Talking to the socket ourselves avoids both: the daemon acts only on the
    request we send, and connecting to a socket cannot start anything. When
    EasyEffects isn't running the socket isn't there and we fall back to its
    config file, as we do for a Flatpak install whose socket sits inside the
    sandbox.
    """
    if request not in _EE_READ_REQUESTS:
        raise ValueError(f"refusing to send a non-read-only request: {request!r}")
    return ee_socket.query(request)


@dataclass
class LiveState:
    """EasyEffects state resolved from the best source available per value.

    Each value carries where it came from, because the report has to say so:
    a config-file reading can be arbitrarily old (see `read_ee_rc`), and one
    presented as current is how --doctor came to report the silent 'Nothing'
    preset while a Dolby one was loaded.
    """
    preset: str = ""
    preset_is_live: bool = False
    sink: str = ""
    sink_source: str = "saved"   # "live" | "pinned" | "saved"
    bypass: bool = False
    bypass_is_live: bool = False
    # Whether `sink` is one PipeWire calls an internal speaker. Only ever set
    # from a live reading, and only used to drop a closing bullet asking the
    # reader to confirm what we just printed.
    sink_is_speaker: bool = False
    # Requests a listening daemon did not answer. Non-empty means EasyEffects'
    # socket protocol changed, not that it is absent — reported rather than
    # absorbed, so this stops reporting stale values as current the moment it
    # happens instead of whenever someone next reads the source.
    unanswered: list[str] = field(default_factory=list)


def _resolve_live_state(rc: dict) -> LiveState:
    """Prefer the running daemon and the live graph; fall back to the rc."""
    state = LiveState()

    # An answered request wins even when the name is empty: that is EasyEffects
    # saying nothing is loaded, which its config file cannot distinguish from
    # never-written.
    reply = _ee_query(_EE_PRESET_REQUEST)
    if reply.answered:
        state.preset, state.preset_is_live = reply.value, True
    else:
        state.preset = rc.get("last_output_preset", "")
        if reply.reached:
            state.unanswered.append("loaded preset")

    # useDefaultOutputDevice defaults ON, in which case EE just follows the
    # system default sink and the rc holds a stale copy of it. Pinned is the
    # opposite: only the GUI writes that key, so the rc is then the truth and
    # the live default sink is the wrong answer.
    if rc.get("use_default_output_device", True):
        live_sink = _live_default_sink()
        if live_sink:
            state.sink, state.sink_source = live_sink, "live"
            state.sink_is_speaker = _sink_is_internal_speaker(live_sink)
        else:
            state.sink, state.sink_source = rc.get("output_device", ""), "saved"
    else:
        state.sink, state.sink_source = rc.get("output_device", ""), "pinned"

    # The daemon answers exactly 1 (on) or 2 (off). Parsing strictly means a
    # changed reply format degrades to the config copy instead of being read
    # as a confident "off" — and, since we got *an* answer, counts as drift.
    # The rc copy is only ever a display fallback, never a verdict.
    bypass_reply = _ee_query(_EE_BYPASS_REQUEST)
    if bypass_reply.value in ("1", "2"):
        state.bypass, state.bypass_is_live = bypass_reply.value == "1", True
    else:
        state.bypass = rc.get("bypass", False)
        if bypass_reply.reached:
            state.unanswered.append("global bypass")

    return state


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


# A version anywhere on a package manager's answer, and the labels that mark
# the line worth reading. `apt-cache policy` prints Installed *and* Candidate
# and only the second says what an install would get; `pacman -Si` and
# `zypper info` label theirs "Version". A `dnf repoquery --qf` prints the bare
# number with no label at all, which is why an unlabelled line still counts.
_VERSION_TOKEN = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_CANDIDATE_LABELS = ("candidate", "version")
# A whole line that *is* a version, allowing a packaging suffix: `apk policy`
# lists each available version as its own heading, so "8.2.8-r0:" arrives
# looking like a label whose name happens to be the answer. Anchored, not
# searched, so a line merely mentioning a number is not mistaken for one.
_VERSION_LINE = re.compile(r"[\d.]+(?:[-_+~][\w.]+)*")


def _distro_easyeffects_major(fam: str) -> int | None:
    """The major version this distribution would install, or None.

    None for every way of not knowing — no query for this family, the tool
    absent, a non-zero exit, a timeout, an answer we can't read — and callers
    treat all of them the same way, because the remedy that doesn't depend on
    the distribution is right in every one of them.

    Asked rather than tabulated: which release ships EasyEffects 8 changes
    every few months, and a stale table here names a package that installs
    7.x, loads the preset and silently does almost nothing — exactly what the
    check that calls this exists to catch.
    """
    argv = packages.available_version_cmd(packages.EASYEFFECTS, fam)
    if not argv:
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    for line in (proc.stdout or "").splitlines():
        label, sep, rest = line.partition(":")
        if not sep:
            # `dnf --qf` prints the bare number with no label at all.
            answer = line
        elif label.strip().lower() in _CANDIDATE_LABELS:
            # apt's "Candidate", pacman's and zypper's "Version" — the label
            # that means "what an install would get". apt prints "Installed"
            # too, and taking that one would read a 7.x already on the machine
            # as what the distribution ships.
            answer = rest
        elif _VERSION_LINE.fullmatch(label.strip()):
            # apk's own heading for an available version.
            answer = label
        else:
            continue
        m = _VERSION_TOKEN.search(answer)
        if m:
            return int(m.group(1))
    return None


def easyeffects_install_steps() -> tuple[tuple[str, str], ...]:
    """How to get EasyEffects 8, for this machine — the distro's own package
    when the distro actually ships 8, and the Flatpak otherwise.

    Both, never one: the Flatpak works everywhere and is the answer when we
    cannot place the machine or cannot ask it, and a distro package that ships
    7.x is worse than no suggestion at all — it installs cleanly, loads the
    preset, and leaves the speaker-correction filter doing nothing.
    """
    fam = packages.family()
    major = _distro_easyeffects_major(fam) if fam else None
    steps: list[tuple[str, str]] = []
    if (major or 0) >= 8:
        command = packages.install_command([packages.EASYEFFECTS], fam)
        # NixOS answers with a configuration edit and a rebuild rather than a
        # command, so the bullet takes whichever of the two that family has.
        native = ([("cta", f"      {command}")] if command
                  else list(packages.install_steps([packages.EASYEFFECTS],
                                                   indent="      ")))
        if native:
            # The version, because this bullet is the one that could be
            # wrong: a reader whose distribution shipped 7 needs to see that
            # this run actually asked, not that it guessed.
            steps.append(("cta", "  • from your distribution, which has "
                                 f"EasyEffects {major}:"))
            steps.extend(native)
    # Labelled rather than listed, so two commands read as a choice instead of
    # a procedure — a bulleted caption above each keeps the command alone on
    # its line, which is what makes it pasteable.
    steps.append(("cta", "  • or the Flathub Flatpak, which works anywhere:"
                         if steps else "  • the Flathub Flatpak:"))
    steps.append(("cta", "      flatpak install flathub "
                         "com.github.wwmm.easyeffects"))
    # The Flatpak is only "works anywhere" once Flatpak itself is set up, and
    # on a plain install of most distributions the Flathub remote is not
    # there. Named, not linked — the one-link rule keeps URLs out of message
    # bodies (.claude/rules/user-messages.md) — but a reader who hits
    # "remote flathub not found" now knows it isn't this tool's fault.
    steps.append(("dim", "        (needs Flatpak installed and the Flathub "
                         "remote added)"))
    if len(steps) == 3:
        # Which of the two it was, rather than both at once: "older than 8, or
        # couldn't be checked" leaves a reader unable to tell whether an
        # upgrade of their own distribution would fix it.
        steps.append(("dim", f"    (your distribution ships EasyEffects "
                             f"{major}, which these presets can't use)"
                             if major else
                             "    (couldn't ask your package manager what it "
                             "would install)"))
    return tuple(steps)


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
        ee_version, found, silent = probe()
        src = "flatpak info" if is_flatpak else "easyeffects --version"
        if not found:
            if silent and fallback.silent is None:
                fallback.silent = silent
                fallback.source = src
            continue
        if ee_version is not None:
            return EEProbe(ee_version, True, src, is_flatpak)
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
    # `ee_version`, not `version`: this module imports `lib.version`, and
    # `_print_doctor_report` below calls `version.get_version()` for the tool's
    # own version. A local of that name reads identically and means the other
    # thing entirely.
    ee_version, found, source, ee_is_flatpak = (
        probe.version, probe.found, probe.source, probe.is_flatpak)
    report.checks.append(
        # Built only for the branch that prints it: the offer costs a
        # package-manager query, and the version this run already read is
        # enough to know whether anyone will read the answer.
        environment.ee_version_status(
            ee_version, found, probe.silent,
            easyeffects_install_steps()
            if ee_version and ee_version[0] < 8 else ()))

    # 2. Install location (skip the EE-location verdict for custom dirs)
    if custom_dirs:
        # UNKNOWN, not PASS: the run skipped the location checks, and a green
        # box said the location was fine when nothing had looked at it.
        report.checks.append(CheckResult(DOCTOR_UNKNOWN, "Install location",
            f"custom output dir ({doctor.tilde(output_dir)}) — skipping EasyEffects "
            "location checks."))
    else:
        report.checks.append(environment.install_status(
            ee_paths.FLATPAK_BASE.exists(), ee_paths.NATIVE_BASE.exists(), ee_paths.USE_FLATPAK,
            doctor.tilde(ee_paths.EASYEFFECTS_BASE), ee_is_flatpak))

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
    bypass_present = any(p.stem == autoload.BYPASS_PRESET_NAME
                         for p in preset_paths)
    if not dolby_presets:
        # The bypass preset is one this tool wrote, so "no presets found" in a
        # folder holding it reads as the doctor missing a file it can see.
        found = ("no presets other than the bypass preset found in"
                 if bypass_present else "no presets found in")
        report.checks.append(CheckResult(DOCTOR_WARN, "Generated presets",
            f"{found} {doctor.tilde(output_dir)} — run the script on your "
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
    live = _resolve_live_state(rc)
    # The selected-preset check compares against presets in output_dir; that's
    # only meaningful when output_dir is where EE actually loads from (default
    # dirs). Under custom dirs, surface the loaded preset as a fact instead.
    # A live answer runs the check even with no rc at all — the daemon knows
    # what it loaded whether or not it has got round to writing it down.
    if (rc_text or live.preset_is_live) and not custom_dirs:
        report.checks.append(environment.loaded_preset_status(
            rc, generated_names,
            live_preset=live.preset if live.preset_is_live else None))
    # Global bypass silences the whole chain, and it is the first thing to
    # suspect behind "I hear no difference". Only a live reading may raise it:
    # the rc copy predates any GUI toggle since EE last saved.
    if live.bypass_is_live and live.bypass:
        report.checks.append(environment.global_bypass_status())
    # A listening daemon that ignored our request means its protocol moved,
    # not that it is absent — surfaced so the fallback below can't quietly
    # become permanent.
    if live.unanswered:
        report.checks.append(environment.ee_unanswered_status(live.unanswered))
    # Background-service / autostart is install-global, not output-dir-specific,
    # so it runs even under custom dirs (unlike the selected-preset check).
    if rc_text:
        report.checks.append(environment.autostart_status(rc))

    # 5. Hardware / codec context (folds in --speaker-info)
    report.speaker_info = report_speaker._gather_speaker_info()

    # 6. Smart-amp firmware gate — upstream of the whole preset (issue #17)
    gate_check = environment.firmware_gate_status(
        report.speaker_info.firmware_gates,
        report.speaker_info.firmware_gates_checked)
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
        "ee_version": (".".join(map(str, ee_version)) if ee_version
                       else "unknown")
                      + (f" (via {source})" if source else ""),
        "ee_running": easyeffects_is_running(),
        "install": "Flatpak" if ee_paths.USE_FLATPAK else "native",
        "output_dir": doctor.tilde(output_dir),
        "irs_dir": doctor.tilde(irs_dir),
        "preset_count": len(generated_names),
        "bypass_preset_present": bypass_present,
        "irs_count": len(irs_stems),
        "rc_path": doctor.tilde(rc_path),
        "rc_present": bool(rc_text),
        "selected_preset": live.preset or (""  if live.preset_is_live
                                           else rc.get("fallback_preset", "")),
        "selected_is_live": live.preset_is_live,
        "autostart_on_login": rc.get("autostart_on_login", False),
        "service_mode": rc.get("service_mode", True),
        "output_device": live.sink,
        "output_device_source": live.sink_source,
        "output_is_speaker": live.sink_is_speaker,
        "output_plugins": rc.get("output_plugins", []),
        "bypass": live.bypass,
        "bypass_is_live": live.bypass_is_live,
    }
    return report


def _environment_lines(f: dict) -> list[str]:
    """The `=== Environment ===` body: where this tool wrote, and what
    EasyEffects is doing with it. Labels pad to a 16-column gutter so the
    values line up. The last three rows appear only once there is one."""
    # Rows below come from whichever source is authoritative for that value,
    # so a row EasyEffects could have answered live but didn't says where it
    # came from instead. Only worth saying while EE is running: with no daemon
    # every row is necessarily the last save, and the Config: line says so
    # once rather than repeating it down the block.
    # A row EasyEffects could have answered live but didn't says where it came
    # from — including when EE isn't running at all. Stating it once up top
    # instead was worse: the rows then read identically whether or not the
    # value was confirmed, and the closing block's bypass reminder (which is
    # dropped only on a live reading) looked arbitrary next to a `Bypass:` line
    # that looked equally sure of itself either way.
    saved = " (from saved config)"
    running = f.get("ee_running")
    lines = [
        f"  Tool:         speaker-tuning-to-easyeffects {version.get_version()}",
        f"  EasyEffects:  {f.get('ee_version', '?')}; "
        f"running: {'yes' if running else 'no'}",
        f"  Install:      {f.get('install')} (writes to {f.get('output_dir')})",
        # Both numbers are what the folders hold — the bypass preset, presets
        # the user put there and stray .irs files included — so neither is
        # derived from the other. "Presets sharing impulse files" explained
        # the gap with a relationship these counts don't establish.
        f"  Presets/IRs:  {f.get('preset_count', 0)} preset files and "
        f"{f.get('irs_count', 0)} impulse files in the folders",
        f"  Config:       {f.get('rc_path')} "
        f"({'present' if f.get('rc_present') else 'absent'})",
    ]
    lines.append(
        f"  Background:   service mode "
        f"{'on' if f.get('service_mode') else 'off'}, autostart "
        f"{'on' if f.get('autostart_on_login') else 'off'}")
    if f.get("selected_preset"):
        lines.append(f"  Selected:     {f['selected_preset']}"
                     + ("" if f.get("selected_is_live") else saved))
    if f.get("output_device"):
        # Whichever sink this names can be a Bluetooth one — the live default
        # follows the headset on connect exactly as EE's own record did — so
        # this stays the one redacted node name on a path the issue form asks
        # for whole.
        source = {"live": "", "pinned": " (pinned in EasyEffects)"}.get(
            f.get("output_device_source", "saved"), saved)
        lines.append("  Output sink:  "
                     + doctor.no_bt_address(f['output_device']) + source)
    # No live source exists for the chain, so it is always the saved copy —
    # worth marking next to rows that aren't. Wrapped because a full chain is
    # seven plugin names and ran to ~145 columns on one line; continuations
    # land on the same 16-column gutter as the values above. break_on_hyphens
    # is off for the same reason `_cprint_wrapped` turns it off — a plugin name
    # split across lines stops being greppable.
    if f.get("output_plugins"):
        width = console._wrap_width()
        chain = textwrap.wrap(
            ", ".join(f["output_plugins"]),
            width=width, break_on_hyphens=False,
            initial_indent="  Active chain: ", subsequent_indent=" " * 16)
        # The marker is appended after wrapping, not wrapped with the list:
        # split across lines it reads as part of the last plugin name
        # ("limiter#0 (from" / "saved config)").
        if len(chain[-1]) + len(saved) <= width:
            chain[-1] += saved
        else:
            chain.append(" " * 16 + saved.strip())
        lines += chain
    # Prints even when off: "is it bypassed?" is the first question behind
    # "I hear no difference", and a positive "off" answers it. Label is
    # `Bypass:` so the value still lands on the 16-column gutter.
    if f.get("bypass_is_live") or f.get("rc_present"):
        lines.append(f"  Bypass:       {'on' if f.get('bypass') else 'off'}"
                     + ("" if f.get("bypass_is_live") else saved))
    return lines


def _collapse_preset_checks(checks: list[CheckResult], *,
                           bypass_present: bool = False) -> list[CheckResult]:
    """Fold a run of passing per-preset checks into one line.

    A machine can have dozens of profiles, and a screenful of identical PASS
    lines buries everything else; any preset with a problem is still listed
    individually, in place of the run. The collapsed line is a `CheckResult`
    carrying no detail, so it renders through the same printer as every other
    check rather than a hand-built copy of its format.

    Display only — the summary counts the originals (`print_check_block`'s
    ``counted``), so the PASS total still says how many presets were read.
    """
    presets = [c for c in checks if c.label.startswith("Preset ")]
    problems = [c for c in presets if c.status != DOCTOR_PASS]
    passing = len(presets) - len(problems)

    shown: list[CheckResult] = []
    folded = False
    for c in checks:
        if not c.label.startswith("Preset "):
            shown.append(c)
        elif not folded:
            folded = True
            if passing:
                # The detail reconciles two numbers readers compared and
                # distrusted: this denominator is one short of the preset count
                # above (the bypass preset carries no filters, so there is
                # nothing to check), and the summary's PASS total is mostly
                # these, counted individually but shown as one line.
                #
                # "checked out", not "load their impulse file": a preset can
                # fail this check for reasons that have nothing to do with an
                # impulse file — including having no convolver at all.
                detail = f"{passing} checks on one line."
                if bypass_present:
                    detail += (f" The '{autoload.BYPASS_PRESET_NAME}' bypass "
                               "preset isn't among them — it has no filters "
                               "by design.")
                shown.append(CheckResult(
                    DOCTOR_PASS,
                    f"Presets ({passing}/{len(presets)} checked out)",
                    detail))
            shown += problems
    return shown


def _print_doctor_report(report: environment.DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    layout.print_report_header(version.get_version())
    if report.speaker_info is not None:
        report_speaker._print_speaker_info(report.speaker_info)
    layout.print_environment(_environment_lines(report.facts))
    layout.print_check_block("=== EasyEffects doctor ===",
                             _collapse_preset_checks(
                                 report.checks,
                                 bypass_present=report.facts.get(
                                     "bypass_preset_present", False)),
                             counted=report.checks)
    # What the doctor can't see — guide the user through the manual checks.
    # The bypass line drops out once we have asked the daemon and it said off:
    # sending someone to verify a setting we just read is how a closing block
    # teaches people to skip it. When bypass is genuinely on, a check says so
    # above and this stops being the place it's raised.
    closing = [
        ("dim", "If you still hear no difference between the preset and bypass:"),
        ("dim", "  • In EasyEffects, toggle the preset off/on to A/B it."),
    ]
    if not report.facts.get("bypass_is_live"):
        closing.append(
            ("dim", "  • Make sure global bypass (the power-button icon, top bar) is OFF."))
    # Same rule for the sink half: we print which output PipeWire is using, and
    # when that is one of the machine's own speakers there is nothing left for
    # the reader to confirm. The volume half always stays — no level we read
    # tells us what the reader can hear.
    closing.append(
        ("dim", "  • Confirm the volume is up.")
        if report.facts.get("output_is_speaker")
        else ("dim", "  • Confirm system output is the speaker sink and volume is up."))
    layout.print_closing(tuple(closing))


def _uses_custom_ee_dirs(args) -> bool:
    """Did the run write somewhere other than EasyEffects' own tree?

    *Either* dir moved counts, so every check that keys on this agrees about
    what "custom" means: --doctor skips the EE-location and selected-preset
    verdicts here, and the end-of-run install-mismatch warning fires only on
    its negation. Written once because the two read as De Morgan duals and
    an inverted hand-written copy would be silent.
    """
    return (args.output_dir != ee_paths.DEFAULT_OUTPUT_DIR
            or args.irs_dir != ee_paths.DEFAULT_IRS_DIR)


def report_doctor(args) -> None:
    """--doctor entry point: run environment self-diagnostics and print them."""
    report = _gather_doctor_report(args.output_dir, args.irs_dir,
                                   ee_paths.DEFAULT_EASYEFFECTS_RC,
                                   custom_dirs=_uses_custom_ee_dirs(args))
    _print_doctor_report(report)


def warn_ee_environment(args) -> None:
    """End-of-run check for a normal generation run: loudly warn if the
    installed EasyEffects can't use the presets we just wrote. Silent on the
    happy path. Reuses --doctor's probes; mirrors warn_speaker_firmware_gate."""
    probe = _probe_ee_version()
    ee_version, found, ee_is_flatpak = (
        probe.version, probe.found, probe.is_flatpak)
    ver = environment.ee_version_status(ee_version, found, probe.silent)

    if ver.status == DOCTOR_FAIL:
        vstr = ".".join(str(x) for x in ee_version)
        console.cprint("err", f"\n{'=' * 60}")
        console.cprint("err", f"⚠  EasyEffects {vstr} detected — these presets need EasyEffects 8.")
        print()
        console._cprint_wrapped("dim", environment.ee_v7_message(vstr))
        print()
        console.cprint("dim", "To fix, install EasyEffects 8:")
        # Was a hand-maintained list of which distros still shipped 7.x. That
        # sentence was true when written and had no way of staying true; the
        # machine's own package manager answers the same question and can't go
        # stale.
        for style, text in easyeffects_install_steps():
            console.cprint(style, text)
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
    if (not _uses_custom_ee_dirs(args)
            and ee_is_flatpak is not None and ee_is_flatpak != ee_paths.USE_FLATPAK):
        run_where = "Flatpak" if ee_is_flatpak else "native"
        where = "Flatpak" if ee_paths.USE_FLATPAK else "native"
        console.cprint("warn", f"\n⚠  Presets were written to the {where} EasyEffects "
                       f"location, but the {run_where} install was detected — if "
                       "that's the one you use, it won't see them (run --doctor).")
