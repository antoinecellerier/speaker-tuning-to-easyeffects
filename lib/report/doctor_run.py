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
from lib.hardware import sinks
from lib.pipewire import session
from lib.preset import autoload
from lib.report import doctor_layout as layout
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
_EE_READ_REQUESTS = frozenset({ee_socket.PRESET_REQUEST, ee_socket.BYPASS_REQUEST})


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
    if request == ee_socket.PRESET_REQUEST:
        return ee_socket.last_loaded_output_preset()
    return ee_socket.global_bypass()


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
    # What PipeWire's description of `sink` settles about where its audio
    # comes out: "speaker", "other" or "unknown" (lib.hardware.sinks.sink_kind).
    # Three states, not a bool, because two checks act on it in opposite
    # directions — the closing block drops a bullet on "speaker", and the
    # selected-preset check softens only on a confident "other". Folding
    # "unknown" into either would make one of them wrong.
    sink_kind: str = "unknown"
    # A human name for `sink` — the sink's own description, except on
    # Bluetooth, which renders under one fixed label (lib.hardware.sinks).
    # Empty when the probe settled nothing, and the report then shows the
    # node name alone, as it always did.
    sink_label: str = ""
    # Why `sink` is empty, when it is: the graph couldn't be read, or the rc
    # pins an output without naming one. Empty with an empty `sink` means
    # both sources answered and there is genuinely no default.
    sink_reason: str = ""
    # Requests a listening daemon did not answer. Non-empty means EasyEffects'
    # socket protocol changed, not that it is absent — reported rather than
    # absorbed, so this stops reporting stale values as current the moment it
    # happens instead of whenever someone next reads the source.
    unanswered: list[str] = field(default_factory=list)
    # WirePlumber's version as the graph reports it, off the same pw-dump the
    # sink came from; None when it wasn't there to read.
    wireplumber: "session.Version | None" = None

    @property
    def system_output_is_speaker(self) -> bool:
        """Is the *system's* output one of this machine's own speakers?

        Live readings only. A pinned sink answers a different question —
        EasyEffects' own device — and the one caller asks the reader to
        confirm the system output. Someone pinned to the speakers while the
        system default is HDMI needs that prompt most: nothing they are
        listening to goes through the chain. Spending a pinned "speaker" on
        it would drop the line exactly there.
        """
        return self.sink_kind == "speaker" and self.sink_source == "live"


def _resolve_live_state(rc: dict) -> LiveState:
    """Prefer the running daemon and the live graph; fall back to the rc."""
    state = LiveState()

    # An answered request wins even when the name is empty: that is EasyEffects
    # saying nothing is loaded, which its config file cannot distinguish from
    # never-written.
    reply = _ee_query(ee_socket.PRESET_REQUEST)
    if reply.answered:
        state.preset, state.preset_is_live = reply.value, True
    else:
        state.preset = rc.get("last_output_preset", "")
        if reply.reached:
            state.unanswered.append("loaded preset")

    # One pw-dump answers two rows of the report — which sink PipeWire is
    # sending to, and which WirePlumber is running — so it is read even for a
    # pinned output, whose sink comes from the rc.
    d, state.wireplumber = sinks.live_session()
    # useDefaultOutputDevice defaults ON, in which case EE just follows the
    # system default sink and the rc holds a stale copy of it. Pinned is the
    # opposite: only the GUI writes that key, so the rc is then the truth and
    # the live default sink is the wrong answer.
    if rc.get("use_default_output_device", True):
        if d.effective:
            state.sink, state.sink_source = d.effective, "live"
            state.sink_kind, state.sink_label = \
                sinks.sink_kind_and_label(d.effective)
        else:
            state.sink, state.sink_source = rc.get("output_device", ""), "saved"
            if not state.sink:
                # Keep the live probe's why (or its "" = genuinely none):
                # with the rc empty too, this is all the row has to print.
                state.sink_reason = d.reason
    else:
        state.sink, state.sink_source = rc.get("output_device", ""), "pinned"
        # Classified too: the pinned name is the truth (only the GUI writes
        # that key), so it earns an answer the same way the live one does. A
        # sink that has since left the graph isn't found and comes back
        # "unknown", which is the right answer rather than a stale one. The
        # `saved` arm above gets no classification on purpose — that name is
        # EasyEffects' cache of a default it may have followed hours ago.
        if state.sink:
            state.sink_kind, state.sink_label = \
                sinks.sink_kind_and_label(state.sink)

    # The daemon answers exactly 1 (on) or 2 (off). Parsing strictly means a
    # changed reply format degrades to the config copy instead of being read
    # as a confident "off" — and, since we got *an* answer, counts as drift.
    # The rc copy is only ever a display fallback, never a verdict.
    bypass_reply = _ee_query(ee_socket.BYPASS_REQUEST)
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
        installed = shutil.which("easyeffects") or ee_socket.easyeffects_running()
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


