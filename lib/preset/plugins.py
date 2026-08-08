"""One builder per non-EQ stage: leveler, compressors, bass, limiter.

Each function turns one parsed Dolby tuning block into the parameter dict for
the EasyEffects plugin standing in for it. None of them decides whether the
stage ships — `build.py` does — and none of them writes the copy a user reads
about it, which is `lib/report/messages.py`'s `--disable`/`--enable` menus.

The Q15 decoders sit here rather than in `lib/dax/parse.py` because they are
the *mapping*, not the parse: `parse_xml` hands over the stored integers, and
turning them into the milliseconds and ratios LSP wants is a hypothesis about
what Dolby meant by them (CLAUDE.md, "XML-only derivability").

**This module reaches numpy transitively** — `lib.preset.fir`, for the sample
rate the MBC time constants decode against — so `dolby_to_easyeffects.py`
reaches it only through the function-local imports in `main()`, never at the
top of the file: the same deferral `fir.py`'s own docstring explains.

`make_band`, which `make_dialog_enhancer` builds its speech bell with, comes
in as a bare name: it is arithmetic over its arguments, with no state a patch
would have to reach.
"""

from __future__ import annotations

import math

from lib import console
from lib.dax import parse
from lib.preset import fir
from lib.preset.bands import make_band


def make_dialog_enhancer(dialog_enhancer: dict | None) -> dict | None:
    """Dialog enhancer mapped as a broad speech-band EQ boost.

    Dolby's dialog enhancer (DE) isolates speech frequencies and
    selectively boosts them. We approximate this with a broad Bell
    filter centered at 2.5 kHz (speech presence region), with gain
    scaled by the DE amount (0-16 scale): amount/16 * 6 dB, giving a
    maximum of +6 dB.

    (An earlier SoundWire-only variant used a stronger *8 mapping plus
    a 4 kHz "clarity" bell — removed: it was calibrated against the
    pre-#13 chain whose over-applied IEQ crushed the treble it was
    compensating; see design-notes unvalidated-scaling entry 1.)
    """
    if not dialog_enhancer:
        return None

    amount = dialog_enhancer["amount"]

    gain = round(amount / parse.DB_FIXED_POINT_SCALE * 6.0, 2)
    if gain <= 0:
        return None

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "mode": "IIR",
        "num-bands": 1,
        "split-channels": False,
        "left": {"band0": make_band(2500.0, gain, q=0.7)},
        "right": {"band0": make_band(2500.0, gain, q=0.7)},
    }


def make_autogain(vol_leveler: dict | None,
                  conservative: bool = False,
                  enabled: bool = False) -> dict | None:
    """Autogain plugin mapping from Dolby volume leveler.

    The Dolby volume leveler brings quiet passages up to a target loudness.
    EasyEffects' autogain does the same using EBU R 128 loudness measurement.

    Dolby volume-leveler-amount (0-10) maps to aggressiveness:
      0 = gentle (long history window)
      10 = aggressive (short history window)

    For HDA presets: bypassed by default. EE's leveler has no equivalent
    of Dolby's MI steering: it boosts legitimate quiet content (a low
    background under intermittent speech, ~+14 dB measured) and each loud
    onset then rides ~4 dB of overshoot into the downstream dynamics —
    audible saturation, measured independent of `maximum-history`
    (design-notes). `--enable autogain` (enabled=True) opts in for the
    ~+9 dB program loudness it brings (issue #25). Either way the silence
    gate ships at -50 dB — the #25 field-confirmed fix for crackle on
    short sounds arriving after silence — so manual GUI enabling is safe.

    For SoundWire presets (conservative=True): active with gentler
    settings — a -6 dB target offset and a longer history window.
    """
    if not vol_leveler or not vol_leveler["enable"]:
        return None

    amount = vol_leveler["amount"]
    target = vol_leveler["out_target"]

    if conservative:
        max_history = max(40 - amount * 4, 15)
        target -= 6.0
    else:
        max_history = max(30 - amount * 5, 10)
    return {
        "bypass": not (conservative or enabled),
        "input-gain": 0.0,
        "output-gain": 0.0,
        "maximum-history": max_history,
        "reference": "Geometric Mean (MSI)",
        "silence-threshold": -50.0,
        "target": round(target, 1),
    }


