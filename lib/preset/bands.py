"""The EQ bands, and the convolver slot the FIR kernel rides in.

One builder per Dolby PEQ filter type — bell, low/high shelf, high/low pass —
each funnelled through `_eq_band` so the EE-schema fillers (`mode`, `mute`,
`solo`, `width`) are written in one place and a schema change lands once.
`make_peq_eq` stacks them into the speaker-PEQ stage, matching the channels
band-for-band and paying back the boost it added — everything it knows about a
shape (its Dolby type codes, builder, filler, boost) is one `_PEQ_CATEGORIES`
row beside it. Everything else that becomes a plugin block is in `plugins.py`,
and what ships is decided in `build.py`.

Stdlib-only, and the numbers are why: a shelf Q from its S, a slope label from
a Dolby order and a per-channel gain sum are closed-form arithmetic. So this
half of preset construction is importable at the top of the generator, unlike
`plugins.py` and `build.py` behind it, which numpy keeps deferred to `main()`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import NamedTuple


def _eq_band(*, frequency, gain, q, slope, lsp_type) -> dict:
    """One EQ band in EE PEQ schema order. ``mode``/``mute``/``solo``/``width``
    are EE-schema fillers (topology, not tuning) — defined once here so a future
    EE-schema tweak lands in a single place. The per-band builders below pass
    only the values that differ (frequency/gain/q/slope/type)."""
    return {
        "frequency": frequency,
        "gain": gain,
        "mode": "RLC (BT)",
        "mute": False,
        "q": q,
        "slope": slope,
        "solo": False,
        "type": lsp_type,
        "width": 4.0,
    }


def make_band(freq: float, gain: float, q=1.5) -> dict:
    return _eq_band(
        frequency=freq, gain=round(gain, 4), q=q, slope="x1", lsp_type="Bell"
    )


# The rate the whole pipeline is built at: the FIR is designed at it
# (`fir.make_fir`), the `.irs` WAV is written at it (`emit`), the MBC time
# constants divide by it (`plugins`) and the profile report's Nyquist line
# halves it. It sits beside `make_convolver` because the convolver is the one
# plugin carrying a kernel that *has* a rate, and it lives here rather than in
# `fir.py`, where it started, because `--doctor` now compares it against the
# running PipeWire graph rate and that path must not pay numpy — which
# importing `fir` costs (`tests/test_layout.py::STDLIB_ONLY`). `fir.py`'s
# docstring predicted exactly this move.
SAMPLE_RATE = 48000


def make_convolver(kernel_name: str) -> dict:
    """Convolver plugin config referencing an IR by name.

    EasyEffects 8.x uses kernel-name (filename stem without extension),
    and looks for the WAV in its irs/ directory.

    ``autogain`` stays off because this tool owns the gain budget end to end
    (docs/design-notes.md, "Gain-staging budget"). That has a measured
    consequence off the 48 kHz path: EasyEffects resamples this kernel to the
    graph rate and compensates no gain for the longer filter, so on a graph
    above `SAMPLE_RATE` the preset runs hot by the rate ratio in dB — +11.8 dB
    measured at 192 kHz — and nothing in this block corrects it. `--doctor`
    warns instead; flipping this to True is *not* the fix, since it would also
    change the level at 48 kHz and invalidate that budget.
    """
    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": 0.0,
        "kernel-name": kernel_name,
        "ir-width": 100,
        "autogain": False,
    }


# Dolby HP/LP ``order=N`` → LSP user-facing slope ``x{N/2}`` (LSP internally
# doubles the slope so nSlope equals the filter order; see make_hp_band).
_ORDER_TO_LSP_SLOPE = {2: "x1", 4: "x2", 6: "x3", 8: "x4"}


def _make_passfilter(freq: float, order: int, lsp_type: str) -> dict:
    """Shared HP/LP pass-filter band. ``lsp_type`` selects the LSP ``type``
    label ("Hi-pass"/"Lo-pass"); the rest is identical between directions
    (see make_hp_band / make_lp_band for the slope-doubling rationale)."""
    return _eq_band(
        frequency=freq,
        gain=0.0,
        q=0.707,
        slope=_ORDER_TO_LSP_SLOPE.get(order, "x4"),
        lsp_type=lsp_type,
    )


def make_hp_band(freq: float, order: int) -> dict:
    """High-pass filter band for speaker protection.

    Dolby's ``order=N`` declares an N-th-order high-pass. LSP's
    ``RLC (BT)`` HP user-facing slope ``x1..x4`` is *internally doubled*
    to ``nSlope=2,4,6,8`` (that's literally ``*slope = 2 * *slope`` in
    ``para_equalizer.cpp:167``), and ``calc_rlc_filter`` then builds
    ``nSlope/2`` cascaded 2nd-order sections at the user-Q — so internal
    ``nSlope`` equals filter order. So Dolby ``order=N`` maps to LSP
    user-facing slope ``x{N/2}`` (see ``_ORDER_TO_LSP_SLOPE``). Corpus has
    order ∈ {2, 4, 8}.
    """
    return _make_passfilter(freq, order, "Hi-pass")


def _shelf_q_from_s(gain: float, s: float) -> float:
    """Standard audio S-to-Q conversion for shelving filters.

    Q = 1/sqrt((A + 1/A) * (1/S - 1) + 2) where A = 10^(gain/40).
    For S=1.0 this simplifies to Q ≈ 0.707 (Butterworth). The
    (A + 1/A) term is symmetric in A↔1/A, so the sign of gain does
    not affect Q — and the formula is also symmetric between
    low-shelf and high-shelf variants.
    """
    a = 10 ** (gain / 40.0) if gain != 0 else 1.0
    denom = (a + 1.0 / a) * (1.0 / s - 1.0) + 2.0
    return 1.0 / math.sqrt(max(denom, 0.01))


def _make_shelf(freq: float, gain: float, s: float, lsp_type: str) -> dict:
    """Shared low/high-shelf band. ``lsp_type`` selects the LSP ``type``
    label ("Lo-shelf"/"Hi-shelf"); the Q-from-S derivation is identical in
    both directions (``_shelf_q_from_s`` is symmetric in shelf direction)."""
    return _eq_band(
        frequency=freq,
        gain=round(gain, 4),
        q=round(_shelf_q_from_s(gain, s), 4),
        slope="x1",
        lsp_type=lsp_type,
    )


def make_shelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """Low-shelf filter band from Dolby PEQ type 4."""
    return _make_shelf(freq, gain, s, "Lo-shelf")


def make_hishelf_band(freq: float, gain: float, s: float = 1.0) -> dict:
    """High-shelf filter band from Dolby PEQ type 3.

    Mirror of make_shelf_band with LSP's "Hi-shelf" mode. Same Q-from-S
    derivation — the formula is symmetric in shelf direction. Corpus
    gains are strictly non-negative (0 to +15 dB) across the 1754
    type-3 filters observed, typically a +2-5 dB presence lift around
    2.7 kHz. Experimental path — not yet audibly validated.
    """
    return _make_shelf(freq, gain, s, "Hi-shelf")


def make_lp_band(freq: float, order: int) -> dict:
    """Low-pass filter band from Dolby PEQ types 6 and 8.

    Mirror of make_hp_band with LSP's "Lo-pass" mode — same LSP slope
    doubling convention (see make_hp_band docstring), so order N maps
    to slope ``x{N/2}`` via ``_ORDER_TO_LSP_SLOPE``. Rare: a few hundred LP
    filters across the corpus, mostly order=8 tweeter-guard rolloff.
    Experimental path — not yet audibly validated.
    """
    return _make_passfilter(freq, order, "Lo-pass")


class _PeqCategory(NamedTuple):
    """One Dolby PEQ filter shape, and everything `make_peq_eq` does with it.

    A row is the whole of it: bucketing, band building, the filler and the
    boost sum all read from here, so a new shape — or a re-read Dolby type
    code — is one row's edit, not the same knowledge restated in four places
    that a partial change leaves disagreeing.
    """
    types: tuple[int, ...]          # Dolby PEQ `type` codes selecting this
                                    # shape. Must be disjoint across rows —
                                    # each row buckets independently, so a
                                    # code in two rows emits its filter twice.
    build: Callable[[dict], dict]   # parsed filter → EE band
    filler: Callable[[], dict]      # band for a slot the shorter channel has
                                    # no filter for (see the fill pass)
    boost: Callable[[dict], float] | None
                                    # broadband dB a positive-gain filter of
                                    # this shape adds, for the output-gain
                                    # compensation below; None = cut-only, so
                                    # it contributes nothing


# The shapes `make_peq_eq` emits, in the order their bands are laid out —
# row order *is* band order, and both channels walk it from the same offsets,
# which is what keeps L and R matched band-for-band. Fillers keep the slot's
# shape rather than being transparent: shelf and bell fill at 0 dB (a genuine
# no-op), while HP/LP have no neutral setting and fill at an out-of-the-way
# corner instead.
_PEQ_CATEGORIES = (
    _PeqCategory(   # high pass
        types=(7, 9),
        build=lambda pf: make_hp_band(pf["f0"], pf["order"]),
        filler=lambda: make_hp_band(100.0, 4),
        boost=None,
    ),
    _PeqCategory(   # low pass
        types=(6, 8),
        build=lambda pf: make_lp_band(pf["f0"], pf["order"]),
        filler=lambda: make_lp_band(20000.0, 4),
        boost=None,
    ),
    _PeqCategory(   # low shelf
        types=(4,),
        build=lambda pf: make_shelf_band(pf["f0"], pf["gain"], pf["s"]),
        filler=lambda: make_shelf_band(100.0, 0.0),
        boost=lambda pf: pf["gain"],
    ),
    _PeqCategory(   # high shelf
        types=(3,),
        build=lambda pf: make_hishelf_band(pf["f0"], pf["gain"], pf["s"]),
        filler=lambda: make_hishelf_band(10000.0, 0.0),
        boost=lambda pf: pf["gain"],
    ),
    _PeqCategory(   # bell
        types=(1,),
        build=lambda pf: make_band(pf["f0"], pf["gain"], q=pf["q"]),
        filler=lambda: make_band(1000.0, 0.0),
        boost=lambda pf: pf["gain"] * min(1.0, 2.0 / pf.get("q", 1.0)),
    ),
)


def make_peq_eq(peq_filters: list[dict]) -> dict | None:
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Handles the filter types in `_PEQ_CATEGORIES`: 1 (bell), 4 (low-shelf),
    7/9 (high-pass), 3 (high-shelf, experimental), 6/8 (low-pass,
    experimental); any other type is dropped. The HP protects laptop
    speakers from sub-bass energy they can't reproduce; the LP is a
    tweeter-guard rolloff seen on a handful of ALC274 SKUs.
    """
    def bucket(cat: _PeqCategory, speaker: int) -> list[dict]:
        return [f for f in peq_filters
                if f["speaker"] == speaker and f["type"] in cat.types]

    buckets = [(bucket(cat, 0), bucket(cat, 1)) for cat in _PEQ_CATEGORIES]
    counts = [max(len(b_l), len(b_r)) for b_l, b_r in buckets]
    num_bands = sum(counts)

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    off = 0
    for (bucket_l, bucket_r), cat, count in zip(buckets, _PEQ_CATEGORIES,
                                                counts):
        for j, pf in enumerate(bucket_l):
            left_bands[f"band{off + j}"] = cat.build(pf)
        for j, pf in enumerate(bucket_r):
            right_bands[f"band{off + j}"] = cat.build(pf)
        off += count

    # Fill missing bands on whichever channel is shorter. Each slot keeps
    # its filter category so the channels stay topologically matched.
    slot_category = [cat for cat, count in zip(_PEQ_CATEGORIES, counts)
                     for _ in range(count)]
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            left_bands[key] = slot_category[idx].filler()
        if key not in right_bands:
            right_bands[key] = slot_category[idx].filler()

    # Compensate for PEQ boost to prevent clipping. Bells are scaled by
    # bandwidth: a narrow Q=4.6 bell at +4 dB barely raises broadband
    # level, while a wide Q=0.7 bell at +4 dB raises it nearly 4 dB
    # (effective boost ≈ gain * min(1, 2/Q)). Shelves (both low- and
    # high-shelf) contribute their full gain because they raise an entire
    # half-band above/below the corner. HP/LP filters are cut-only and
    # reduce headroom, so they don't enter the compensation sum — that is
    # the `boost=None` rows above.
    effective_boosts = []
    for (bucket_l, bucket_r), cat in zip(buckets, _PEQ_CATEGORIES):
        if cat.boost is None:
            continue
        for pf in bucket_l + bucket_r:
            if pf["gain"] <= 0:
                continue
            effective_boosts.append(cat.boost(pf))
    peak_boost = max(effective_boosts, default=0.0)
    output_gain = -peak_boost

    return {
        "bypass": False,
        "input-gain": 0.0,
        "output-gain": round(output_gain, 2),
        "mode": "IIR",
        "num-bands": num_bands,
        "split-channels": True,
        "left": left_bands,
        "right": right_bands,
    }
