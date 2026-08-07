"""What a run tells the user about this machine's speakers.

The reporting half of the speaker code; `lib/hardware/speakers.py` is the
probing half that answers *what is there*, and everything here decides what to
say about the answer. Three surfaces read from it and none of them may drift
apart: `--speaker-info` prints the full hardware dump, `--doctor` folds that
same dump in under its verdicts, and a normal conversion run raises the
findings whose asks land in the closing block.

The gathering functions come in two sizes on purpose. `_gather_speaker_pins`
is a few /proc reads and one sysfs walk, which is all a normal run needs to
answer "is a speaker pin missing"; `_gather_speaker_info` also shells out per
card and globs /lib/firmware for the amp-status section, which only
`--speaker-info` and `--doctor` ever print.

`upgrade_prospect`, `speaker_pin_fix_steps` and `speaker_pin_status` are the
shared-copy rule in miniature: the end-of-run warning and `--doctor` render
the same procedure from the same builder, so the two surfaces cannot tell a
user different things about the same fixup.

`report_findings` keeps the generator's alias for `lib/report/findings.py`
because these bodies read through it verbatim, and `Finding` arrives as a bare
name for the same reason — it is a frozen record with nothing to patch, the way
`lib/report/messages.py` already takes it. `CheckResult` and `DOCTOR_WARN`
come from `lib/doctor.py` on the same terms.

Nothing here may import `lib/report/environment.py`'s importers: this module
reads that one (`parse_kernel_series`, `_kernel_series_age`) and the `--doctor`
I/O that assembles both sits above the two.
"""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

from lib import console, version
from lib.data import speaker_pin_quirks
from lib.doctor import DOCTOR_WARN, CheckResult
from lib.hardware import amps, codecs, speakers
from lib.report import environment
from lib.report import findings as report_findings
from lib.report.findings import Finding


