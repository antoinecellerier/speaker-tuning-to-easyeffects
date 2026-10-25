"""The format-neutral shape of a vendor APO layer.

A layer is what one vendor APO config contributes on top of the Dolby chain,
or, for a vendor-only device, instead of it. It is a linear EQ, folded into
the convolver's FIR, plus zero or more multiband dynamics stages placed after
Dolby's own. The EQ is a biquad cascade, a dB curve, or both. Each format's
parser (`lib/apo/surface.py`, `lib/apo/sonitude.py`) fills one in.
`lib/preset/` consumes it, and never looks at the vendor file itself.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), because discovery runs
before the generator's deferred DSP imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The one `--enable`/`--disable` name for every vendor format. A device binds
# at most one layer (`lib/apo/stacked.py`), so the run's report, which names
# the format, says what the flag switched.
FLAG = "vendor-apo"


# One biquad section as (b0, b1, b2, a1, a2), normalised so a0 == 1 and the
# denominator reads 1 + a1·z⁻¹ + a2·z⁻².
Biquad = tuple[float, float, float, float, float]


@dataclass(frozen=True)
class DynBand:
    """One band of a multiband dynamics stage, in LSP compressor units.

    `enabled` False keeps the band's split but compresses nothing. The
    sidechain range, when set, narrows what the band's detector listens to.
    That is how a resonance limiter whose bands overlap is approximated
    with contiguous splits.
    """
    enabled: bool
    threshold_db: float
    ratio: float
    attack_ms: float
    release_ms: float
    pregain_db: float = 0.0
    sidechain_hz: tuple[float, float] | None = None


@dataclass(frozen=True)
class BandDynamics:
    """A multiband dynamics stage: N bands split at N − 1 crossovers."""
    name: str
    crossovers_hz: tuple[float, ...]
    bands: tuple[DynBand, ...]
    detection: str  # LSP sidechain-mode label: "RMS" or "Peak"
    knee_db: float


# What a run says of each vendor stage. Approximated is built, but knowingly
# not as the file states it. None of them says a mapping is confirmed:
# `docs/reference.md` "Validated vs unvalidated mappings" holds that.
BUILT = "built"
APPROXIMATED = "approximated"
NOT_REPRODUCED = "not reproduced"
STATUSES = (BUILT, APPROXIMATED, NOT_REPRODUCED)


@dataclass(frozen=True)
class Stage:
    """One stage of a vendor chain, as the run reports it.

    `name` is in a listener's words, with the vendor block's name in
    parentheses for triage where the format names its blocks. `summary` is
    a short clause or two: for a built stage, what it holds or where the
    build differs from the file; for one not reproduced, why. `builds` ties
    a built stage to what the report describes it from: "eq" for the
    layer's EQ, which it describes as a tone, or the `BandDynamics.name`
    built from it. That description needs the DSP imports this module runs
    before. `evidence` is the research unit's `r-` tag the stage's reading
    rests on, if one does.
    """
    name: str
    status: str  # BUILT, APPROXIMATED or NOT_REPRODUCED
    summary: str = ""
    builds: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class ApoLayer:
    """What one vendor APO config adds to the preset.

    `default_on` decides whether `FLAG` reaches it through `--enable` or
    `--disable` (`is_active`). It is per layer, because each format earns
    its default separately. `stages` lists every stage of the vendor chain
    the run reports, built or not (`Stage`). The run prints one line each,
    so a listener comparing with Windows knows what is missing, and a stage
    left out reads as a stage rather than as a detail of one.

    `curve_hz` with `curve_left_db`/`curve_right_db` is an EQ the vendor
    ships as a magnitude curve rather than as filters. It is interpolated in
    log frequency, flat beyond its ends, and folded into the FIR with the
    biquads (`lib/preset/fir.py` `design_target_db`). Empty when the format
    has none.

    `whole_tuning` marks a layer that is the device's whole tuning: no
    Dolby file ships beside it, so nothing is stacked under it, and each
    profile has a layer of its own.

    `changes_nothing` marks a whole-tuning layer whose build would leave the
    sound as it is, such as a placeholder preset's flat curve. A run builds
    no preset from it (`lib/tuning_source.py`).
    """
    label: str
    config_path: Path
    inf_path: Path
    hardware_id: str
    sample_rate: int
    eq_left: tuple[Biquad, ...]
    eq_right: tuple[Biquad, ...]
    dynamics: tuple[BandDynamics, ...]
    stages: tuple[Stage, ...]
    default_on: bool = False
    whole_tuning: bool = False
    curve_hz: tuple[float, ...] = ()
    curve_left_db: tuple[float, ...] = ()
    curve_right_db: tuple[float, ...] = ()
    changes_nothing: bool = False

    def curve(self, side: str):
        """``(hz, db)`` for *side* ("left" or "right"), or None."""
        if not self.curve_hz:
            return None
        return self.curve_hz, (self.curve_left_db if side == "left"
                               else self.curve_right_db)


class UnsupportedApoConfig(Exception):
    """A config bound to this device exists but cannot be translated.

    Carries the path and the reason. The run reports both rather than
    silently building the Dolby-only preset.
    """

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


class BoundButUnreadable(RuntimeError):
    """A tuning binds this device but can't be read. The run says so over
    any other kind's "none for this device" (`lib/tuning_lookup.py`), since
    it names the tuning that is this device's."""


def is_active(layer: ApoLayer | None, enabled: set[str],
              disabled: set[str]) -> bool:
    """True when *layer* exists and the run's flags switch it on."""
    if layer is None:
        return False
    if layer.default_on:
        return FLAG not in disabled
    return FLAG in enabled
