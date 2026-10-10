"""The format-neutral shape of a vendor APO layer.

A layer is what one vendor APO config contributes on top of the Dolby chain.
It is a linear EQ, folded into the convolver's FIR, plus zero or more
multiband dynamics stages placed after Dolby's own. Each format's parser
(`lib/apo/surface.py`) fills one in. `lib/preset/` consumes it, and never
looks at the vendor file itself.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), because discovery runs
before the generator's deferred DSP imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The one `--enable`/`--disable` name for every vendor format. A device binds
# at most one layer (`lib/apo/discover.py`), so the run's report, which names
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


@dataclass(frozen=True)
class ApoLayer:
    """What one vendor APO config adds to the preset.

    `default_on` decides whether `FLAG` reaches it through `--enable` or
    `--disable` (`is_active`). It is per layer, because each format earns
    its default separately. `notes` lists what the layer does not
    reproduce, each in a listener's words with the vendor block's name in
    parentheses for triage. The run prints them, so a listener comparing
    with Windows knows what is missing. `skipped` lists the whole stages
    the layer could not build, as `(stage, reason)`. The run prints each
    beside the stages it did build, so a missing stage doesn't read like a
    detail.
    """
    label: str
    config_path: Path
    inf_path: Path
    hardware_id: str
    sample_rate: int
    eq_left: tuple[Biquad, ...]
    eq_right: tuple[Biquad, ...]
    dynamics: tuple[BandDynamics, ...]
    notes: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...] = ()
    default_on: bool = False


class UnsupportedApoConfig(Exception):
    """A config bound to this device exists but cannot be translated.

    Carries the path and the reason. The run reports both rather than
    silently building the Dolby-only preset.
    """

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def is_active(layer: ApoLayer | None, enabled: set[str],
              disabled: set[str]) -> bool:
    """True when *layer* exists and the run's flags switch it on."""
    if layer is None:
        return False
    if layer.default_on:
        return FLAG not in disabled
    return FLAG in enabled