def _speaker_autoload_preset(autoload_dir: Path | None) -> str:
    """The preset EasyEffects would autoload on this machine's own speakers.

    Answers the question behind "the bypass preset is selected" once the
    output is something else: not "is a tuning loaded now?" (it correctly
    isn't) but "will the speakers still be right?". Empty when nothing
    settles it — no directory, no entry for a speaker sink, or no speaker
    sink to match against — and the caller then reports a check it couldn't
    make rather than a pass.
    """
    if autoload_dir is None:
        return ""
    entries = autoload.read_autoload_entries(autoload_dir)
    if not entries:
        return ""
    try:
        selected = sinks.select_speaker_sinks()["selected"]
    except (OSError, KeyError, TypeError):
        return ""
    # Matched on node.name *and* the active output route, because that pair is
    # what EasyEffects keys an autoload file on — not the name alone (issue
    # #18, and `write_autoload`'s filename convention). An entry left behind
    # when the route changed still names the right device, and matching on the
    # name would report a mapping EasyEffects will never act on as the preset
    # the speakers autoload: a green line for a machine that has none.
    wanted = {(s.get("name"), s.get("route")) for s in selected
              if s.get("name") and s.get("route")}
    for entry in entries:
        # Past an entry that names no preset, not stopping at it: one speaker
        # sink mapped to nothing must not hide another's real mapping.
        if ((entry.get("device"), entry.get("device-profile")) in wanted
                and entry.get("preset-name")):
            return entry["preset-name"]
    return ""


def _read_presets(output_dir: Path
                  ) -> tuple[list[tuple[Path, dict | None]], int, list[Path]]:
    """The preset files in *output_dir*: ours, how many are someone else's,
    and the ones that couldn't be read.

    Each of ours comes back with its parsed JSON — ``None`` for the bypass
    preset, which is ours by name and carries nothing to check. The folder is
    EasyEffects' own, so the user's other presets sit beside ours; judged by
    this tool's standards every one of them "lacks a speaker-correction
    filter", and the verdict then sent issue #84's reporter to fix two files
    this tool never wrote. They are counted on the folded preset line and
    otherwise left alone. A file that won't parse is neither: nothing says
    whose it is, so it gets its own line rather than a verdict about
    authorship in either direction.
    """
    try:
        paths = sorted(output_dir.glob("*.json"))
    except OSError:
        return [], 0, []
    ours: list[tuple[Path, dict | None]] = []
    foreign = 0
    unreadable: list[Path] = []
    for p in paths:
        if p.stem == autoload.BYPASS_PRESET_NAME:
            ours.append((p, None))
            continue
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            unreadable.append(p)
            continue
        if environment.is_generated_preset(data, p.stem):
            ours.append((p, data))
        else:
            foreign += 1
    return ours, foreign, unreadable


