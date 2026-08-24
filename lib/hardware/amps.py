"""Evidence about the smart amplifiers, keyed by driver rather than by bus.

HDA-attached Cirrus and TI parts load DSP firmware exactly as SoundWire ones
do, so "did the amplifier come up" is a smart-amp question and not a SoundWire
question. ``_AMP_FAMILIES`` is the single registry that answers it — one row
per part family, carrying the driver-name tokens, the firmware globs and the
kernel-log strings those drivers really print — and everything around it is a
generic engine over that table, so adding a device is one row rather than a
new code path.

Nothing here renders a verdict, deliberately: no sysfs or debugfs attribute
exposes amp audio-state, so this collects *evidence* (which blobs are present,
which log lines matched, which of those are unambiguous failures) and leaves
the reading of it to the report.

Two boundary notes:

* ``_gather_amp_evidence`` annotates ``SpeakerInfo``, which lives next door in
  ``lib/hardware/speakers.py``. It is a forward reference and stays
  unresolved: ``from __future__ import annotations`` keeps annotations as
  strings, so the record type can own the amp fields this fills without this
  module importing — and cycling with — the one that defines it. The body
  touches attributes only.
* Standard library only, so ``tests/test_layout.py``'s ``STDLIB_ONLY`` covers
  it. ``lib/hardware/speakers.py`` imports this module and not the other way
  round; keep that direction.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


# --- Smart-amp status: bus-agnostic evidence (issue #27) --------------------
#
# Whether the speaker amps are actually live is a smart-amp question, not a
# SoundWire one: HDA-attached Cirrus (cs35l41) and TI (TAS2781) amps load DSP
# firmware too, and a cs35l56 with no firmware still plays — but as a quiet
# "mono mix" with no voicing/protection (cs35l56 kernel doc). No sysfs/debugfs
# exposes amp audio-state, so the authoritative signal is the kernel log. We
# gather *evidence* (an enumerated-but-unbound amp is the one hard verdict;
# firmware files + log markers are shown for a human) keyed by driver, so the
# engine is generic and adding a device is one registry row.

# One row per smart-amp family: (driver/module name tokens, firmware globs,
# kernel-log keywords, kernel-log failure markers). Empty globs = the family
# ships no DSP blob. Tokens are matched as substrings of a driver/module name
# and double as the SoundWire amp-detection patterns, so adding a device is
# genuinely one row. The max98 tokens are the specific *smart*-amp parts, not a
# bare "max98" — that would also catch the max98090 jack codec and the dumb
# max98357/360 I2S amps (same reason "rt13" is narrow enough to skip rt711).
#
# The failure markers are firmware/tuning/DSP-bring-up error strings verified
# verbatim against the mainline driver source (file:line cited per family) — NOT
# the kernel doc, whose ".bin file required but not found" is prose the driver
# never prints. They classify which collected log lines are failures; "" = no
# honest tell. Absence of a marker is never a pass: a real cs35l56 report (#27)
# printed FIRMWARE_MISSING / "Calibration disabled…" / "Can't read tuning IDs"
# while our first marker set (boot/init timeouts only) reported "no errors",
# which is exactly why the no-error line tells the reader to eyeball the log.
_AMP_FAMILIES = (
    # Cirrus cs35l41 (HDA) / cs35l56 / cs35l57 (SoundWire). Markers:
    # cs35l56-shared.c "FIRMWARE_MISSING" (l.1388), "Can't read tuning IDs"
    # (l.1424), "Firmware boot timed out" (l.455); cs35l56.c "init_completion
    # timed out" (l.866/1373); cs-amp-lib.c "Calibration disabled due to missing
    # firmware controls" (l.140/172, shared lib — also fires for cs35l41).
    (("cs35l",), ("cirrus/cs35l*",), ("cs35l", "cirrus"),
     r"firmware_missing|can't read tuning ids"
     r"|calibration disabled due to missing firmware controls"
     r"|firmware boot timed out|init_completion timed out"),
    # TI smart amps (issue #17 family). Markers from tas2781-fmwlib.c /
    # tas2781-i2c.c: "FW download failed", "Failed to read firmware",
    # "Request firmware … failed", "Firmware is NULL", "Bin file error".
    (("tas2",), ("TAS2*", "ti/tas2*", "tas2*"), ("tas2",),
     r"fw download failed|failed to read firmware|request firmware .* failed"
     r"|firmware is null|bin file error"),
    # Realtek SoundWire amps — only rt1320 loads a firmware patch (rt1316/rt1318
    # are register-only). Markers from rt1320-sdw.c: "Failed to load … firmware",
    # "FW file doesn't match to device", "Can't find proper FW file name".
    (("rt13", "rt_amp"), (), ("rt13",),
     r"failed to load .* firmware|fw file doesn't match to device"
     r"|can't find proper fw file name"),
    # Awinic AW88399, the woofer amp on 2025 Lenovo Legion laptops (upstream
    # 7.3, ALC287 + AWDZ8399 over I2C). Its HDA side codec arrived with the
    # symptom this project already knows from issue #53 — "only the tweeters
    # produce sound", in the driver's own words — but the cause is the missing
    # driver, not a hidden pin, so nothing in the speaker-pin table catches it.
    # The whole family loads one aw88NNN_acf.bin; markers from aw88399-lib.c
    # "request [%s] failed!" (l.1292, no file) and "load [%s] failed!" (l.1309,
    # bad ACF), the same pair the ASoC siblings print. Not "dev init failed"
    # (l.1317): too generic to attribute. Derived from upstream source — no
    # device has been reported on yet.
    (("aw88",), ("aw88*_acf.bin",), ("aw88",),
     r"request \[aw88[0-9]*_acf\.bin\] failed"
     r"|load \[aw88[0-9]*_acf\.bin\] failed"),
    # Maxim DSM smart amps — no honest firmware-missing tell: only max98390 loads
    # a DSM calibration param, and a missing file falls through silently
    # (max98390.c err path), so we collect its log lines but flag nothing.
    (("max98373", "max98390", "max98363", "max98396", "max98512"),
     (), ("max98",), ""),
)

_AMP_DRIVER_TOKENS = tuple(tok for fam in _AMP_FAMILIES for tok in fam[0])


def _amp_firmware_profile(driver: str) -> tuple[list[str], list[str]] | None:
    """(firmware globs under /lib/firmware, kernel-log keywords) for a driver.

    Looks the driver up in ``_AMP_FAMILIES`` — the single source of amp-family
    identity. None ⇒ not a recognised smart amp.
    """
    d = driver.lower()
    for tokens, globs, keywords, _markers in _AMP_FAMILIES:
        if any(t in d for t in tokens):
            return (list(globs), list(keywords))
    return None


def _loaded_amp_drivers() -> list[str]:
    """Loaded kernel modules that look like smart-amp drivers (any bus)."""
    moddir = Path("/sys/module")
    if not moddir.is_dir():
        return []
    return sorted(m.name for m in moddir.iterdir()
                  if any(t in m.name.lower() for t in _AMP_DRIVER_TOKENS))


def _list_firmware_files(globs: list[str], roots=None) -> list[str]:
    """Existing firmware files matching globs under /lib/firmware (+ updates/)."""
    if roots is None:
        roots = (Path("/lib/firmware"), Path("/lib/firmware/updates"))
    found = set()
    for root in roots:
        for g in globs:
            for p in root.glob(g):
                if p.is_file():
                    found.add(str(p.relative_to(root)))
    return sorted(found)


def _read_kernel_log() -> str | None:
    """Current-boot kernel log via journalctl then dmesg; None if none readable.

    ``journalctl -o cat`` emits the message text only — no hostname or wall-clock
    timestamp — so the lines stay safe to paste into a device-report issue (same
    privacy posture as get_distro_pretty_name; dmesg carries no hostname either).
    ``errors="replace"`` keeps a stray non-UTF-8 byte from aborting the report,
    and the timeout is kept short since this runs on the default --doctor path.
    """
    for cmd in (["journalctl", "-k", "-b", "-o", "cat", "--no-pager"], ["dmesg"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               errors="replace", timeout=4)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
    return None


# Union of every family's source-verified failure markers (co-located in
# _AMP_FAMILIES above). We do NOT try to classify "healthy": no log line
# reliably proves firmware loaded (`patched=N` is nuanced; success strings are
# vendor-specific), so a green verdict would mislead — and the marker list is
# deliberately not exhaustive, so the no-error path tells the reader to read the
# lines themselves. Every matched line is shown verbatim as evidence.
_AMP_LOG_ERROR_RE = re.compile(
    "|".join(markers for *_, markers in _AMP_FAMILIES if markers), re.I,
)


def _amp_log_is_error(line: str) -> bool:
    """True when a kernel-log line is an unambiguous amp firmware/init error."""
    return bool(_AMP_LOG_ERROR_RE.search(line))


def scan_amp_log(log_text: str, keywords: list[str]) -> list[tuple[bool, str]]:
    """(is_error, line) for kernel-log lines mentioning any amp keyword."""
    if not keywords:
        return []
    kw = re.compile("|".join(re.escape(k) for k in keywords), re.I)
    return [(_amp_log_is_error(line), line.strip())
            for line in log_text.splitlines() if kw.search(line)]


def _gather_amp_evidence(info: SpeakerInfo) -> None:
    """Populate driver-keyed firmware-presence and kernel-log evidence."""
    # Driver tokens from both loaded modules and bound SoundWire amps.
    drivers = _loaded_amp_drivers() + [a.driver for a in info.amp_status if a.driver]
    profiles = [p for p in (_amp_firmware_profile(d) for d in drivers) if p]
    if not profiles:
        return
    globs = sorted({g for pr in profiles for g in pr[0]})
    keywords = sorted({k for pr in profiles for k in pr[1]})
    # Self-check hint derived from the keywords we actually scan, so the printed
    # `grep` command can't contradict what the report found.
    info.amp_log_grep = "|".join(keywords)
    if globs:
        info.amp_firmware = _list_firmware_files(globs)
        # A blob-needing driver is loaded but none was found. Decided once here
        # (not at render) so the flag can't diverge from what we searched for.
        info.amp_firmware_missing = not info.amp_firmware
    log = _read_kernel_log()
    if log is None:
        info.amp_log_available = False
    else:
        info.amp_log = scan_amp_log(log, keywords)
