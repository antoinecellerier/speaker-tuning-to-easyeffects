"""Guards tools/preview_doctor.py — the --doctor scenario renderer.

The scenarios exist so /user-review can show a reviewer states this machine
isn't in. That only works while each one still *reaches* the state its slug
promises, and nothing in the tool connects the two: the registry names the
fixture, the shipped code decides the answer. A change to `_classify_sink`
could quietly turn the Bluetooth scenario into another speaker one, and the
round would report on a branch it never saw while `meta.txt` claimed
otherwise.

So these assert the reachable states — and assert them on the *rendered*
report, because the first version of this file checked the verdict function
directly and would have passed while the blocks showed something else
entirely (code review 2026-08-29).
"""
import importlib.util
import re
from pathlib import Path

import pytest

from lib.doctor import DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN, tag
from lib.hardware import sinks
from lib.preset.autoload import BYPASS_PRESET_NAME
from lib.report import doctor_run

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "preview_doctor", _REPO / "tools" / "preview_doctor.py"
)
preview_doctor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preview_doctor)


# slug -> (sink_kind, a speaker preset resolves, the Selected-preset status)
_EXPECTED = {
    "output-speakers": ("speaker", True, DOCTOR_PASS),
    "bypass-on-speakers": ("speaker", True, DOCTOR_WARN),
    "output-other-autoloaded": ("other", True, DOCTOR_PASS),
    "output-other-no-autoload": ("other", False, DOCTOR_UNKNOWN),
    "output-unknown": ("unknown", False, DOCTOR_WARN),
}


def test_registry_and_expectations_agree():
    """A new scenario has to say what it is reaching for, or it goes
    unchecked here and silently stops covering anything."""
    assert set(preview_doctor.SCENARIOS) == set(_EXPECTED)


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_each_scenario_reaches_the_state_its_slug_promises(slug):
    kind, has_preset, _ = _EXPECTED[slug]
    spec = preview_doctor.SCENARIOS[slug]
    with preview_doctor._scenario(slug) as autoload_dir:
        assert sinks.sink_kind(spec["default"]) == kind
        found = doctor_run._speaker_autoload_preset(autoload_dir)
        assert bool(found) is has_preset, found


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_each_scenario_stubs_easyeffects_own_state(slug):
    """TRAP: the stubs used to cover the sink graph only, so which branch a
    block rendered was decided by whatever preset the capture machine had
    loaded, and by whether its EasyEffects was pinned to a device (which
    routes around the graph stub entirely). Both made the block map lie."""
    spec = preview_doctor.SCENARIOS[slug]
    with preview_doctor._scenario(slug):
        from lib.preset import autoload
        live = doctor_run._resolve_live_state(autoload.read_ee_rc(""))
    assert (live.preset, live.preset_is_live) == (spec["preset"], True)
    assert (live.sink, live.sink_source) == (spec["default"], "live")


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_rendered_block_shows_the_status_the_scenario_is_for(slug, capsys,
                                                             monkeypatch):
    """The one that matters: assert on what a reviewer actually reads."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render(slug)
    out = capsys.readouterr().out
    # The check line, not the Environment row that now carries the same
    # words: only the check is tagged, and the tag is what is under test.
    line = next(ln for ln in out.splitlines()
                if re.match(r"\s*\[.+\]\s+Selected preset", ln))
    assert tag(_EXPECTED[slug][2]) in line, line


def test_scenarios_cover_every_selected_preset_branch():
    """The point of the set: a reviewer sees the healthy state, the fault,
    and the one that could not be checked — side by side, which is how
    "is something wrong?" gets tested at all."""
    assert {v[2] for v in _EXPECTED.values()} == {
        DOCTOR_PASS, DOCTOR_WARN, DOCTOR_UNKNOWN}


def test_the_bypass_scenarios_actually_select_the_bypass_preset():
    """Three of the five exist to render the bypass copy; a registry edit
    that dropped the preset would leave them rendering something else."""
    for slug in ("bypass-on-speakers", "output-other-autoloaded",
                 "output-other-no-autoload"):
        assert preview_doctor.SCENARIOS[slug]["preset"] == BYPASS_PRESET_NAME


def test_scenario_restores_every_probe_it_stubbed():
    """They all run in one process, so a leak would let one scenario decide
    the next one's answer — and the block map would still look right."""
    from lib.preset import autoload
    before = (sinks._enumerate_audio_sinks, sinks.live_default_sink,
              doctor_run._ee_query, autoload.read_ee_rc)
    with preview_doctor._scenario("output-other-autoloaded"):
        assert sinks._enumerate_audio_sinks is not before[0]
        assert doctor_run._ee_query is not before[2]
    assert (sinks._enumerate_audio_sinks, sinks.live_default_sink,
            doctor_run._ee_query, autoload.read_ee_rc) == before


def test_scenario_restores_the_probes_even_when_the_body_raises():
    from lib.preset import autoload
    before = (sinks._enumerate_audio_sinks, sinks.live_default_sink,
              doctor_run._ee_query, autoload.read_ee_rc)
    with pytest.raises(RuntimeError):
        with preview_doctor._scenario("output-speakers"):
            raise RuntimeError("boom")
    assert (sinks._enumerate_audio_sinks, sinks.live_default_sink,
            doctor_run._ee_query, autoload.read_ee_rc) == before