def _no_presets_found(where: str, foreign: int, bypass_present: bool,
                      unreadable: int = 0) -> str:
    """The `Generated presets` WARN for a folder with nothing of ours to check.

    Four folders look empty to the checks and must not read alike: one that
    is empty, one holding only the bypass preset (ours, so "no presets found"
    reads as the doctor missing a file it can see), one holding only the
    user's own presets (where "no presets found" beside two visible files
    reads as the doctor failing to count), and one whose files couldn't be
    read (each of which gets its own line above). "Other" whenever the
    bypass preset is there too, so the count matches the folder's.
    """
    others = foreign + unreadable
    if others:
        one = others == 1
        what = ("wasn't" if one else "weren't") + " written by it" if not unreadable \
            else ("couldn't be read" if not foreign
                  else ("wasn't" if one else "weren't")
                  + " written by it or couldn't be read")
        found = (f"no speaker presets from this tool found in {where} (the "
                 f"{others}{' other' if bypass_present else ''} preset "
                 f"file{'' if one else 's'} there {what})")
    elif bypass_present:
        found = f"no presets other than the bypass preset found in {where}"
    else:
        found = f"no presets found in {where}"
    return f"{found} — run the script on your tuning XML first."


def _gather_doctor_report(output_dir: Path, irs_dir: Path, rc_path: Path,
                          custom_dirs: bool = False,
                          autoload_dir: Path | None = None
                          ) -> environment.DoctorReport:
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

    # 3. Preset + impulse-file integrity — for the presets this tool wrote
    try:
        irs_stems = {p.stem for p in irs_dir.glob("*.irs")}
    except OSError:
        irs_stems = set()
    ours, foreign, unreadable = _read_presets(output_dir)
    generated_names = [p.stem for p, _ in ours]
    dolby_presets = [(p, data) for p, data in ours if data is not None]
    bypass_present = any(data is None for _, data in ours)
    for p in unreadable:
        # UNKNOWN, not FAIL: whose file it is can't be told from a file that
        # won't parse, and the two remedies differ. Not silent either — at
        # feb0739 this was a FAIL, and dropping it would turn a truncated
        # preset of ours into "no problems detected".
        report.checks.append(CheckResult(DOCTOR_UNKNOWN, f"Preset {p.stem}",
            "couldn't be read (not valid JSON), so it wasn't checked — if this "
            "tool wrote it, re-run the script; if it's yours, EasyEffects "
            "can't load it either."))
    if not dolby_presets:
        report.checks.append(CheckResult(DOCTOR_WARN, "Generated presets",
            _no_presets_found(doctor.tilde(output_dir), foreign, bypass_present,
                              len(unreadable))))
    else:
        for p, data in dolby_presets:
            report.checks.append(environment.check_preset_kernel(data, irs_stems, p.stem))
    # 3b. Presets written by an older build — the same artefact class the
    #     PipeWire doctor's conf check covers, in the same sentence
    #     (lib.doctor). A preset without a stamp (an EasyEffects GUI re-save
    #     drops it) reads as unknown, never stale. The bypass preset stays
    #     out by construction (`data is None` above): a re-run keeps an
    #     existing bypass file, so the remedy could never clear it. The
    #     label must not start with "Preset " or the per-preset fold in
    #     `_collapse_preset_checks` would sweep it up.
    stale = doctor.another_version_check(
        "Presets from another version", "preset",
        [autoload.generator_version(data) for _p, data in dolby_presets],
        version.get_version(),
        "re-run dolby_to_easyeffects.py on your tuning XML, the way you "
        "last ran it")
    if stale is not None:
        report.checks.append(stale)

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
        # Resolved only when the check is about to soften: this costs a
        # pw-dump and a directory read, and on the common path (output *is*
        # the speakers) the answer is never looked at.
        speaker_preset = (_speaker_autoload_preset(autoload_dir)
                          if live.sink_kind == "other" else "")
        report.checks.append(environment.loaded_preset_status(
            rc, generated_names,
            live_preset=live.preset if live.preset_is_live else None,
            output_kind=live.sink_kind, speaker_preset=speaker_preset))
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

    # 5b. The PipeWire clock the chain runs at, and whether the graph is
    #     dropping buffers. The *dropout* numbers stay facts, not checks, and
    #     the original reason holds: no quantum is known to be too small
    #     (docs/ee-to-pipewire.md keeps that regime on the unvalidated list),
    #     a client can legitimately pull the session down to min-quantum, and
    #     the xrun counter is cumulative — non-zero on healthy machines — so a
    #     WARN on those would fire with no fault and send the reader to change
    #     a session setting. What issue #84's paste lacked was the numbers, and
    #     a remote reader can weigh them.
    #
    #     The clock *rate* is the one carve-out, and it is a different kind of
    #     claim: not a heuristic about load but a measured, deterministic error
    #     in what we emit — above 48 kHz EasyEffects resamples the convolver
    #     kernel without compensating its gain, so the preset is hot by the
    #     rate ratio (+11.8 dB at 192 kHz, isolated to the convolver;
    #     docs/design-notes.md). It still infers — two of its three rate
    #     sources are settings rather than what ran — but it infers a
    #     configuration, not a fault, and the error it reports is arithmetic
    #     from that rate rather than a judgement about load. What it cannot do
    #     is fire on a graph at the rate we build for, which is the objection
    #     above. Note it reads the driver of whatever sink is current, so a
    #     machine playing to a high-rate external DAC raises it with the
    #     speaker path untouched — correct, since that is the graph the preset
    #     would run in, but it is why the copy never says "your speakers".
    pw_clock = session.read_settings()
    pw_xruns = session.read_xruns(sink=live.sink or "")
    pw_age = session.process_age("pipewire")
    ee_age = session.process_age("easyeffects")
    # Which server those numbers describe — probed once here, beside them.
    pw_version = session.pipewire_version()
    # The running daemon when the graph named it, the installed binary
    # otherwise — same order as the filter-chain doctor, same probe.
    wp_version = live.wireplumber or session.wireplumber_version()
    unread = _pipewire_unread_check(pw_clock, pw_xruns)
    if unread is not None:
        report.checks.append(unread)
    # Prefer the rate the driver actually ran at, falling back to the session
    # default when nothing played during the probe; both can be absent, and
    # the check returns None rather than reading either as zero.
    rate_check = environment.graph_rate_status(pw_xruns.running_rate,
                                               pw_clock.rate,
                                               pw_clock.force_rate)
    if rate_check is not None:
        report.checks.append(rate_check)

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

    # 7b. The neighbouring class: a speaker pin routed through a widget with
    #     no volume amp, observed in the codec dump this report prints below
    route_check = report_speaker.speaker_route_status(report.speaker_info)
    if route_check is not None:
        report.checks.append(route_check)

    # 8. Kernel age — speaker-amp fixes land kernel-side (issue #33)
    report.checks.append(environment.kernel_age_status(report.speaker_info.kernel))

    report.facts = {
        "ee_version": (".".join(map(str, ee_version)) if ee_version
                       else "unknown")
                      + (f" (via {source})" if source else ""),
        "ee_running": _ee_running_fact(live),
        "install": "Flatpak" if ee_paths.USE_FLATPAK else "native",
        "output_dir": doctor.tilde(output_dir),
        "irs_dir": doctor.tilde(irs_dir),
        # Every file in the folder, ours or not: the Environment row counts
        # folder contents, and the folded preset line explains the difference.
        "preset_count": len(generated_names) + foreign + len(unreadable),
        "foreign_preset_count": foreign,
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
        "output_reason": live.sink_reason,
        "output_device_source": live.sink_source,
        "output_is_speaker": live.system_output_is_speaker,
        "output_label": live.sink_label,
        "output_plugins": rc.get("output_plugins", []),
        "bypass": live.bypass,
        "bypass_is_live": live.bypass_is_live,
        "pw_clock": pw_clock,
        "pipewire_version": pw_version,
        "wireplumber_version": wp_version,
        "pw_xruns": pw_xruns,
        "pw_age": pw_age,
        "ee_age": ee_age,
    }
    return report