def warn_speaker_firmware_gate(gates: list[speakers.FirmwareGate]) -> Finding | None:
    """Warn — with copy-paste fixes — about any firmware-load gate that's off,
    and return the ask for whether toggling it worked.

    Silent when no gate is off (the gate is either absent or already enabled,
    so the speakers aren't muted on its account).

    The procedure below *is* this finding's detail, so the caller doesn't
    reprint it — only the returned one-line ask travels to the closing block.
    """
    off = [g for g in gates if not g.on]
    if not off:
        return None
    g0 = off[0]  # representative gate for the verify examples

    console.cprint("warn", f"\n{'=' * 60}")
    console.cprint("warn", "⚠  [firmware-gate] Smart-amp firmware gate is OFF — your speakers")
    console.cprint("warn", "   may be silent, thin or crackly even though the preset is correct.")
    console.cprint("dim", "Many devices drive their speakers through a TI TAS2563/2781 smart")
    console.cprint("dim", "amplifier whose firmware does not auto-load; until this ALSA control")
    console.cprint("dim", "is switched on the amp runs untuned upstream of the preset. On most")
    console.cprint("dim", "devices that mutes the woofers; where the amp drives every speaker,")
    console.cprint("dim", "it can instead make everything thin, quiet or prone to dropouts.")
    print()
    # Enable now: no root needed — the active logind session already holds an
    # ACL on /dev/snd/control*. Persist with `alsactl store`, which saves the
    # state that alsa-restore.service replays at boot (the standard ALSA path).
    console.cprint("dim", "1. Enable it now (no root needed), then listen for a change:")
    for g in off:
        console.cprint("cta", f"     {speakers.amixer_enable_cmd(g)}")
    print()
    # The card name is this machine's own (read from /proc/asound/cardN/id),
    # so the command is right as printed — but a card that renumbers between
    # boots, or a copy-paste into a later session, lands on "cannot find
    # card", and until now the only recovery text covered the command working
    # and changing nothing.
    console.cprint("dim", "   Errors with \"cannot find card\"? The card was renamed or")
    console.cprint("dim", "   renumbered since this run — list them with:  aplay -l")
    console.cprint("dim", f"   and use the name it shows in place of {g0.card_id}.")
    print()
    console.cprint("dim", "   No change at all, and nothing sounded wrong to begin with? Then")
    console.cprint("dim", "   this gate wasn't your problem — skip the rest of this section.")
    print()
    console.cprint("dim", "2. If that worked, persist it across reboots — saves the ALSA state")
    console.cprint("dim", "   that alsa-restore replays at boot:")
    console.cprint("cta", "     sudo alsactl store")
    print()
    # No systemd-unit fallback here any more. It named one ("fall back to a
    # systemd --user oneshot that runs the amixer command above at login")
    # without the unit, the path or the enable command — a reviewer rated it
    # unusable for the same reason as the old firmware-extraction line: a fix
    # you can name but not run. Writing the unit out is four more lines for a
    # case we have never seen reported, so the ask goes back to us instead.
    console.cprint("dim", "   (Doesn't survive a reboot? alsa-restore can race the driver on")
    console.cprint("dim", "   some setups — tell us and we'll work out the fix with you.)")
    print()
    console.cprint("dim", "3. Self-check — confirm the control stuck and the firmware loaded:")
    console.cprint("cta", f"     amixer -c {g0.card_id} cget "
                  f"\"iface={g0.iface},name='{g0.name}'\"")
    # No ".bin" suffix in the glob: distros may ship the blobs compressed
    # (TAS2XXX….bin.zst on SteamOS — the kernel decompresses transparently)
    # and the narrower pattern would report them "missing" (#39).
    console.cprint("cta", "     journalctl -k -b | grep -iE 'tas2|firmware'")
    console.cprint("cta", "     ls -l /lib/firmware/TAS2*")
    console.cprint("dim", "   (no journal access? try:  sudo dmesg | grep -i tas2)")
    print()
    console.cprint("dim", "   Still wrong, and the log shows 'Direct firmware load for")
    console.cprint("dim", "   TAS2XXX….bin failed' or no such file exists? Your distro's")
    console.cprint("dim", "   linux-firmware is missing this machine's blob, and no preset")
    console.cprint("dim", "   makes up for it. Update linux-firmware; if the file still")
    console.cprint("dim", "   doesn't turn up, report it with that log line — it names the")
    console.cprint("dim", "   exact file the kernel wants.")
    # No extraction pointer here, deliberately. The old wording ("extract it
    # from your Windows driver or TI's TAS2781-LINUX package and drop it into
    # /lib/firmware") read as a step and stopped a reviewer dead: no tool, no
    # method, and nothing marking it as specialist work. It meant to record
    # that a reporter had managed it — but that case is Cirrus (#27), whose
    # file layout and naming don't transfer to TI, and TI's own package is
    # driver source and a calibration tool, not a source of per-machine
    # blobs. A hint that fits neither the reader's amp nor their skill level
    # costs more attention than it returns.
    # The feedback ask (it gates whether we automate this) used to be two dim
    # lines here, deliberately whispered so it wouldn't rival the closing call
    # to action. It travels to that block instead now, where it can be a
    # normal ask without competing with anything.
    return report_findings._firmware_gate_finding()


_MODPROBE_CONF = "/etc/modprobe.d/speaker-pin-fix.conf"


def upgrade_prospect(quirk: speaker_pin_quirks.PinQuirk,
                     release: str | None = None) -> str:
    """Whether upgrading the kernel would fix this, in the user's terms.

    Three genuinely different situations, and telling the wrong one wastes a
    reader's evening: no release carries the fix yet; a release does and they
    are behind it; or they are already past it, in which case the fix is
    reaching them and something else on the machine is stopping it — so
    "upgrade" would be advice to go and get what they already have.

    Shared by the end-of-run block and --doctor so the two can't drift.
    """
    import platform

    # Where the reader is left depends on whether a hand-forcible name exists:
    # with one, each branch hands off to the procedure that follows; without,
    # the branch has to be a complete answer on its own.
    tail = (" To apply it on the kernel you have now:" if quirk.model else
            " This fixup has no name the driver accepts, so it can't be forced "
            "by hand — a kernel that carries it is the only route.")
    if not quirk.since:
        return ("The fix is merged upstream but is not in any released kernel "
                "yet, so upgrading won't help today." + tail)
    running = environment.parse_kernel_series(release or platform.release())
    fixed_in = environment.parse_kernel_series(quirk.since)
    if running and fixed_in and running >= fixed_in:
        return (f"Linux {quirk.since} carries this fix and you are on "
                f"{running[0]}.{running[1]}, so it should already be applying "
                "— something on this machine is stopping it (a vendor kernel "
                "that dropped the fix, or a different model id than upstream "
                "expects)." + tail)
    return (f"Linux {quirk.since} and newer apply this automatically, so a "
            "kernel upgrade is the durable fix." + tail)