# Dolby DSP coefficients (MBC gain/attack/release) are Q15 fixed point:
# the stored int divided by 2^15 gives the fractional value; 2^15 is "unity".
Q15_SCALE = 32768.0
# The Dolby MB compressor operates per block of this many samples (not per
# sample), so time-constant decoding converts via blocks-per-second.
MBC_BLOCK_SIZE = 256


def decode_mbc_time_constant(coeff: int, block_size: int = MBC_BLOCK_SIZE) -> float:
    """Decode a Dolby time constant coefficient to milliseconds.

    Dolby stores time constants as exponential smoothing coefficients
    in Q15 fixed-point format, operating per block (not per sample).
    coeff/32768 = (1 - alpha), where alpha = 1 - exp(-1/(tau * blocks_per_sec)).
    """
    blocks_per_sec = fir.SAMPLE_RATE / block_size
    one_minus_alpha = coeff / Q15_SCALE
    if one_minus_alpha <= 0.0 or one_minus_alpha >= 1.0:
        return 100.0  # fallback
    tau = -1.0 / (blocks_per_sec * math.log(one_minus_alpha))
    return tau * 1000.0  # seconds to ms


# LSP MBC/limiter release-threshold floor: parked just under -80 dB so the
# release stage effectively never re-triggers. Shared by the disabled and
# active band builders.
MBC_RELEASE_THRESHOLD_FLOOR = -80.01


def _disabled_band() -> dict:
    """The LSP 'band off' parameter dict, shared by make_multiband_compressor
    and make_regulator (the literal was byte-identical in both).

    Key order and the trap-fix values are load-bearing: the preset JSON
    preserves insertion order, and design-notes track compression-mode
    "Downward" (over LSP's "Upward" default), boost-amount 0.0, and
    enable-band False as the LSP defaults that must be explicitly overridden.
    Returns a fresh dict each call so each band gets its own object.
    """
    return {
        "enable-band": False,
        "compressor-enable": False,
        "mute": False,
        "solo": False,
        "attack-threshold": -12.0,
        "attack-time": 20.0,
        "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
        "release-time": 100.0,
        "ratio": 1.0,
        "knee": -6.0,
        "makeup": 0.0,
        "compression-mode": "Downward",
        "sidechain-type": "Internal",
        "sidechain-mode": "RMS",
        "sidechain-source": "Middle",
        "stereo-split-source": "Left/Right",
        "sidechain-lookahead": 0.0,
        "sidechain-reactivity": 10.0,
        "sidechain-preamp": 0.0,
        "sidechain-custom-lowcut-filter": False,
        "sidechain-custom-highcut-filter": False,
        "sidechain-lowcut-frequency": 10.0,
        "sidechain-highcut-frequency": 20000.0,
        "boost-threshold": -60.0,
        "boost-amount": 0.0,
    }