_NO_SINK_TAIL = ", and EasyEffects has no saved output to fall back on"


def _ee_running_fact(live) -> bool | None:
    """The `running:` value: pgrep's answer, outranked by live proof — a
    socket reply only a running daemon can give must not sit three rows
    below "running: no" or "unknown" (/copy-audit 2026-08-30; pgrep -x can
    also genuinely miss a wrapped binary the socket still answers for)."""
    if live.preset_is_live or live.bypass_is_live:
        return True
    return ee_socket.easyeffects_running()


def _pipewire_unread_check(clock_settings, xruns) -> CheckResult | None:
    """UNKNOWN when nothing PipeWire-side could be read — the filter-chain
    doctor's `PipeWire` check, mirrored, so this report's verdict can't say
    "nothing failed" while the whole `=== PipeWire ===` section above reads
    "not read" (/user-review 2026-08-30). Fires only when *both* probes came
    back empty: one tool missing is a package, not a dead server."""
    if not (clock_settings and clock_settings.reason
            and xruns and xruns.reason):
        return None
    return CheckResult(
        DOCTOR_UNKNOWN, "PipeWire",
        f"nothing PipeWire-side could be read ({clock_settings.reason}; "
        f"{xruns.reason}) — {layout.DAEMON_HINT} If there is no sound at "
        "all, that is the problem to fix before any preset.")