def _pin_phrase(missing: list[str]) -> str:
    """"pin 0x17" / "pins 0x14 and 0x17" — the copy has to work for both.

    A fixup declares one pin or two, and on a machine with none configured
    both of them are missing, so no message here may assume a count.
    """
    if len(missing) == 1:
        return f"pin {missing[0]}"
    return "pins " + " and ".join(missing)


def speaker_pin_fix_steps(quirk: speaker_pin_quirks.PinQuirk,
                          missing: list[str], uses_sof: bool,
                          width: int,
                          speaker_info_below: bool = False,
                          ) -> tuple[tuple[str, str], ...]:
    """Apply → confirm → undo, as ``(style, text)`` lines.

    Shared by the end-of-run warning and ``--doctor``'s check the way
    ``amixer_enable_cmd`` is shared, so the procedure can't drift between the
    two surfaces — and empty where the fixup has no forcible name, since then
    there is no procedure, only the upgrade route ``upgrade_prospect`` states.

    Prose wraps to *width*; commands never do. A command wider than the
    terminal is soft-wrapped by the terminal and still pastes as one line,
    where a folded one would not run at all — so the caller passes the width
    its own surface uses and no line here is broken by hand.

    ``speaker_info_below`` is the one thing that differs between the two
    callers: a --doctor run prints the hardware section itself, so sending
    that reader off to --speaker-info for a section already on their screen
    reads as a third command to type. The commands stay identical either way.
    """
    if not quirk.model:
        return ()
    module, param = speakers.hda_model_module(uses_sof)

    def prose(text: str, indent: str = "", hang: str = "") -> list[tuple[str, str]]:
        return [("dim", line) for line in textwrap.wrap(
            text, width, initial_indent=indent,
            subsequent_indent=hang or indent, break_on_hyphens=False)]

    # Where to look afterwards — the one sentence that differs by surface.
    verify = (f'look at the "HDA internal speakers" section below: '
              f"{_pin_phrase(missing)} should be listed there, tagged "
              "[kernel fixup]."
              if speaker_info_below else
              f"re-run with --speaker-info: {_pin_phrase(missing)} should be "
              'listed under "HDA internal speakers", tagged [kernel fixup].')
    return tuple([
        *prose("1. Write the option, then reboot:"),
        ("cta", f"     echo 'options {module} {param}={quirk.model}' \\"),
        ("cta", f"       | sudo tee {_MODPROBE_CONF}"),
        # The fixup's name is the kernel's, and several of them carry a model
        # that isn't the one running: this row is keyed to a codec id, and its
        # upstream entry reads "Yoga 7 16IAP7" while the name says yoga9. A
        # reader who spots that in a line they are about to sudo stops there.
        *prose("   That name is the kernel's label for the fix, not your "
               "model — it is matched to your machine by hardware id.",
               hang="   "),
        ("", ""),
        # Two independent confirmations, and the audible one leads because it
        # is the one the user cares about. Hedged the way the warning above is:
        # the pin usually drives woofers, but several fixups in the table
        # declare a machine's only speaker pin, where nothing was playing.
        *prose("2. After rebooting you should hear it — usually the bass "
               "coming back, or sound from speakers that were silent. To "
               f"check the kernel side, {verify}", hang="   "),
        ("", ""),
        *prose("Still missing, or the speakers went quiet? Undo it:",
               indent="   "),
        ("cta", f"     sudo rm {_MODPROBE_CONF}"),
        *prose("and reboot again. Nothing else on the system is touched.",
               indent="   "),
    ])