def decode_mbc_bands(mb_comp: dict | None) -> list[dict]:
    """Decode Dolby mb-compressor band_groups into per-band dynamics dicts.

    Single source of truth for the MBC band decode: both
    ``make_multiband_compressor`` (the LSP builder) and the main()
    diagnostics printer (`_report_parsed_profile`) call this so they can
    never drift. Returns a list of dicts, one per emitted band, each with
    keys: ``xover_idx``, ``threshold`` (dB), ``ratio`` (x:1),
    ``attack_ms``, ``release_ms``, ``makeup`` (dB).

    PURE — no printing or warnings. The R5 out-of-range fallback warnings
    (ratio clamp, attack/release Q15-range fallbacks) are emitted by the
    builder only, so they fire exactly once per band per run (this decode
    is also called by the silent diagnostics path). Out-of-range values
    are still *handled* here (ratio clamps to 100.0, time constants fall
    back via ``decode_mbc_time_constant``) so the returned values match
    what the builder emits — the builder just additionally warns.

    Band selection mirrors the builder: at most ``group_count`` bands,
    capped by the number of band_groups parsed and LSP's 8-band limit.
    Returns ``[]`` when there is nothing to decode.
    """
    if not mb_comp:
        return []

    band_groups = mb_comp["band_groups"]
    n_bands = min(mb_comp["group_count"], len(band_groups), 8)
    if n_bands < 1:
        return []

    decoded = []
    for bg in band_groups[:n_bands]:
        xover_idx, thresh_raw, gain_raw, attack_raw, release_raw, makeup_raw = bg
        threshold = thresh_raw / parse.DB_FIXED_POINT_SCALE
        # gain_coeff → ratio: 32767 = 1:1 (bypass), lower = more compression
        gain_frac = gain_raw / Q15_SCALE
        # out-of-range gain → clamp to practical max (builder warns)
        ratio = 1.0 / gain_frac if gain_frac > 0.01 else 100.0
        attack_ms = decode_mbc_time_constant(attack_raw)
        release_ms = decode_mbc_time_constant(release_raw)
        makeup = makeup_raw / parse.DB_FIXED_POINT_SCALE
        decoded.append({
            "xover_idx": xover_idx,
            "threshold": threshold,
            "ratio": ratio,
            "attack_ms": attack_ms,
            "release_ms": release_ms,
            "makeup": makeup,
        })
    return decoded