def _pipewire_lines(f: dict) -> list[str]:
    """The `=== PipeWire ===` body: where the sound goes, the clock it runs
    on, and whether the graph drops buffers — the audio server's side, above
    EasyEffects' because a check's detail names the sink and a crackle report
    needs the clock and the dropouts beside it (issue #84)."""
    saved = " (from saved config)"
    lines: list[str] = []
    if f.get("pipewire_version") and f.get("wireplumber_version"):
        lines += layout.version_rows(f["pipewire_version"],
                                     f["wireplumber_version"], layout.GUTTER)
    if f.get("output_device"):
        # Whichever sink this names can be a Bluetooth one — the live default
        # follows the headset on connect exactly as EE's own record did — so
        # this stays the one redacted node name on a path the issue form asks
        # for whole.
        source = {"live": "", "pinned": " (pinned in EasyEffects)"}.get(
            f.get("output_device_source", "saved"), saved)
        lines += layout.output_sink_rows(
            f.get("output_label", ""), doctor.no_bt_address(f["output_device"]),
            source, layout.GUTTER)
    elif f.get("output_device_source") == "pinned":
        # A parsed rc that pins no device is a "none", not a failed probe:
        # nothing here went unread, and "not read" would send a triager
        # hunting a dead probe that never died (/copy-audit 2026-08-30).
        lines += layout.output_sink_rows(
            "", "", "", layout.GUTTER,
            none="EasyEffects is pinned to an output but its config names "
                 "no device")
    else:
        # No name from either source — the row survives and says why, with
        # this path's context: the rc was the fallback and named nothing.
        lines += layout.output_sink_rows("", "", _NO_SINK_TAIL, layout.GUTTER,
                                         reason=f.get("output_reason", ""))
    # Both rows come straight from PipeWire's own tools (`pw-metadata -n
    # settings`, `pw-top -b -n 7`), rendered by the frame both doctors share.
    if f.get("pw_clock") is not None:
        lines += layout.clock_rows(f["pw_clock"], f.get("pw_xruns"), layout.GUTTER)
    if f.get("pw_xruns") is not None:
        lines += layout.dropouts_rows(f["pw_xruns"], f.get("pw_age"),
                                      f.get("ee_age"), layout.GUTTER)
    return lines


