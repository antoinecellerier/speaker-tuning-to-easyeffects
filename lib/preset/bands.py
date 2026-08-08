"""The EQ bands, and the convolver slot the FIR kernel rides in.

One builder per Dolby PEQ filter type — bell, low/high shelf, high/low pass —
each funnelled through `_eq_band` so the EE-schema fillers (`mode`, `mute`,
`solo`, `width`) are written in one place and a schema change lands once.
`make_peq_eq` stacks them into the speaker-PEQ stage, matching the channels
band-for-band and paying back the boost it added; everything else that becomes
a plugin block is in `plugins.py`, and what ships is decided in `build.py`.

Stdlib-only, and the numbers are why: a shelf Q from its S, a slope label from
a Dolby order and a per-channel gain sum are closed-form arithmetic. So this
half of preset construction is importable at the top of the generator, unlike
`plugins.py` and `build.py` behind it, which numpy keeps deferred to `main()`.
"""

from __future__ import annotations

import math


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


def make_convolver(kernel_name: str) -> dict:
    """Convolver plugin config referencing an IR by name.

    EasyEffects 8.x uses kernel-name (filename stem without extension),
    and looks for the WAV in its irs/ directory.
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


def make_peq_eq(peq_filters: list[dict]) -> dict | None:
    """Parametric EQ for the explicit speaker PEQ from Dolby.

    Handles filter types: 1 (bell), 4 (low-shelf), 7/9 (high-pass),
    3 (high-shelf, experimental), 6/8 (low-pass, experimental). The HP
    protects laptop speakers from sub-bass energy they can't reproduce;
    the LP is a tweeter-guard rolloff seen on a handful of ALC274 SKUs.
    """
    bells_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 1]
    bells_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 1]
    hp_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (7, 9)]
    hp_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (7, 9)]
    lp_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] in (6, 8)]
    lp_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] in (6, 8)]
    loshelf_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 4]
    loshelf_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 4]
    hishelf_l = [f for f in peq_filters if f["speaker"] == 0 and f["type"] == 3]
    hishelf_r = [f for f in peq_filters if f["speaker"] == 1 and f["type"] == 3]

    num_bells = max(len(bells_l), len(bells_r))
    num_hp = max(len(hp_l), len(hp_r))
    num_lp = max(len(lp_l), len(lp_r))
    num_loshelf = max(len(loshelf_l), len(loshelf_r))
    num_hishelf = max(len(hishelf_l), len(hishelf_r))
    num_bands = num_hp + num_lp + num_loshelf + num_hishelf + num_bells

    if num_bands == 0:
        return None

    left_bands = {}
    right_bands = {}

    def place(bucket_l, bucket_r, builder, off):
        for j, pf in enumerate(bucket_l):
            left_bands[f"band{off + j}"] = builder(pf)
        for j, pf in enumerate(bucket_r):
            right_bands[f"band{off + j}"] = builder(pf)

    off = 0
    place(hp_l, hp_r, lambda pf: make_hp_band(pf["f0"], pf["order"]), off)
    off += num_hp
    place(lp_l, lp_r, lambda pf: make_lp_band(pf["f0"], pf["order"]), off)
    off += num_lp
    place(loshelf_l, loshelf_r,
          lambda pf: make_shelf_band(pf["f0"], pf["gain"], pf["s"]), off)
    off += num_loshelf
    place(hishelf_l, hishelf_r,
          lambda pf: make_hishelf_band(pf["f0"], pf["gain"], pf["s"]), off)
    off += num_hishelf
    place(bells_l, bells_r,
          lambda pf: make_band(pf["f0"], pf["gain"], q=pf["q"]), off)

    # Fill missing bands on whichever channel is shorter. Each slot keeps
    # its filter category so the channels stay topologically matched.
    fillers = []
    for _ in range(num_hp):
        fillers.append(lambda: make_hp_band(100.0, 4))
    for _ in range(num_lp):
        fillers.append(lambda: make_lp_band(20000.0, 4))
    for _ in range(num_loshelf):
        fillers.append(lambda: make_shelf_band(100.0, 0.0))
    for _ in range(num_hishelf):
        fillers.append(lambda: make_hishelf_band(10000.0, 0.0))
    for _ in range(num_bells):
        fillers.append(lambda: make_band(1000.0, 0.0))
    for idx in range(num_bands):
        key = f"band{idx}"
        if key not in left_bands:
            left_bands[key] = fillers[idx]()
        if key not in right_bands:
            right_bands[key] = fillers[idx]()

    # Compensate for PEQ boost to prevent clipping. Bells are scaled by
    # bandwidth: a narrow Q=4.6 bell at +4 dB barely raises broadband
    # level, while a wide Q=0.7 bell at +4 dB raises it nearly 4 dB
    # (effective boost ≈ gain * min(1, 2/Q)). Shelves (both low- and
    # high-shelf) contribute their full gain because they raise an entire
    # half-band above/below the corner. HP/LP filters are cut-only and
    # reduce headroom, so they don't enter the compensation sum.
    effective_boosts = []
    for pf in bells_l + bells_r:
        if pf["gain"] <= 0:
            continue
        q = pf.get("q", 1.0)
        effective_boosts.append(pf["gain"] * min(1.0, 2.0 / q))
    for pf in loshelf_l + loshelf_r + hishelf_l + hishelf_r:
        if pf["gain"] <= 0:
            continue
        effective_boosts.append(pf["gain"])
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