def make_multiband_compressor(mb_comp: dict | None,
                              freqs: list[int]) -> dict | None:
    """Multi-band compressor mapping from Dolby mb-compressor-tuning.

    The Dolby MB compressor uses raw DSP coefficients in 6-tuples:
      [crossover_band_idx, threshold_q4, gain_coeff_q15,
       attack_coeff_q15, release_coeff_q15, makeup_q4]

    Where:
      - crossover_band_idx: index into the 20-band frequency table.
        For each band i, this is the *upper* edge of that band; the
        last band's value is a sentinel (typically len(freqs) = 20)
        meaning "up to Nyquist".
      - threshold: in 1/16 dB
      - gain_coeff: Q15 fixed-point, 32767 = unity (bypass)
        ratio ≈ 1 / (gain_coeff / 32768)
      - attack/release: exponential smoothing coefficients (block-rate)
      - makeup: in 1/16 dB

    Corpus composition (1050-XML cohort, MBC-enabled rows): 1 band on
    294 profiles (music-dominated, fast attack/release used as a
    loudness maximiser with full-band ratio up to 2:1), 2 bands on
    561, 3 on 175, 4 on 121. LSP MBC supports 8 bands max, so any
    value above that would be clipped — but Dolby's schema only
    allocates 4 band_group_N elements. For group_count=1 the single
    band covers the whole spectrum (no split frequency); bands 1-7
    in the emitted config stay disabled via enable-band=False.
    """
    if not mb_comp:
        return None

    decoded = decode_mbc_bands(mb_comp)
    n_bands = len(decoded)
    if n_bands < 1:
        return None
    band_groups = mb_comp["band_groups"]

    # R5 fallback warnings about the EMITTED dynamics. decode_mbc_bands is
    # pure/silent (it is also called by the main() diagnostics, which must
    # not re-warn), so the warnings live here in the builder path only —
    # firing exactly once per affected band per run. Walk the decoded bands
    # alongside their raw band_groups to inspect the original coefficients.
    for i, (b, bg) in enumerate(zip(decoded, band_groups[:n_bands])):
        _, _, gain_raw, attack_raw, release_raw, _ = bg
        if not gain_raw / Q15_SCALE > 0.01:
            console.warn(f"MBC band {i} gain coeff {gain_raw} "
                 f"out of range — clamping ratio to {b['ratio']:.0f}:1")
        if not 0 < attack_raw < Q15_SCALE:
            console.warn(f"MBC band {i} attack coeff {attack_raw} "
                 f"out of range — using {b['attack_ms']:.0f} ms fallback")
        if not 0 < release_raw < Q15_SCALE:
            console.warn(f"MBC band {i} release coeff {release_raw} "
                 f"out of range — using {b['release_ms']:.0f} ms fallback")

    # Crossovers between adjacent bands. Band i ends at freqs[decoded[i].xover_idx];
    # band i+1's lower edge is the same frequency. Only the first n_bands - 1
    # crossovers are meaningful — the last band's xover_idx is the high-cap
    # sentinel and isn't used as a split point.
    def xover_to_freq(idx, fallback):
        if 0 <= idx < len(freqs):
            return float(freqs[idx])
        return fallback

    crossovers = [xover_to_freq(decoded[i]["xover_idx"], 500.0)
                  for i in range(n_bands - 1)]

    result = {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i < n_bands:
            b = decoded[i]
            # Band i sits between its lower edge (crossovers[i-1] for i>0,
            # else 0/DC) and its upper edge (crossovers[i] for i<n_bands-1,
            # else 20 kHz Nyquist).
            lower = crossovers[i - 1] if i > 0 else 10.0
            upper = crossovers[i] if i < n_bands - 1 else 20000.0
            band = {}
            if i > 0:
                # Band 0 is always enabled with no split-frequency; bands 1+
                # need both fields set so LSP MBC actually splits at lower.
                band["enable-band"] = True
                band["split-frequency"] = lower
            band.update({
                "compressor-enable": True,
                "mute": False,
                "solo": False,
                "attack-threshold": round(b["threshold"], 4),
                "attack-time": round(b["attack_ms"], 4),
                "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
                "release-time": round(b["release_ms"], 4),
                "ratio": round(b["ratio"], 4),
                "knee": -6.0,
                "makeup": round(b["makeup"], 4),
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "RMS",
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 0.0,
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": lower,
                "sidechain-highcut-frequency": upper,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            })
            result[bandn] = band
        else:
            # Disabled bands
            result[bandn] = _disabled_band()

    return result


def make_regulator(regulator: dict | None, freqs: list[int],
                   volmax_boost: float = 0.0,
                   volmax_slot: str = "input-gain",
                   couple_bands: bool = False) -> dict | None:
    """Per-band limiter mapped from Dolby regulator-tuning.

    The Dolby regulator is a 20-band limiter that prevents speaker
    distortion. We approximate it using EasyEffects' multiband compressor
    configured as a limiter.

    The 20 Dolby bands are grouped into zones with similar thresholds
    to fit within EasyEffects' 8-band limit.

    Regulator parameters mapped:
      - distortion_slope: controls limiter ratio. 1.0 = hard limiter
        (infinity:1), lower values = softer limiting. Mapped as
        ratio = 1 / (1 - slope) when slope < 1, else 100:1.
      - timbre_preservation: 0-1, controls knee softness. Higher values
        mean softer knee to preserve spectral shape. Mapped to
        knee = -6 * timbre dB (0 = hard knee, 1 = -6 dB soft knee).

    `regulator-stress-amount`, `regulator-overdrive` and
    `regulator-relaxation-amount` are parsed for visibility (debug
    print + `_UNMODELED_FEATURES` watch list) but not mapped here. See
    docs/design-notes.md "Follow-ups" entry on regulator-stress for
    the empirical work that closed that hypothesis.

    couple_bands (experimental, `--enable coupled-bands`, issue #44):
    by default a zone whose threshold_high is >= 0 dBFS is treated as
    "never triggers" and disabled. A second-device DAX capture showed
    band dynamics on exactly such bands when the XML marks them
    non-isolated (`isolated_band` 0). With couple_bands on, a zero-dB
    zone whose bands are all isolated_band==0 takes its threshold at
    face value instead — a live limiter at full scale, which engages
    when upstream gain (e.g. volmax on input-gain) pushes the band past
    0 dBFS. Zones without isolated data, or containing an
    isolated_band==1 band, keep the default disabled behaviour. See
    design-notes Finding 10 / unvalidated-scaling entry 11 (f).

    volmax_boost lands on `input-gain` by default (issue #23) so the per-band
    compression tames the boosted low end before the brickwall;
    `volmax_slot="output-gain"` opts back into the older post-band-limiting
    placement. See `make_preset` for how that interacts with the chain.
    """
    if not regulator:
        return None

    th = regulator["threshold_high"]
    slope = regulator.get("distortion_slope", 1.0)
    timbre = regulator.get("timbre_preservation", 0.75)

    # Derive ratio from distortion slope:
    # slope=1.0 → hard limiter (use 100:1 as practical maximum)
    # slope=0.5 → ratio=2:1 (moderate compression)
    if slope >= 1.0:
        ratio = 100.0
    elif slope <= 0.0:
        ratio = 1.0  # bypass
    else:
        ratio = 1.0 / (1.0 - slope)

    # Derive knee from timbre preservation:
    # timbre=0 → hard knee (0 dB), timbre=1 → soft knee (-6 dB)
    knee = -6.0 * timbre

    # Group the 20 bands into zones with distinct thresholds.
    # Find runs of identical threshold_high values.
    zones = []  # list of (start_idx, end_idx, threshold)
    i = 0
    while i < len(th):
        j = i + 1
        while j < len(th) and th[j] == th[i]:
            j += 1
        zones.append((i, j - 1, th[i]))
        i = j

    # Merge zones if we have more than 8 (EasyEffects limit)
    # In practice, Dolby regulators typically produce 2-5 zones
    while len(zones) > 8:
        # Merge the two adjacent zones with the smallest threshold difference
        min_diff = float("inf")
        min_idx = 0
        for k in range(len(zones) - 1):
            diff = abs(zones[k][2] - zones[k + 1][2])
            if diff < min_diff:
                min_diff = diff
                min_idx = k
        z1 = zones[min_idx]
        z2 = zones[min_idx + 1]
        merged_thresh = max(z1[2], z2[2])  # use the less aggressive threshold
        zones[min_idx] = (z1[0], z2[1], merged_thresh)
        del zones[min_idx + 1]

    # Build the multiband compressor (used as limiter: ratio=100:1, fast attack).
    # volmax_slot picks which gain slot carries the static volmax-boost:
    # input-gain (default, issue #23) applies it pre-band-limiting, letting the
    # regulator's per-band downward compression tame the boosted low end before
    # the brickwall; output-gain opts back into post-band-limiting placement
    # (the full loudness makeup straight into the brickwall — the pre-#23
    # behaviour, kept for A/B and aggressive-regulator loudness recovery).
    # Neither placement is Dolby-documented (volmax-boost is a CP-stage leveler
    # ceiling; both slots are pragmatic approximations). Any value other than
    # "output-gain" keeps the input-gain default.
    boost = round(volmax_boost, 1)
    on_input = volmax_slot != "output-gain"
    result = {
        "bypass": False,
        "input-gain": boost if on_input else 0.0,
        "output-gain": 0.0 if on_input else boost,
        "dry": -80.01,
        "wet": 0.0,
        "compressor-mode": "Modern",
        "envelope-boost": "None",
        "stereo-split": False,
    }

    for i in range(8):
        bandn = f"band{i}"
        if i < len(zones):
            zone_start, zone_end, threshold = zones[i]
            # Crossover at the geometric mean between the last freq of this
            # zone and the first freq of the next zone
            if i > 0:
                prev_end = zones[i - 1][1]
                cross_freq = math.sqrt(freqs[prev_end] * freqs[zone_start])
            else:
                cross_freq = 10.0  # not used for band 0

            # Bands with threshold >= 0 dB never trigger; disable to save CPU
            # — unless the experimental coupled-bands mapping takes the 0 dBFS
            # threshold at face value on a fully non-isolated zone (docstring).
            is_active = threshold < 0
            if not is_active and couple_bands:
                iso = regulator.get("isolated_band")
                is_active = (iso is not None and
                             all(iso[k] == 0
                                 for k in range(zone_start, zone_end + 1)))
            band = {
                "compressor-enable": is_active,
                "mute": False,
                "solo": False,
                "attack-threshold": round(threshold, 4),
                "attack-time": 1.0,  # very fast for limiting
                "release-threshold": MBC_RELEASE_THRESHOLD_FLOOR,
                "release-time": 50.0,
                "ratio": round(ratio, 4),
                "knee": round(knee, 4),
                "makeup": 0.0,
                "compression-mode": "Downward",
                "sidechain-type": "Internal",
                "sidechain-mode": "Peak",  # peak detection for limiting
                "sidechain-source": "Middle",
                "stereo-split-source": "Left/Right",
                "sidechain-lookahead": 1.0,  # 1 ms head start for transients
                "sidechain-reactivity": 10.0,
                "sidechain-preamp": 0.0,
                "sidechain-custom-lowcut-filter": False,
                "sidechain-custom-highcut-filter": False,
                "sidechain-lowcut-frequency": 10.0,
                "sidechain-highcut-frequency": 20000.0,
                "boost-threshold": -60.0,
                "boost-amount": 0.0,
            }
            if i > 0:
                band["enable-band"] = True
                band["split-frequency"] = round(cross_freq, 1)
            result[bandn] = band
        else:
            # Disabled band
            result[bandn] = _disabled_band()

    return result


def _coupled_bands_eligible(regulator: dict | None) -> bool:
    """True when the XML carries bands the experimental coupled-bands
    mapping could activate: threshold_high >= 0 dBFS (excluded from
    limiting by default) while marked non-isolated (isolated_band == 0).
    Band-level check used for the end-of-run `--enable` hint; the actual
    activation in make_regulator is zone-level and can be stricter."""
    iso = (regulator or {}).get("isolated_band")
    if not iso:
        return False
    return any(t >= 0 and i == 0
               for t, i in zip(regulator["threshold_high"], iso))


def make_bass_enhancer(hp_freq: float, amount: float = 12.0) -> dict:
    """Psychoacoustic bass enhancement via harmonic generation.

    Small laptop speakers cannot reproduce low frequencies physically.
    The bass enhancer generates upper harmonics of the bass content,
    which the brain perceives as bass (the "missing fundamental" effect).

    Scope is set to 2x the high-pass cutoff so harmonics are generated
    only for frequencies the speaker rolls off.
    """
    scope = min(hp_freq * 2.0, 300.0)
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "amount": round(amount, 1),
        "harmonics": 10.0,
        "scope": round(scope, 1),
        "floor": 10.0,
        "blend": -10.0,
        "floor-active": True,
        "listen": False,
    }