def warn_hidden_speaker_pin(
        found: tuple[speaker_pin_quirks.PinQuirk, str, list[str]] | None,
        info: speakers.SpeakerInfo) -> Finding | None:
    """Warn — with a copy-paste fix and its undo — that the kernel is leaving
    one of this machine's speakers unconfigured.

    Silent when nothing matched, which is the overwhelming majority of
    machines: the table lists the models upstream has had to fix, and a
    2-driver device showing one pin is simply correct.

    The procedure below *is* this finding's detail, so the caller doesn't
    reprint it — only the returned one-line ask travels to the closing block.
    """
    if not found:
        return None
    quirk, codec_ssid, missing = found
    phrase = _pin_phrase(missing)

    # "the preset shapes the rest alone" only holds if a speaker pin survived:
    # where the fixup declares the machine's only one, there is no rest.
    others = any(p.codec == codec_ssid for p in info.speakers)
    console.cprint("warn", f"\n{'=' * 60}")
    console.cprint("warn", "⚠  [speaker-pin] Linux isn't driving all of your speakers.")
    console._cprint_wrapped("dim",
        f"Upstream Linux carries a fix for this exact model that declares "
        f"{phrase} on codec {codec_ssid} an internal speaker, and your kernel "
        "isn't applying it. Your machine's firmware describes it as "
        "unconnected and the kernel takes it at its word, so whatever it "
        "drives — often the woofers — gets no signal"
        + (", and the preset shapes the rest alone." if others else "."))
    print()
    console._cprint_wrapped("dim", upgrade_prospect(quirk))
    steps = speaker_pin_fix_steps(quirk, missing,
                                  speakers._card_uses_sof(info.sound_cards),
                                  console._wrap_width())
    if steps:
        print()
    for style, text in steps:
        if text:
            console.cprint(style, text)
        else:
            print()
    print()
    return _hidden_pin_finding(quirk, missing)


def _hidden_pin_finding(quirk: speaker_pin_quirks.PinQuirk,
                        missing: list[str]) -> Finding:
    """Whether forcing the missing pin actually restored the bass.

    Carries an ask only when the run printed a procedure to ask about. Where
    the fixup has no forcible name there is nothing the reader can do on this
    run, and `.claude/rules/user-messages.md` is explicit that such a finding
    takes no ask — its detail still travels, so a pasted report still shows it.
    """
    phrase = _pin_phrase(missing)
    if not quirk.model:
        return Finding(
            slug="speaker-pin", kind="hint",
            detail=f"{phrase[0].upper() + phrase[1:]} is declared an internal "
                   "speaker by a kernel fix this machine isn't getting, and "
                   "the driver accepts no name that would force it — see above.")
    return Finding(
        slug="speaker-pin", kind="hint",
        detail=f"{phrase[0].upper() + phrase[1:]} is left unconfigured, so "
               "those speakers get no signal — see the procedure above.",
        ask="Did forcing the missing speaker pin bring your bass back? "
            "(issue #53)")


def unlisted_speaker_pin_finding(info: speakers.SpeakerInfo) -> Finding | None:
    """Ask for the *negative* signal: one speaker pin, spare output pins, and
    no upstream fixup for this machine.

    The table above only knows machines upstream Linux has already been told
    about. A laptop whose woofers are hidden and whose subsystem id nobody has
    reported yet looks exactly like a genuine 2-driver laptop from here — and
    both are common. The manufacturer's spec sheet settles it in seconds, but
    only the owner can look it up, so the ask goes to them.

    Deliberately narrow. Spare output-capable pins are ordinary (the
    development machine has two), so this stays quiet unless the machine is
    also down to a single speaker pin — the one shape where a driver could
    plausibly be missing. Silent when a quirk already matched: that run has a
    real fix to offer and doesn't need a question competing with it.
    """
    if info.bus_type != "hda" or not info.unconfigured_pins:
        return None
    if len(info.speakers) != 1 or speakers.find_hidden_speaker_pin(info):
        return None
    spare = ", ".join(p.node for p in info.unconfigured_pins)
    # Names the pins rather than pointing at a table: the only place this
    # detail prints during a normal run is here, with no --speaker-info
    # output above it to refer back to.
    return Finding(
        slug="speaker-count", kind="ask",
        detail=f"Linux configured one internal speaker pin on this machine "
               f"({info.speakers[0].node}), and left {spare} unused though "
               "they can drive output. That is normal on a device with a "
               "single stereo pair — most machines have spare pins. It is "
               "only wrong if your device really has more speakers than that: "
               "then a kernel fix is missing for your exact model. Tell us "
               "and we can suggest a setting to test — the fix itself has to "
               "land in Linux, which is outside this project.",
        ask="Does your device have more speakers than the single pair Linux "
            "found? (issue #53)")


