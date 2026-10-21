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

from collections.abc import Callable
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

    `curve_hz` with `curve_left_db`/`curve_right_db` is an EQ the vendor
    ships as a magnitude curve rather than as filters. It is interpolated in
    log frequency, flat beyond its ends, and folded into the FIR with the
    biquads (`lib/preset/fir.py` `design_target_db`). Empty when the format
    has none.

    `standalone` marks a layer that is the device's whole tuning: no Dolby
    file ships beside it, so nothing is stacked under it.
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
    standalone: bool = False
    curve_hz: tuple[float, ...] = ()
    curve_left_db: tuple[float, ...] = ()
    curve_right_db: tuple[float, ...] = ()

    def curve(self, side: str):
        """``(hz, db)`` for *side* ("left" or "right"), or None."""
        if not self.curve_hz:
            return None
        return self.curve_hz, (self.curve_left_db if side == "left"
                               else self.curve_right_db)


@dataclass(frozen=True)
class StandaloneFormat:
    """A vendor format that is a device's whole tuning, in place of the XML.

    One per format, in `lib/apo/discover.py` `STANDALONE`. A device that
    ships one of these ships no Dolby file, so the run builds each profile
    from the layer `resolve` returns for it alone (`lib/tuning_source.py`).
    """
    # Whether a file or folder is one this format can stand in for the XML.
    is_source: Callable[[Path], bool]
    # The members of a driver download's file list this format reads, which
    # `lib/driver_package.py` keeps as it keeps a DAX3 XML.
    wanted_members: Callable[[list[str]], set[str]]
    # (source, --hw-id or None, warn) → profile name → the layer bound to
    # this machine, or to the IDs given, the profile a run without
    # --profile builds first. Raises RuntimeError, saying why, when none is.
    resolve: Callable[[Path, str | None, Callable[[str], None]],
                      dict[str, ApoLayer]]
    # The preset-name stem when --prefix is not given.
    prefix: str
    # Whose tuning it is, as the refusals of the Dolby-only flags name it.
    owner: str
    # The run's heading line, in place of the Dolby endpoint's.
    heading: str
    # Where the correction check probes a build from it.
    probe_freqs: Callable[[], list[int]]
    # The profiles no listener has heard yet: the run marks their presets
    # experimental wherever it lists them.
    experimental: tuple[str, ...] = ()


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
