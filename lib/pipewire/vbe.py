"""The opt-in virtual-bass wet branch for the PipeWire conf (issue #14).

DAX runs a psychoacoustic Virtual Bass Enhancement stage on HDA internal
speakers that the 1:1 EasyEffects translation cannot express: a parallel
band-passed saturator summed back into the untouched dry chain. EasyEffects'
pipeline is strictly serial, so the generator only records the XML's
`virtual-bass-*` values in the preset's top-level `_vbe` block
(`lib/preset/build.py`), and this module turns that block into filter-graph
nodes and links around the translated chain.

Topology (measured against the DAX capture — docs/design-notes.md Finding 8):

    input copies -+-> translated dry chain -----------------> final mix In 1
                  +-> arm 1: HP@35  -> LP@57.4 -> saturator -\
                  |                              (premix, -2/-9 dB)
                  +-> arm 2: HP@57.4 -> LP@94  -> saturator -/
                       premix -> HP@94 -> HP@94 -> LP@469 --> final mix In 2

Sub-band edges partition [src-freqs[0], mix-freqs[0]] geometrically over the
arms whose subgain is above the schema's -192 "off" floor; the mix band is
`mix-freqs` verbatim; the double HP at the mix-band edge is what keeps arm
fundamentals out of the sum (guard G1 in the Finding 8 evidence). Everything
is IIR with no look-ahead, so the branch adds zero latency.

`wrap_chain` sandwiches the translated stages: a copy fan-out becomes the
graph input and the dry+wet mixer becomes the graph output, so
`conf.emit_links` and `conf.format_conf` need no changes — they only ever
look at the first and last stage's port references.
"""

from __future__ import annotations

from lib.pipewire.plugins import Stage, db_to_lin

LSP_FILTER_URI = "http://lsp-plug.in/plugins/lv2/filter_stereo"
CALF_SATURATOR_URI = "http://calf.sourceforge.net/plugins/Saturator"

# Saturator shape: measurement-calibrated global engine constants, fit once
# against the DAX bass-burst capture and identical for every device (same
# debt class as the SoundWire Calf BassEnhancer constants — design-notes
# empirical-shortcut list). Each won its sweep by more than the 1.5 dB
# margin; the XML has no field for either.
VBE_SAT_DRIVE = 4.0
VBE_SAT_BLEND = -10.0

# LSP filter_stereo enum values (lv2info-verified, lsp-plugins-lv2 1.2.33;
# same tables as tools/measure_ee/render_vbe_chain.py): brick-wall
# Butterworth-Chebyshev bilinear at the steepest x16 slope, IIR engine.
_FT_HIPASS = 1
_FT_LOPASS = 0
_FM_BWC_BT = 2
_SLOPE_X16 = 7
_MODE_IIR = 0

# Two cascaded HPs at the mix-band low edge: one x16 pass leaks enough arm
# fundamental into the sum to fail the wet-leakage guard (Finding 8, G1).
_MIX_HP_STAGES = 2


def _filter_node(name: str, ft: int, freq: float) -> dict:
    return {
        "type": "lv2",
        "name": name,
        "plugin": LSP_FILTER_URI,
        "control": {
            "enabled": 1,
            "mode": _MODE_IIR,
            "ft": ft,
            "fm": _FM_BWC_BT,
            "s": _SLOPE_X16,
            "f": round(freq, 4),
        },
    }


def _saturator_node(name: str) -> dict:
    # Unity levels and full wet: band selection and the wet level live in
    # the surrounding filters and mixers, so the saturator contributes shape
    # only. Internal pre/post tone controls stay off for the same reason.
    return {
        "type": "lv2",
        "name": name,
        "plugin": CALF_SATURATOR_URI,
        "control": {
            "bypass": 0,
            "level_in": 1.0,
            "level_out": 1.0,
            "mix": 1.0,
            "drive": VBE_SAT_DRIVE,
            "blend": VBE_SAT_BLEND,
            "pre": 0,
            "post": 0,
        },
    }


def _mixer_node(name: str, gains: list[float]) -> dict:
    return {
        "type": "builtin",
        "name": name,
        "label": "mixer",
        "control": {f"Gain {i + 1}": round(g, 6)
                    for i, g in enumerate(gains)},
    }