def get_distro_pretty_name(os_release=Path("/etc/os-release")) -> str:
    """Read PRETTY_NAME from /etc/os-release (e.g. "Fedora Linux 44"), or "".

    Only PRETTY_NAME — no hostname, machine-id, or serials. A missing or
    unreadable file, or an absent key, yields "" so the caller drops the line.
    """
    try:
        text = Path(os_release).read_text()
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith("PRETTY_NAME="):
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def _gather_speaker_pins() -> speakers.SpeakerInfo:
    """Just enough of a SpeakerInfo to answer find_hidden_speaker_pin.

    A few /proc reads and one sysfs walk. Kept apart from
    ``_gather_speaker_info`` deliberately: that one also shells out to
    ``amixer`` per card and to ``journalctl``/``dmesg`` (seconds, on a machine
    with a big journal), globs /lib/firmware, and honours the demo-injection
    env vars — all of it for the amp-status report, which a default run never
    prints. A normal conversion must not pay for it.
    """
    import platform

    info = speakers.SpeakerInfo(kernel=platform.release())
    cards_path = Path("/proc/asound/cards")
    if cards_path.exists():
        info.sound_cards = [l.strip() for l
                            in cards_path.read_text().strip().splitlines()]
    info.hda_codecs = codecs.get_hda_codec_ids()
    info.soundwire_devices = codecs.get_soundwire_ids()   # decides bus_type
    info.pci_subsystem = codecs.get_pci_audio_subsystem()
    # Asked first, not folded into the condition below: injecting the demo is
    # what makes bus_type read "hda", so testing bus_type first would skip it.
    if not speakers._maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "hda":
            speakers._detect_hda_speakers(info)
    return info


def _gather_speaker_info() -> speakers.SpeakerInfo:
    """Collect all audio hardware information into a SpeakerInfo."""
    import platform

    info = speakers.SpeakerInfo(kernel=platform.release(), distro=get_distro_pretty_name())

    # System identity
    for attr, path in [("product", "/sys/class/dmi/id/product_name"),
                       ("family", "/sys/class/dmi/id/product_family")]:
        p = Path(path)
        if p.exists():
            setattr(info, attr, p.read_text().strip())

    # Sound cards
    cards_path = Path("/proc/asound/cards")
    if cards_path.exists():
        info.sound_cards = [l.strip() for l in cards_path.read_text().strip().splitlines()]

    # Bus-agnostic detection
    info.hda_codecs = codecs.get_hda_codec_ids()
    info.soundwire_devices = codecs.get_soundwire_ids()
    info.pci_subsystem = codecs.get_pci_audio_subsystem()

    # PCM playback devices
    for card_dir in sorted(Path("/proc/asound").glob("card*")):
        for pcm_dir in sorted(card_dir.glob("pcm*p")):
            info_path = pcm_dir / "info"
            if not info_path.exists():
                continue
            fields = {}
            for line in info_path.read_text().splitlines():
                if ": " in line:
                    k, v = line.split(": ", 1)
                    fields[k.strip()] = v.strip()
            info.pcm_devices.append((fields.get("device", "?"), fields.get("id", "?")))

    # Speaker detection — branch by bus type, unless a demo machine stands in
    if not speakers._maybe_demo_hidden_speaker_pin(info):
        if info.bus_type == "soundwire":
            speakers._detect_soundwire_speakers(info)
        elif info.bus_type == "hda":
            speakers._detect_hda_speakers(info)

    # Bus-agnostic: a TI smart-amp firmware gate sits on the SOF/HDA card
    # regardless of how the speakers themselves are wired.
    info.firmware_gates = speakers.detect_speaker_firmware_gates()

    # Merged amp-status evidence (firmware presence + kernel-log markers),
    # unless a demo override is requested for previewing the section.
    if not speakers._maybe_demo_amp_status(info):
        amps._gather_amp_evidence(info)

    return info


