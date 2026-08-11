"""Assembling the stages into one EasyEffects output preset.

`make_preset` is the only place that decides what ships. It walks the chain in
Dolby's own order (dialog → leveler → compressor → regulator → limiter), asks
`bands.py` and `plugins.py` for each stage, and returns the preset dict
alongside the set of flag-actionable names the run actually emitted — recorded
inline with each emission branch, so the end-of-run menu cannot claim a stage
the JSON does not contain.

It also owns the one placement decision that is not a mapping: which slot the
static volmax boost (and `--enable level-restore`'s giveback) is injected
into. Both are measured trade-offs rather than XML fields; the evidence is in
`docs/design-notes.md`.

Imports `plugins.py`, so it reaches numpy too and stays behind the generator's
function-local imports for it.

The stage builders come in as bare names because they are arithmetic over
their arguments, with no state a patch would have to reach.
"""

from __future__ import annotations

from lib import version
from lib.preset.bands import make_convolver, make_peq_eq
from lib.preset.plugins import (
    _coupled_bands_eligible,
    bass_enhancer_from_peq,
    make_autogain,
    make_dialog_enhancer,
    make_limiter,
    make_multiband_compressor,
    make_regulator,
)


# NOTE: there is deliberately no surround→stereo-widening builder. Earlier
# revisions mapped `surround-boost` to a Calf Stereo Tools `stereo-base`
# widening (commit 82d7f3d). A 2026-06-13 DAX capture on the X1 Yoga
# falsified that mapping: on 2-channel content DAX applies *zero* stereo
# widening — `surround-boost=96` (movie) is identical to `surround-boost=0`
# (game) to 0.01 dB RMS in both L and R, and leaves the L/R correlation
# untouched (no magnitude M/S rebalance, no phase decorrelation). The field
# is a virtualization/surround-render depth control that is dormant without
# a multichannel/object bed, not a stereo-width knob — so the faithful
# stereo-playback behaviour is to not widen. See docs/design-notes.md,
# unvalidated-scaling entry 2. (The converter keeps `emit_stereo_tools`, in
# lib/pipewire/plugins.py, as a translator for any preset that still carries
# a stereo_tools block.)