def _sub_band_edges(src_lo: float, mix_lo: float, arms: int) -> list[float]:
    """Geometric partition of [src_lo, mix_lo] into `arms` sub-bands.

    For the corpus values (35, 94, two live arms) this yields the measured
    v3 edges 35 / 57.3585 / 94. The split point is an assumption, not an
    XML field — Finding 8 measured it as non-load-bearing (an alternative
    70 Hz edge scored within 0.03 dB).
    """
    ratio = (mix_lo / src_lo) ** (1.0 / arms)
    return [src_lo * ratio ** i for i in range(arms + 1)]


def wrap_chain(stages: list[Stage],
               vbe: dict) -> tuple[list[Stage], list[dict]]:
    """Sandwich the translated chain between a fan-out and a dry+wet mix.

    Returns (new_stages, wet_links). The caller concatenates `wet_links`
    after `conf.emit_links(new_stages)` — the serial linker wires the copy
    fan-out into the dry chain and the dry chain into the final mixer's
    `In 1` on its own, because those nodes are the new first/last stages.
    """
    taken = {n["name"] for s in stages for n in s.nodes}
    clash = sorted(n for n in taken if n.startswith("vbe_"))
    if clash:
        raise ValueError(
            f"chain already contains vbe_-prefixed nodes: {', '.join(clash)}")

    arm_gains_db = list(vbe["arm_gains_db"])
    edges = _sub_band_edges(vbe["src_lo_hz"], vbe["mix_lo_hz"],
                            len(arm_gains_db))

    head = Stage(
        nodes=[
            {"type": "builtin", "name": "vbe_in_l", "label": "copy"},
            {"type": "builtin", "name": "vbe_in_r", "label": "copy"},
        ],
        in_l=("vbe_in_l", "In"), in_r=("vbe_in_r", "In"),
        out_l=("vbe_in_l", "Out"), out_r=("vbe_in_r", "Out"),
    )

    nodes: list[dict] = []
    links: list[dict] = []

    def link(out_ref: str, in_ref: str) -> None:
        links.append({"output": out_ref, "input": in_ref})

    for i in range(len(arm_gains_db)):
        hp = f"vbe_a{i}f1"
        lp = f"vbe_a{i}f2"
        sat = f"vbe_a{i}sat"
        nodes.append(_filter_node(hp, _FT_HIPASS, edges[i]))
        nodes.append(_filter_node(lp, _FT_LOPASS, edges[i + 1]))
        nodes.append(_saturator_node(sat))
        link("vbe_in_l:Out", f"{hp}:in_l")
        link("vbe_in_r:Out", f"{hp}:in_r")
        for ch in ("l", "r"):
            link(f"{hp}:out_{ch}", f"{lp}:in_{ch}")
            link(f"{lp}:out_{ch}", f"{sat}:in_{ch}")
        link(f"{sat}:out_l", f"vbe_premix_l:In {i + 1}")
        link(f"{sat}:out_r", f"vbe_premix_r:In {i + 1}")

    arm_gains_lin = [db_to_lin(g) for g in arm_gains_db]
    nodes.append(_mixer_node("vbe_premix_l", arm_gains_lin))
    nodes.append(_mixer_node("vbe_premix_r", arm_gains_lin))

    posts = [f"vbe_post{i + 1}" for i in range(_MIX_HP_STAGES + 1)]
    for name in posts[:-1]:
        nodes.append(_filter_node(name, _FT_HIPASS, vbe["mix_lo_hz"]))
    nodes.append(_filter_node(posts[-1], _FT_LOPASS, vbe["mix_hi_hz"]))
    for ch in ("l", "r"):
        link(f"vbe_premix_{ch}:Out", f"{posts[0]}:in_{ch}")
        for prev, nxt in zip(posts, posts[1:]):
            link(f"{prev}:out_{ch}", f"{nxt}:in_{ch}")

    # Final sum: In 1 = the translated chain (unity), In 2 = the wet branch
    # at the XML's overall gain (0 dB -> 1.0 on every corpus file).
    mix_gains = [1.0, db_to_lin(vbe["overall_gain_db"])]
    nodes.append(_mixer_node("vbe_mix_l", mix_gains))
    nodes.append(_mixer_node("vbe_mix_r", mix_gains))
    link(f"{posts[-1]}:out_l", "vbe_mix_l:In 2")
    link(f"{posts[-1]}:out_r", "vbe_mix_r:In 2")

    tail = Stage(
        nodes=nodes,
        in_l=("vbe_mix_l", "In 1"), in_r=("vbe_mix_r", "In 1"),
        out_l=("vbe_mix_l", "Out"), out_r=("vbe_mix_r", "Out"),
    )
    return [head, *stages, tail], links