def _amp_status_lines(info: speakers.SpeakerInfo) -> list[str]:
    """Build the compact "Speaker amplifier status" body — raw evidence, no verdict.

    Terse by default (a one-line bound-amp summary) and shows actual kernel-log
    lines rather than a health verdict, because nothing in the log reliably
    proves an amp is voicing correctly. Only an enumerated-but-unbound device,
    an off firmware gate (#17), and a narrow set of unambiguous kernel-log
    errors are flagged; the rest is shown for the reader to interpret.
    """
    lines: list[str] = []
    bound = [a for a in info.amp_status if a.bound]
    unbound = [a for a in info.amp_status if not a.bound]

    if bound:
        drivers = ", ".join(sorted({a.driver for a in bound})) or "unknown"
        chans = sorted({a.channels for a in bound if a.channels})
        ch_str = "/".join(f"{c}ch" for c in chans) if chans else "?ch"
        lines.append(f"  {len(bound)} amplifier(s) bound ({drivers}); {ch_str}")
    if unbound:
        # Neutral: an unbound slave may be a non-amp peripheral (jack codec,
        # DMIC) or one still binding — not necessarily a silent speaker.
        names = ", ".join(a.node for a in unbound)
        lines.append(f"  {len(unbound)} SoundWire device(s) with no driver bound "
                     f"(may be non-amp or still binding): {names}")

    # #17 TI firmware gate, folded into the unified view (HDA or SoundWire).
    # The symptom stays open-ended for the same reason the finding's copy
    # does: the amp drives only the woofers on most laptops, but where it
    # drives every speaker an off gate makes everything thin or crackly
    # rather than silencing the bass (#39).
    for g in info.firmware_gates:
        mark = "" if g.on else "⚠ "
        state = "on" if g.on else "OFF — speakers may be silent, thin or crackly"
        lines.append(f"  {mark}{g.name}: {state} (card {g.card_id})")
        if not g.on:
            # Both --speaker-info and --doctor end in this section, so it is
            # the only place either of them can hand over the fix. Its own
            # line, unwrapped, because it has to survive a copy-paste.
            lines.append(f"      turn it on:  {speakers.amixer_enable_cmd(g)}")

    # Driver-keyed firmware presence (only when a smart-amp driver is loaded).
    if info.amp_firmware:
        extra = f", …+{len(info.amp_firmware) - 1} more" if len(info.amp_firmware) > 1 else ""
        lines.append(f"  Firmware: {len(info.amp_firmware)} file(s) present "
                     f"(e.g. {info.amp_firmware[0]}{extra}); presence is generic — "
                     "the kernel log decides whether this model's blob loaded")
    elif info.amp_firmware_missing:
        # Neutral: absence isn't proof — the blob may live outside the searched
        # roots, or under an SSID-specific name we can't predict.
        lines.append("  Firmware: none found under /lib/firmware — could not "
                     "confirm (see the kernel log)")

    # Self-check grep derived from the keywords we actually scanned, so the
    # printed command can't contradict what the report found.
    grep = info.amp_log_grep or "cs35l|tas2|cirrus"
    grep_hint = f"journalctl -k -b | grep -iE '{grep}'"

    # Kernel-log evidence: show the lines, flag only unambiguous errors.
    if not info.amp_log_available:
        lines.append(f"  Kernel log: not accessible — run:  {grep_hint}")
    elif info.amp_log:
        errors = [l for is_err, l in info.amp_log if is_err]
        if errors:
            lines.append("  ⚠ Kernel log — amp firmware/init error:")
            lines += [f"      {l}" for l in errors[:3]]
            # Surface the cap (no silent truncation) and the command to read the
            # full log — the matched lines are a sample, not the whole story.
            tail = f" (+{len(errors) - 3} more)" if len(errors) > 3 else ""
            lines.append(f"      see full log{tail}:  {grep_hint}")
        else:
            lines.append(f"  Kernel log: {len(info.amp_log)} amp line(s), no known "
                         "failure marker — scan isn't exhaustive, so read them yourself:")
            lines.append(f"      {grep_hint}")
    elif info.amp_firmware_missing:
        # Firmware looked missing but the current boot log has no amp lines (e.g.
        # rotated out) — still point at the log rather than dangle the reference.
        lines.append(f"  Kernel log: no amp lines this boot — inspect:  {grep_hint}")

    return lines or ["  (no smart amplifier detected)"]