def _setup_lines(f: dict) -> list[str]:
    """The `=== EasyEffects setup ===` body: the install this tool wrote
    into, then — after a group break — what EasyEffects is doing with it now.
    Labels pad to `doctor_layout.GUTTER` so the values line up. No `Tool:` row: the
    report's first line already carries the version."""
    # Rows below come from whichever source is authoritative for that value,
    # so a row EasyEffects could have answered live but didn't says where it
    # came from — including when EE isn't running at all. Stating it once up
    # top instead was worse: the rows then read identically whether or not
    # the value was confirmed, and the closing block's bypass reminder (which
    # is dropped only on a live reading) looked arbitrary next to a bypass
    # line that looked equally sure of itself either way.
    saved = " (from saved config)"
    running = f.get("ee_running")
    # Three states, not two: pgrep can be missing, denied or hung, and "no"
    # there would be wrong in the direction that reassures.
    running_txt = ("unknown (pgrep couldn't be run)" if running is None
                   else "yes" if running else "no")
    lines = layout.wrapped_row(
        "EasyEffects",
        f"{f.get('ee_version', '?')}; running: {running_txt}; "
        f"service mode {'on' if f.get('service_mode') else 'off'}, "
        f"autostart {'on' if f.get('autostart_on_login') else 'off'}", layout.GUTTER)
    # Both counts are what the folders hold — the bypass preset, presets the
    # user put there and stray .irs files included — so neither is derived
    # from the other. "Presets sharing impulse files" explained the gap with
    # a relationship these counts don't establish.
    lines += layout.wrapped_row(
        "Install",
        f"{f.get('install')} — writes to {f.get('output_dir')}; "
        f"{f.get('preset_count', 0)} preset files and "
        f"{f.get('irs_count', 0)} impulse files in the folders", layout.GUTTER)
    lines.append(layout.row(
        "Config",
        f"{f.get('rc_path')} ({'present' if f.get('rc_present') else 'absent'})",
        layout.GUTTER))
    live: list[str] = []
    if f.get("selected_preset"):
        # `Selected preset:`, not `Selected:`, and the name quoted as every
        # other mention of one in this report is. Bare, the bypass preset
        # rendered as "Selected: Nothing", which reads as "nothing is
        # selected" and gave no hint the row was about an EasyEffects preset
        # at all. The label is what carries that — it is the widest in the
        # block, and `doctor_layout.GUTTER` is sized for it.
        #
        # The row says what the thing *is*; whether it should be loaded is
        # the check's to say. In particular not "the bypass preset" here:
        # that word belongs to `Global bypass:` below, EasyEffects' own
        # toggle, and spending it on a preset name is what made the two rows
        # read as if they contradicted each other.
        live.append(layout.row("Selected preset", f"'{f['selected_preset']}'",
                               layout.GUTTER)
                    + ("" if f.get("selected_is_live") else saved))
    # No live source exists for the chain, so it is always the saved copy —
    # worth marking next to rows that aren't. Wrapped because a full chain is
    # seven plugin names and ran to ~145 columns on one line; continuations
    # land on the same gutter as the values above. break_on_hyphens
    # is off for the same reason `_cprint_wrapped` turns it off — a plugin name
    # split across lines stops being greppable.
    if f.get("output_plugins"):
        width = console._wrap_width()
        chain = textwrap.wrap(
            ", ".join(f["output_plugins"]),
            width=width, break_on_hyphens=False,
            initial_indent=layout.row("Active chain", "", layout.GUTTER),
            subsequent_indent=" " * layout.GUTTER)
        # The marker is appended after wrapping, not wrapped with the list:
        # split across lines it reads as part of the last plugin name
        # ("limiter#0 (from" / "saved config)").
        if len(chain[-1]) + len(saved) <= width:
            chain[-1] += saved
        else:
            chain.append(" " * layout.GUTTER + saved.strip())
        live += chain
    # Prints even when off: "is it bypassed?" is the first question behind
    # "I hear no difference", and a positive "off" answers it. `Global
    # bypass:`, not `Bypass:` — this is EasyEffects' one power-button toggle,
    # and the short label collided with the bypass *preset* two rows up, so a
    # reader met the same word for two things and had to work out that
    # "'Nothing' is the expected bypass" and "Bypass: off" were not
    # contradicting. It is also what the closing block already calls it.
    if f.get("bypass_is_live") or f.get("rc_present"):
        live.append(layout.row("Global bypass", "on" if f.get("bypass") else "off",
                               layout.GUTTER)
                    + ("" if f.get("bypass_is_live") else saved))
    if live:
        lines += [""] + live   # the group break: install above, live state below
    return lines