def bass_enhancer_from_peq(peq_filters: list[dict]) -> dict:
    """The bass-enhancer stage as make_preset ships it for SoundWire,
    derived from the PEQ high-pass corner (fallback 100 Hz). Shared with
    the run report so the printed numbers cannot drift from the built
    stage.

    Whether the corner was derived or fell back is answered by
    ``bass_enhancer_scope_is_derived`` rather than a key on the returned
    stage: every key here is emitted into the preset, and the converter's
    coverage guard rightly rejects one it cannot translate.
    """
    hp = [f for f in peq_filters if f["type"] in (7, 9)]
    return make_bass_enhancer(hp[0]["f0"] if hp else 100.0)


def bass_enhancer_scope_is_derived(peq_filters: list[dict]) -> bool:
    """True when the bass-enhancer range came from the tuning's own high-pass.

    Most SoundWire tunings carry no PEQ at all — 36 of 39 distinct corpus
    files — so the printed range is twice the 100 Hz fallback, and the run
    report used to credit that constant to "this speaker's bass cutoff".
    """
    return any(f["type"] in (7, 9) for f in peq_filters)


def make_limiter(input_gain: float = 0.0) -> dict:
    """Brickwall output limiter to catch any remaining overshoot.

    Placed at the very end of the chain as a safety net. Uses the LSP
    limiter plugin with a -1 dB threshold and 1 ms lookahead for
    transparent true-peak limiting.

    input_gain is the fallback injection point for Dolby's volmax-boost
    when the regulator (multiband_compressor#1) is absent, so the
    static loudness boost still pushes peaks into the brick-wall and
    the resulting limiting acts as a crude loudness maximiser.
    """
    return {
        "bypass": False,
        "input-gain": round(input_gain, 1),
        "output-gain": 0.0,
        "mode": "Herm Thin",
        "oversampling": "None",
        "dithering": "None",
        "sidechain-type": "Internal",
        "lookahead": 1.0,
        "attack": 1.0,
        "release": 5.0,
        "threshold": -1.0,
        "gain-boost": False,
        "stereo-link": 100.0,
        "alr": False,
        "sidechain-preamp": 0.0,
    }