def _print_speaker_info(info: speakers.SpeakerInfo):
    """Print the collected speaker info report."""
    sections = []

    # System
    lines = []
    if info.product:
        lines.append(f"  Product: {info.product}")
    if info.family:
        lines.append(f"  Family:  {info.family}")
    if info.distro:
        lines.append(f"  OS:      {info.distro}")
    kernel_line = f"  Kernel:  {info.kernel}"
    # Age annotation (issue #33): makes a pasted report self-triaging — an old
    # series is a real bad-sound suspect regardless of the preset.
    series = environment.parse_kernel_series(info.kernel)
    aged = environment._kernel_series_age(series, date.today()) if series else None
    if aged:
        released, months = aged
        plural = "" if months == 1 else "s"
        kernel_line += (f" (series {series[0]}.{series[1]}, released {released}"
                        f" — ~{months} month{plural} old)")
    lines.append(kernel_line)
    sections.append(("System", lines))

    # Sound cards
    sections.append(("Sound cards",
                      [f"  {c}" for c in info.sound_cards] or ["  (none found)"]))

    # HDA codecs
    sections.append(("HDA codecs",
                      [f"  {name or 'Unknown'} — Vendor: 0x{v}  Subsystem: 0x{s}"
                       for v, s, name in info.hda_codecs]
                      or ["  (none)"]))

    # SoundWire devices
    sections.append(("SoundWire devices",
                      [f"  Manufacturer: 0x{m}  Part: 0x{p}" for m, p in info.soundwire_devices]
                      or ["  (none)"]))

    # PCI audio subsystem
    pci_line = f"  Subsystem: {info.pci_subsystem[0]}:{info.pci_subsystem[1]}" if info.pci_subsystem else "  (none)"
    sections.append(("PCI audio subsystem", [pci_line]))

    # Speaker amplifiers / HDA pins (bus-specific section)
    if info.bus_type == "soundwire":
        amp_lines = [f"  Codec: {c}" for c in info.sdw_codecs]
        amp_lines += [f"  Amplifier: {a}" for a in info.sdw_amplifiers]
        if not info.sdw_amplifiers:
            amp_lines.append("  (no speaker amplifiers detected)")
        sections.append(("Speaker amplifiers", amp_lines))
    elif info.bus_type == "hda" and (info.speakers or info.unconfigured_pins):
        speaker_lines = [
            f"  {s.node}: {s.control_name} ({s.role}, "
            f"{'stereo' if s.channels == 2 else 'mono'})"
            # The tag is what makes the fix verifiable: the firmware still
            # calls such a pin unconnected, so without it an applied quirk and
            # a BIOS-declared speaker are the same line (issue #53).
            + (f" [{s.override}]" if s.override else "")
            for s in info.speakers
        ] or ["  (none configured)"]
        if info.unconfigured_pins:
            # Raw evidence, and one verdict where we have one: these are
            # usually spare pins, but a speaker pin the BIOS wrongly calls
            # unconnected looks identical (issue #53) — except on the machines
            # upstream ships a fix for, where the quirk table names the pin.
            # Marking it is what keeps this section from talking a reader out
            # of a fix the same report just handed them.
            found = speakers.find_hidden_speaker_pin(info)
            flagged = set(found[2]) if found else set()
            speaker_lines.append("  Output-capable pins left unconfigured:")
            speaker_lines += [
                f"    {p.node}: pincap {p.pincap}, default {p.pin_default}"
                + ("  ⚠ a kernel fix declares this a speaker"
                   if p.node in flagged else "")
                for p in info.unconfigured_pins
            ]
            # Said here because this section is what a reader stares at: spare
            # pins are ordinary, and a list of them is not a fault report.
            # Wrapped to the terminal like the rest of this tool's prose —
            # rich is handed soft_wrap=True and never reflows — with the
            # continuation hanging under the opening bracket.
            speaker_lines += textwrap.wrap(
                ("(the unflagged ones are normal — a spare pin only matters "
                 "if your device has more speakers than are listed above)"
                 if flagged else
                 "(spare pins are normal — this only matters if your device "
                 "has more speakers than are listed above)"),
                width=console._wrap_width(), initial_indent="    ",
                subsequent_indent="     ", break_on_hyphens=False)
        sections.append(("HDA internal speakers", speaker_lines))

    # PCM playback devices
    sections.append(("PCM playback devices",
                      [f"  pcm{dev}p: {name}" for dev, name in info.pcm_devices]))

    # Merged, bus-agnostic amplifier status: per-amp bind/channels/runtime, the
    # #17 TI firmware gate, driver-keyed firmware presence, and kernel-log
    # evidence — one section, kept terse (detail only when something's wrong).
    sections.append(("Speaker amplifier status", _amp_status_lines(info)))

    # Speaker layout estimate. It counts what Linux configured, so on a machine
    # with a pin fix missing it states the very number the warning above says
    # is wrong — read as a bottom line, that talks the reader out of the fix.
    layout = f"  {info.layout_summary}"
    if info.bus_type == "hda" and speakers.find_hidden_speaker_pin(info):
        layout += " (what Linux drives — the flagged pin above would add more)"
    sections.append(("Speaker layout estimate", [layout]))

    for title, lines in sections:
        console.cprint("head", f"=== {title} ===")
        print("\n".join(lines))
        print()