def make_preset(kernel_name: str, peq_filters: list[dict],
                vol_leveler: dict | None = None,
                dialog_enhancer: dict | None = None,
                mb_comp: dict | None = None, regulator: dict | None = None,
                freqs: list[int] | None = None,
                is_soundwire: bool = False, volmax_boost: float = 0.0,
                volmax_slot: str = "input-gain",
                fir_peak_db: float = 0.0,
                enabled: set[str] | None = None,
                disabled: set[str] | None = None) -> tuple[dict, set[str]]:
    """Build a preset dict.

    Returns (preset, emitted) where emitted is the set of flag-actionable
    names for a rerun: DISABLEABLE_FILTERS names that actually ran
    (--disable candidates) plus ENABLEABLE_FILTERS names that shipped
    present but inactive (--enable candidates). Tracked inline with each
    emission branch so the set can't drift from what is in the returned
    dict.
    """
    enabled = enabled or set()
    disabled = disabled or set()
    emitted = set()
    preset = {
        "_generator": f"dolby_to_easyeffects.py {version.get_version()}",
        "output": {
            "blocklist": [],
            "convolver#0": make_convolver(kernel_name),
            "plugins_order": ["convolver#0"],
        }
    }

    # SoundWire speakers lack Dolby's proprietary Virtual Bass Enhancement
    # (VBE) that runs in the Windows driver. Compensate with psychoacoustic
    # harmonic generation so small speakers still produce perceived bass.
    if is_soundwire and "bass-enhancer" not in disabled:
        preset["output"]["bass_enhancer#0"] = bass_enhancer_from_peq(
            peq_filters)
        preset["output"]["plugins_order"].append("bass_enhancer#0")
        emitted.add("bass-enhancer")

    # No stereo widening: `surround-boost` is a virtualization-render-depth
    # control, dormant on 2-channel content — DAX applies no stereo widening
    # on stereo playback (design-notes entry 2). Earlier revisions emitted a
    # stereo_tools#0 widener here.

    effective_peq = peq_filters
    if "high-shelf" in disabled:
        effective_peq = [f for f in effective_peq if f["type"] != 3]
    if "lo-pass" in disabled:
        effective_peq = [f for f in effective_peq if f["type"] not in (6, 8)]
    peq = make_peq_eq(effective_peq)
    if peq:
        preset["output"]["equalizer#0"] = peq
        preset["output"]["plugins_order"].append("equalizer#0")
        if any(f["type"] == 3 for f in effective_peq):
            emitted.add("high-shelf")
        if any(f["type"] in (6, 8) for f in effective_peq):
            emitted.add("lo-pass")

    # Dialog enhancer (speech presence boost) before the volume leveler,
    # matching Dolby's CP order: DE → IEQ → Volume Leveler.
    if "dialog" not in disabled:
        de = make_dialog_enhancer(dialog_enhancer)
        if de:
            preset["output"]["equalizer#1"] = de
            preset["output"]["plugins_order"].append("equalizer#1")
            emitted.add("dialog")

    # Autogain (volume leveler) goes before the compressor/regulator to match
    # Dolby's signal flow: CP (volume leveler) → VLLDP (compressor → regulator).
    # This lets the compressor and regulator catch any overshoot from the leveler.
    if "autogain" not in disabled:
        autogain = make_autogain(vol_leveler, conservative=is_soundwire,
                                 enabled="autogain" in enabled)
        if autogain:
            preset["output"]["autogain#0"] = autogain
            preset["output"]["plugins_order"].append("autogain#0")
            if autogain["bypass"]:
                emitted.add("autogain")  # actionable via --enable on a rerun
            else:
                # Marker (not an ENABLEABLE_FILTERS key, so it never reaches
                # the hint block): lets main() tell "--enable autogain worked"
                # from "the XML's leveler is disabled, so the flag did
                # nothing".
                emitted.add("autogain-active")

    if "mbc" not in disabled:
        mbc = make_multiband_compressor(mb_comp, freqs)
        if mbc:
            preset["output"]["multiband_compressor#0"] = mbc
            preset["output"]["plugins_order"].append("multiband_compressor#0")
            emitted.add("mbc")
            if mb_comp and mb_comp["group_count"] == 1:
                emitted.add("mbc-1band")

    # volmax-boost injection: regulator input-gain is the default slot (issue
    # #23) — placed pre-band-limiting so the per-band compression tames the
    # boosted low end before the brickwall, instead of feeding the full static
    # makeup straight into it (volmax-boost is a CP-stage volume-leveler
    # ceiling, not a Dolby-documented placement; this is a pragmatic
    # approximation). --volmax-slot output-gain opts back into the pre-#23
    # post-band placement. If the regulator is disabled or absent from the XML,
    # fall back to limiter#0 input-gain so the boost still happens. Never both.
    # volmax_slot only re-routes the regulator path; the limiter fallback is
    # unaffected.
    apply_volmax = volmax_boost if "volmax" not in disabled else 0.0
    # --enable level-restore rides the same slot rather than adding a stage of
    # its own: it is a static broadband gain like volmax-boost, and issue #23
    # measured what the placement is worth (0.06% THD pre-band-limiting vs
    # 11.6% straight into the brickwall). fir_peak_db is what make_fir divided
    # out of the impulse response, so this restores a measured quantity rather
    # than applying an offset. --disable volmax drops only its own term; the
    # two are independent.
    level_restore = fir_peak_db if "level-restore" in enabled else 0.0
    static_boost = apply_volmax + level_restore
    reg = None
    if "regulator" not in disabled:
        reg = make_regulator(regulator, freqs, volmax_boost=static_boost,
                             volmax_slot=volmax_slot,
                             couple_bands="coupled-bands" not in disabled)
    if reg:
        preset["output"]["multiband_compressor#1"] = reg
        preset["output"]["plugins_order"].append("multiband_compressor#1")
        emitted.add("regulator")
        limiter_boost = 0.0
        # A band that is enabled at a >= 0 dB threshold can only come from
        # the coupled-bands mapping — the default path disables those.
        coupled_fired = any(
            reg[f"band{i}"]["compressor-enable"]
            and reg[f"band{i}"]["attack-threshold"] >= 0
            for i in range(8))
        if coupled_fired:
            # Marker, not a DISABLEABLE_FILTERS key in its own right: it is
            # what _DISABLE_MENU_MARKER keys the --disable coupled-bands row
            # off, so the row appears only where the mapping actually put a
            # zone in — same contract as autogain-active above.
            emitted.add("coupled-bands-active")
        elif ("coupled-bands" in disabled
                and _coupled_bands_eligible(regulator)):
            # The opt-out actually removed zones. It needs a marker of its
            # own because the -active one cannot serve here: --disable
            # forces couple_bands off, so `coupled_fired` is false on every
            # run that passes the flag, and keying the "had no effect"
            # warning off its absence made that warning fire on ~99% of
            # opt-out runs — including this one, where 16 zones were
            # dropped. Autogain has no such inversion (--enable is what
            # sets its marker), which is how mirroring it introduced this.
            emitted.add("coupled-bands-dropped")
    else:
        limiter_boost = static_boost

    if apply_volmax > 0:
        emitted.add("volmax")
    if level_restore != 0:
        # Marker, not an --enable candidate: the flag is already on when this
        # fires. Same contract as autogain-active/coupled-bands-active.
        # `!= 0`, not `> 0`: a curve that only cuts normalises to a peak below
        # unity, so make_fir *adds* gain there and restoring it is negative.
        # No corpus XML does that (0 of 3051 checked 2026-08-04), but the
        # marker should track "the flag changed the output", not its sign.
        emitted.add("level-restore-active")
    elif fir_peak_db > apply_volmax:
        # Offer the flag only where it would do something (the precedent is
        # coupled-bands, 619a663). The gate is the deficit itself: the
        # convolver gives back fir_peak_db less than the tuning asks for, and
        # only the static boost puts any of it back — so a peak above it is
        # a preset that plays quieter than bypass. Below it there is nothing
        # to restore and the menu stays quiet.
        emitted.add("level-restore")

    # Brickwall limiter at the end as a safety net
    preset["output"]["limiter#0"] = make_limiter(input_gain=limiter_boost)
    preset["output"]["plugins_order"].append("limiter#0")

    return preset, emitted
