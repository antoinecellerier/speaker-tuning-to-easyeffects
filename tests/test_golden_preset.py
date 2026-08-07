"""Golden snapshot of the emitted preset parameters.

The rest of the suite asserts *properties* — a gain lands in range, a
plugin is present, a shipped bug stays fixed. Nothing asserts that the
numbers themselves are the ones we emitted last time, so a refactor can
move a coefficient in a plugin no trap covers and every test still
passes. That is not hypothetical: the out-of-tree harness that used to
cover this rotted against a `make_preset` signature change and went a
month unnoticed, because nothing in `tests/` ran it.

This pins the values. Inputs are synthetic (the `tests/conftest.py`
builders — never real Dolby tuning), so it needs no corpus and runs for
every contributor on every `pytest tests/`.

**A digest change is not automatically a failure.** It means the output
moved; you decide whether you meant it. If you did:

    ATMOS_UPDATE_GOLDEN=1 python3 -m pytest tests/test_golden_preset.py

and commit the baseline diff alongside the change that caused it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from lib.preset.build import make_preset
from lib.preset.emit import save_wav_stereo
from lib.dax.parse import DB_FIXED_POINT_SCALE, parse_xml
from lib.preset.fir import make_fir
from tests.conftest import (
    SYNTHETIC_FREQS_20,
    is_minimum_phase,
    read_irs_file,
    synthetic_mb_comp,
    synthetic_peq_filters,
    synthetic_regulator,
    write_synthetic_tuning_xml,
)

BASELINE_PATH = Path(__file__).parent / "golden_preset_baseline.json"
_UPDATE = bool(os.environ.get("ATMOS_UPDATE_GOLDEN"))


def _canonical(obj):
    """Blank version-volatile provenance before hashing.

    The preset embeds ``"_generator": "dolby_to_easyeffects.py <version>"``
    where the version is git-describe based — it flips clean→dirty the
    moment the working tree is edited, so hashing it verbatim would report
    every preset as changed after any source edit at all. It is not
    XML-derived, so it sits outside what this snapshot guards.
    """
    if isinstance(obj, dict):
        return {k: ("<generator>" if k == "_generator" else _canonical(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    return obj


def _digest(preset: dict) -> str:
    """Short, portable digest of a preset.

    Hashes the JSON only — every value in it is a plain Python number or
    string derived arithmetically from the inputs, so the digest is stable
    across machines. FIR samples deliberately stay out: they come from a
    numpy FFT whose last bits can move with the BLAS/numpy build, which
    would make this fail for reasons that have nothing to do with the
    converter.
    """
    blob = json.dumps(_canonical(preset), indent=4, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --- synthetic inputs, one builder per feature dimension ---

# (speaker, type, f0, gain, q, order, s) — one of each shape make_peq_eq
# branches on: 1 bell, 3 high-shelf, 4 low-shelf, 6/8 low-pass, 7/9 high-pass.
_PEQ_BELLS = synthetic_peq_filters(
    [(ch, 1, 1000.0, 4.0, 1.5, 0, 1.0) for ch in (0, 1)])
_PEQ_MIXED = synthetic_peq_filters([
    (0, 1, 900.0, 3.0, 1.2, 0, 1.0), (1, 1, 900.0, 3.0, 1.2, 0, 1.0),
    (0, 3, 8000.0, 2.5, 0.7, 0, 1.0), (1, 3, 8000.0, 2.5, 0.7, 0, 1.0),
    (0, 4, 120.0, -3.0, 0.7, 0, 1.0), (1, 4, 120.0, -3.0, 0.7, 0, 1.0),
    (0, 7, 80.0, 0.0, 0.7, 2, 1.0), (1, 7, 80.0, 0.0, 0.7, 2, 1.0),
    (0, 6, 16000.0, 0.0, 0.7, 2, 1.0), (1, 6, 16000.0, 0.0, 0.7, 2, 1.0),
])
_PEQ_ASYMMETRIC = synthetic_peq_filters([
    (0, 1, 700.0, 6.0, 1.0, 0, 1.0),
    (1, 1, 700.0, 2.0, 1.0, 0, 1.0),
    (0, 9, 60.0, 0.0, 0.7, 4, 1.0),
])

_MBC_2 = synthetic_mb_comp(2, [(10, -160, 16384, 30000, 32500, 0),
                               (20, -160, 16384, 30000, 32500, 0)])
_MBC_4 = synthetic_mb_comp(4, [(4, -200, 16000, 29000, 32000, 64),
                               (9, -180, 16400, 30000, 32400, 32),
                               (14, -160, 16800, 31000, 32600, 16),
                               (19, -140, 17200, 31500, 32700, 0)])

_REG = synthetic_regulator([-6.0] * 20)
# Bands 0-3 at full scale but marked non-isolated: the shape --enable
# coupled-bands acts on (issue #44).
_REG_COUPLED = synthetic_regulator(
    [0.0] * 4 + [-8.0] * 16,
    isolated_band=[0] * 4 + [1] * 16,
)

_LEVELER = {"enable": True, "amount": 5, "out_target": -16.0}
_DIALOG = {"enable": True, "amount": 5, "boost": 4.0}


def _base(**overrides) -> dict:
    """make_preset kwargs for the fully-populated device, before overrides."""
    kwargs = dict(
        kernel_name="Golden",
        peq_filters=_PEQ_BELLS,
        vol_leveler=_LEVELER,
        dialog_enhancer=_DIALOG,
        mb_comp=_MBC_2,
        regulator=_REG,
        freqs=SYNTHETIC_FREQS_20,
        is_soundwire=False,
        volmax_boost=0.0,
        volmax_slot="input-gain",
        fir_peak_db=0.0,
        enabled=set(),
        disabled=set(),
    )
    kwargs.update(overrides)
    return kwargs


# Each scenario isolates one dimension of make_preset's behaviour, so a
# digest change points at the branch that moved rather than at "something".
SCENARIOS = {
    "peq-only": _base(vol_leveler=None, dialog_enhancer=None,
                      mb_comp=None, regulator=None),
    "peq-filter-type-mix": _base(peq_filters=_PEQ_MIXED),
    "peq-asymmetric-lr": _base(peq_filters=_PEQ_ASYMMETRIC),
    "full-chain": _base(),
    "mbc-4-band": _base(mb_comp=_MBC_4),
    "no-mbc": _base(mb_comp=None),
    "no-regulator": _base(regulator=None),
    "no-leveler-no-dialog": _base(vol_leveler=None, dialog_enhancer=None),
    "soundwire": _base(is_soundwire=True),
    "volmax-input-gain": _base(volmax_boost=9.0, volmax_slot="input-gain"),
    "volmax-output-gain": _base(volmax_boost=9.0, volmax_slot="output-gain"),
    "enable-autogain": _base(enabled={"autogain"}),
    "enable-coupled-bands": _base(regulator=_REG_COUPLED,
                                  enabled={"coupled-bands"}),
    "coupled-bands-available-but-off": _base(regulator=_REG_COUPLED),
    # --enable level-restore adds fir_peak_db to whatever slot carries
    # volmax-boost, so the pair pins both the sum and the untouched default.
    "enable-level-restore": _base(volmax_boost=9.0, fir_peak_db=11.4,
                                  enabled={"level-restore"}),
    "level-restore-available-but-off": _base(volmax_boost=9.0,
                                             fir_peak_db=11.4),
    "disable-volmax": _base(volmax_boost=9.0, disabled={"volmax"}),
    "disable-dynamics": _base(disabled={"mbc", "regulator"}),
    "disable-peq-shapes": _base(peq_filters=_PEQ_MIXED,
                                disabled={"high-shelf", "lo-pass", "dialog"}),
    # Every scenario above pins its keyword arguments, which makes the
    # *defaults* invisible to the digest — a changed default would move no
    # digest at all. These two call make_preset the way an outside caller
    # would, so the defaults are pinned too. The volmax slot is the one that
    # matters: it is a user-facing default that has already been flipped once
    # (issue #23), so it should never move again unnoticed.
    "defaults-only": dict(kernel_name="Golden", peq_filters=_PEQ_BELLS,
                          vol_leveler=_LEVELER, dialog_enhancer=_DIALOG,
                          mb_comp=_MBC_2, regulator=_REG,
                          freqs=SYNTHETIC_FREQS_20),
    "defaults-only-with-volmax": dict(kernel_name="Golden",
                                      peq_filters=_PEQ_BELLS,
                                      vol_leveler=_LEVELER,
                                      dialog_enhancer=_DIALOG,
                                      mb_comp=_MBC_2, regulator=_REG,
                                      freqs=SYNTHETIC_FREQS_20,
                                      volmax_boost=9.0),
}


END_TO_END = "end-to-end-synthetic-xml"
ALL_CASES = sorted([*SCENARIOS, END_TO_END])


def _end_to_end(workdir: Path):
    """Run the whole path a real conversion takes, on a synthetic XML.

    Returns (preset, irs_path) so both the digest fixture and the
    standalone test below drive exactly the same run.
    """
    xml = write_synthetic_tuning_xml(workdir / "DEV_0287_SUBSYS_TESTTEST.xml")
    tuning = parse_xml(xml)

    scale = tuning.ieq_amount / 100.0
    ao_left = [v / DB_FIXED_POINT_SCALE for v in tuning.ao_left]
    ao_right = [v / DB_FIXED_POINT_SCALE for v in tuning.ao_right]
    ieq = [v / DB_FIXED_POINT_SCALE * scale
           for v in tuning.curves["ieq_balanced"]]

    fir_left, _ = make_fir(tuning.freqs, [i + a for i, a in zip(ieq, ao_left)])
    fir_right, _ = make_fir(tuning.freqs, [i + a for i, a in zip(ieq, ao_right)])
    irs = workdir / "Golden.irs"
    save_wav_stereo(irs, fir_left, fir_right)

    preset, _ = make_preset(
        kernel_name=irs.stem,
        peq_filters=tuning.peq_filters,
        vol_leveler=tuning.vol_leveler,
        dialog_enhancer=tuning.dialog_enhancer,
        mb_comp=tuning.mb_comp,
        regulator=tuning.regulator,
        freqs=tuning.freqs,
        volmax_boost=tuning.volmax_boost,
    )
    return preset, irs


def _load_baseline() -> dict[str, str]:
    return json.loads(BASELINE_PATH.read_text())


@pytest.fixture(scope="module")
def digests(tmp_path_factory):
    current = {name: _digest(make_preset(**kwargs)[0])
               for name, kwargs in SCENARIOS.items()}
    preset, _ = _end_to_end(tmp_path_factory.mktemp("golden"))
    current[END_TO_END] = _digest(preset)
    if _UPDATE:
        BASELINE_PATH.write_text(json.dumps(current, indent=2,
                                            sort_keys=True) + "\n")
    return current


def test_golden_baseline_covers_exactly_the_scenarios(digests):
    """The baseline and the case table must not drift apart — a case added
    without a baseline entry would otherwise never actually be checked."""
    baseline = _load_baseline()
    assert set(baseline) == set(ALL_CASES), (
        f"baseline is missing {sorted(set(ALL_CASES) - set(baseline))} and "
        f"has stale {sorted(set(baseline) - set(ALL_CASES))}. Regenerate with "
        "ATMOS_UPDATE_GOLDEN=1 python3 -m pytest tests/test_golden_preset.py"
    )


@pytest.mark.parametrize("case", ALL_CASES)
def test_preset_parameters_match_the_golden_baseline(digests, case):
    """Every emitted parameter for this case is what it was when the
    baseline was last accepted."""
    expected = _load_baseline().get(case)
    assert digests[case] == expected, (
        f"preset output for {case!r} changed "
        f"({expected} -> {digests[case]}).\n"
        "If the change is intended, accept it with:\n"
        "  ATMOS_UPDATE_GOLDEN=1 python3 -m pytest tests/test_golden_preset.py\n"
        "and commit the baseline diff with the change that caused it."
    )


def test_scenarios_reach_every_make_preset_parameter():
    """No parameter of ``make_preset`` may go unexercised.

    This is the guard for the failure that motivated the file: the previous
    out-of-tree oracle silently stopped covering the arguments added after
    it was written, and eventually called one that had been removed. A new
    parameter now has to appear in a scenario before the suite goes green.
    """
    params = set(inspect.signature(make_preset).parameters)
    covered = set().union(*(set(kwargs) for kwargs in SCENARIOS.values()))
    assert params <= covered, (
        f"make_preset parameters not covered by any scenario: "
        f"{sorted(params - covered)}. Add one that varies them, so a change "
        "to their behaviour moves a digest."
    )


def test_flags_do_what_their_names_say(digests):
    """Two relationships between cases, stated outright rather than left
    as a coincidence a reader has to spot in the baseline file."""
    # --disable volmax must land exactly where a tuning with no boost does.
    assert digests["disable-volmax"] == digests["full-chain"]
    # --enable coupled-bands must actually reach the regulator.
    assert (digests["enable-coupled-bands"]
            != digests["coupled-bands-available-but-off"])
    # A fir_peak_db the flag hasn't been asked for must change nothing: the
    # value is passed on every run, so an accidental default-path leak would
    # otherwise ship silently.
    assert (digests["level-restore-available-but-off"]
            == digests["defaults-only-with-volmax"])
    assert (digests["enable-level-restore"]
            != digests["level-restore-available-but-off"])


def test_digest_ignores_the_generator_version():
    """Editing the working tree bumps the git-describe version string in
    every preset; the snapshot must not read that as an output change."""
    preset, _ = make_preset(**_base())
    bumped = json.loads(json.dumps(preset))
    bumped["_generator"] = "dolby_to_easyeffects.py v9999.99-dirty"
    assert _digest(bumped) == _digest(preset)


def test_digest_moves_when_a_parameter_moves():
    """Guard against the digest going blind — if it can't tell two
    different presets apart it would pass forever."""
    quiet, _ = make_preset(**_base(volmax_boost=0.0))
    loud, _ = make_preset(**_base(volmax_boost=9.0))
    assert _digest(quiet) != _digest(loud)


def test_end_to_end_irs_stays_minimum_phase(tmp_path):
    """The digest above covers the preset the end-to-end run emits; this
    covers the impulse response it writes alongside it. Minimum phase is
    the zero-added-latency invariant, and it is not expressible as a
    digest."""
    _, irs = _end_to_end(tmp_path)
    _, _, channels, left, _ = read_irs_file(irs)
    assert channels == 2
    assert is_minimum_phase(left, tol=1e-2)