def report_speaker_info():
    """Report detected audio hardware and speaker layout."""
    # Version-stamp the block: users paste this verbatim into the device-report
    # issue form, so the maintainer can see which build was tested.
    console.cprint("head", f"speaker-tuning-to-easyeffects {version.get_version()}")
    print()
    info = _gather_speaker_info()
    _print_speaker_info(info)


def speaker_pin_status(info: speakers.SpeakerInfo) -> CheckResult | None:
    """Verdict line for a speaker pin the firmware hides, or None when this
    machine isn't one upstream has had to fix (nearly all of them — a PASS for
    a quirk that was never needed is noise).

    WARN, not FAIL, on the same reasoning as the kernel-age check: the match is
    machine-exact, but only the user can confirm they hear no bass, and a
    2-driver laptop that somehow matched would be unharmed by ignoring this.

    The procedure rides in ``steps``, unwrapped, rather than in the detail:
    the whole fix is here, so nobody has to re-run the tool a different way to
    reach it. Where the fixup has no forcible name the builder returns nothing
    and ``upgrade_prospect`` has already said why, so the check promises no
    command it won't print.
    """
    found = speakers.find_hidden_speaker_pin(info)
    if not found:
        return None
    quirk, codec_ssid, missing = found
    return CheckResult(
        DOCTOR_WARN, "Speaker pins",
        f"upstream Linux carries a fix for this exact model that declares "
        f"{_pin_phrase(missing)} on codec {codec_ssid} an internal speaker, "
        "and your kernel isn't applying it — those speakers get no signal, "
        "whatever the preset does. "
        + upgrade_prospect(quirk, info.kernel),
        # Same width the printer will wrap the detail to, less its indent, so
        # the prose here lines up with the prose above it.
        steps=speaker_pin_fix_steps(quirk, missing,
                                    speakers._card_uses_sof(info.sound_cards),
                                    console._wrap_width() - 9,
                                    speaker_info_below=True))