def _environment_lines(f: dict) -> list[str]:
    """Both inventory blocks in print order — the PipeWire rows, then the
    EasyEffects setup — as one list, for tests and for anyone who wants the
    whole inventory at once. The report prints them as two sections."""
    return _pipewire_lines(f) + _setup_lines(f)


def _collapse_preset_checks(checks: list[CheckResult], *,
                           bypass_present: bool = False,
                           foreign: int = 0) -> list[CheckResult]:
    """Fold a run of passing per-preset checks into one line.

    A machine can have dozens of profiles, and a screenful of identical PASS
    lines buries everything else; any preset with a problem is still listed
    individually, in place of the run. The collapsed line is a `CheckResult`
    carrying no detail, so it renders through the same printer as every other
    check rather than a hand-built copy of its format.

    **Only passes are ever folded** — `problems` is appended whole — which is
    what lets the summary count the returned list rather than the original:
    FAIL, WARN and UNKNOWN totals are identical either way, and the verdict
    reads the same statuses it always did. The count of presets behind the
    folded line lives in that line's own label, beside the thing it counts.
    The summary used to carry it instead (`print_check_block`'s old
    ``counted``) and readers could not reconcile it with the lines on screen.
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
                # The detail reconciles the one number this line cannot
                # explain by itself: the preset count in the Environment
                # block above includes the bypass preset (no filters, nothing
                # to check) and any presets the user put there themselves,
                # which this tool doesn't judge. It no longer explains the
                # summary — that now counts the lines it printed.
                #
                # The label says "passed", not "load their impulse file": a
                # preset can fail this check for reasons that have nothing to
                # do with an impulse file, including having no convolver at
                # all. The detail may say it, because it speaks for the
                # passing ones only, and PASS is exactly that
                # (`check_preset_kernel`). Past tense so it still reads at
                # one preset, which "1 of 1 pass" does not.
                detail = ("Every one of them loads its speaker-correction "
                          "impulse file.")
                if bypass_present:
                    detail += (f" The '{autoload.BYPASS_PRESET_NAME}' bypass "
                               "preset isn't among them — it has no filters "
                               "by design.")
                if foreign:
                    one = foreign == 1
                    detail += (f" {foreign} other preset file{'' if one else 's'}"
                               f" in the folder {'is' if one else 'are'}n't this"
                               f" tool's, so nothing here checked "
                               f"{'it' if one else 'them'}.")
                shown.append(CheckResult(
                    DOCTOR_PASS,
                    f"Presets ({passing} of {len(presets)} passed)",
                    detail))
            shown += problems
    return shown


def _print_doctor_report(report: environment.DoctorReport) -> None:
    """Print a compact, paste-safe diagnostic report."""
    layout.print_report_header(version.get_version())
    if report.speaker_info is not None:
        report_speaker._print_speaker_info(report.speaker_info)
    pipewire = _pipewire_lines(report.facts)
    if pipewire:
        layout.print_environment(pipewire, "=== PipeWire ===")
    layout.print_environment(_setup_lines(report.facts), "=== EasyEffects setup ===")
    layout.print_check_block("=== EasyEffects doctor ===",
                             _collapse_preset_checks(
                                 report.checks,
                                 bypass_present=report.facts.get(
                                     "bypass_preset_present", False),
                                 foreign=report.facts.get(
                                     "foreign_preset_count", 0)))
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


def report_doctor(args) -> None:
    """--doctor entry point: run environment self-diagnostics and print them."""
    report = _gather_doctor_report(args.output_dir, args.irs_dir,
                                   ee_paths.DEFAULT_EASYEFFECTS_RC,
                                   custom_dirs=ee_paths.uses_custom_dirs(
                                       args.output_dir, args.irs_dir),
                                   autoload_dir=args.autoload_dir)
    _print_doctor_report(report)


def _graph_rate_headline(dry_run: bool) -> str:
    """The end-of-run headline. The explanation itself comes from
    `environment.graph_rate_message`, so this path and --doctor cannot drift —
    the arrangement `kernel_old_message` uses.

    "set to", never "running at": this arm reads `pw-metadata` only, because
    the rate the driver actually ran at costs a five-second `pw-top` window a
    generation run should not pay. The detail below says "is set to" / "is
    pinned to" for the same reason, and a headline claiming to know what ran
    would contradict it one line later.
    """
    presets = ("The presets this run would write are" if dry_run
               else "The presets above are")
    return (f"{presets} built for a {environment.SAMPLE_RATE} Hz graph, "
            "and yours isn't set to one.")


def warn_ee_environment(args) -> "report_findings.Finding | None":
    """End-of-run check for a normal generation run: loudly warn if the
    installed EasyEffects can't use the presets we just wrote. Silent on the
    happy path. Reuses --doctor's probes; mirrors warn_speaker_firmware_gate.

    Returns the graph-rate finding when one is raised, so its ask reaches the
    closing block — everything else here is a print, because everything else
    here is about the EasyEffects *install*, which the reader either has to
    fix before anything works or does not have to fix at all.
    """
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
        return None

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
    if (not ee_paths.uses_custom_dirs(args.output_dir, args.irs_dir)
            and ee_is_flatpak is not None and ee_is_flatpak != ee_paths.USE_FLATPAK):
        run_where = "Flatpak" if ee_is_flatpak else "native"
        where = "Flatpak" if ee_paths.USE_FLATPAK else "native"
        console.cprint("warn", f"\n⚠  Presets were written to the {where} EasyEffects "
                       f"location, but the {run_where} install was detected — if "
                       "that's the one you use, it won't see them (run --doctor).")

    # A graph above the rate we build at makes the preset we just wrote wrong,
    # not merely expensive (issue #84) — so it belongs on the run that wrote
    # it, not only in a --doctor most users never type. Settings only: the
    # rate the driver actually ran at costs a five-second pw-top window, which
    # is the diagnostic's to pay and not a generation run's, so this arm reads
    # what the session is configured or pinned to and words itself that way.
    # Only meaningful when the EasyEffects that will play these presets is
    # THIS machine's. With none found, the run is generating for elsewhere
    # (the branch above says so), and this machine's clock describes a graph
    # the presets will never run on.
    if not found:
        return None
    clock = session.read_settings()
    if not clock.ok:
        return None
    finding = environment.graph_rate_finding(0, clock.rate, clock.force_rate)
    if finding is None:
        return None
    console.cprint("warn", "\n⚠  " + _graph_rate_headline(args.dry_run))
    print()
    # Not "dim". Every other wrapped body in this function explains an install
    # the reader still has to go and fix; this one explains a fault that is
    # already audible, and /user-review round 12 read the dimming as "footnote,
    # skip me" on exactly that basis.
    console._cprint_wrapped("warn", finding.detail)
    # The command rides its own unwrapped lines, as it does in the --doctor
    # check's `steps`: folded across two lines it stops being runnable.
    for style, text in environment.graph_rate_steps(clock.force_rate):
        console.cprint(style, text)
    return finding
