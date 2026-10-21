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
import json
import re
import tempfile
from pathlib import Path

import pytest

from lib import ee_autostart
from lib.doctor import DOCTOR_PASS, DOCTOR_UNKNOWN, DOCTOR_WARN, tag
from lib.hardware import sinks
from lib.preset.autoload import BYPASS_PRESET_NAME
from lib.report import doctor_run, environment
from tests.conftest import assert_summary_counts_the_printed_lines

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
    # The speaker tuning itself on the headset: the over-processed fault.
    "dolby-on-headset": ("other", True, DOCTOR_WARN),
    "output-unknown": ("unknown", False, DOCTOR_WARN),
    "presets-from-another-version": ("speaker", True, DOCTOR_PASS),
    "no-pipewire": ("unknown", False, DOCTOR_WARN),
    # A hardware-state scenario: its sink/preset axes are the healthy
    # baseline's, and what it varies is the machine the checks read.
    "speaker-routed-past-volume": ("speaker", True, DOCTOR_PASS),
    # The table-free twin: the same machine shape with an id neither quirk
    # table lists (issue #95), so the check fires without an upstream row.
    "speaker-level-unlisted": ("speaker", True, DOCTOR_PASS),
    # A session-state scenario, same shape: healthy sink and preset, and what
    # it varies is the PipeWire clock the checks read.
    "graph-rate-too-high": ("speaker", True, DOCTOR_PASS),
    # Startup-state scenarios (issue #117): healthy sink and preset, and what
    # they vary is how EasyEffects gets started.
    "session-launched": ("speaker", True, DOCTOR_PASS),
    "service-mode-untraced": ("speaker", True, DOCTOR_PASS),
    "compositor-toggle-only": ("speaker", True, DOCTOR_PASS),
}


def test_the_stale_version_scenario_renders_the_warn(capsys, monkeypatch):
    """The scenario exists so /user-review can put the new WARN in front of
    a reviewer; the healthy baseline must not carry it."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render("presets-from-another-version")
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines()
                if "Presets from another version" in ln)
    assert tag(DOCTOR_WARN) in line, line
    joined = " ".join(ln.strip() for ln in out.splitlines())
    assert "written by v2026.01" in joined
    preview_doctor.render("output-speakers")
    assert "another version" not in capsys.readouterr().out


def test_the_graph_rate_scenario_renders_the_warn(capsys, monkeypatch):
    """Issue #84. The dB figure is computed from the rate, so the assertion is
    on the number too — a hard-coded 12 would be false at every other rate."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render("graph-rate-too-high")
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "Graph sample rate" in ln)
    assert tag(DOCTOR_WARN) in line, line
    joined = " ".join(ln.strip() for ln in out.splitlines())
    assert "192000 Hz" in joined and "12 dB more" in joined
    preview_doctor.render("output-speakers")
    assert "Graph sample rate" not in capsys.readouterr().out


def test_the_unlisted_level_scenario_renders_the_warn(capsys, monkeypatch):
    """Issue #95: the routing shape under an id neither table lists, so the
    reviewer reads the hedged copy. The two scenarios are mutually exclusive."""
    monkeypatch.setenv("COLUMNS", "80")
    # The check asks PipeWire whether it mixes volume in software before it
    # says anything; left real, the capture machine's session would decide
    # whether the scenario renders the WARN it exists for.
    monkeypatch.setattr(sinks, "soft_mixer_in_use", lambda: False)
    preview_doctor.render("speaker-level-unlisted")
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if "Speaker level" in ln)
    assert tag(DOCTOR_WARN) in line, line
    joined = " ".join(ln.strip() for ln in out.splitlines())
    assert "1D059999" in joined and "issue #95" in joined

    # Assert on the injected id, not the row label: a host that genuinely has
    # this fault (a firmware port switched off, issue #95 — reproduced on the
    # dev machine) renders "Speaker level" for real, and asserting the label
    # away made this pass only on healthy hardware.
    preview_doctor.render("output-speakers")
    assert "1D059999" not in capsys.readouterr().out
    preview_doctor.render("speaker-routed-past-volume")
    assert "1D059999" not in capsys.readouterr().out


def test_registry_and_expectations_agree():
    """A new scenario has to say what it is reaching for, or it goes
    unchecked here and silently stops covering anything."""
    assert set(preview_doctor.SCENARIOS) == set(_EXPECTED)


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_each_scenario_reaches_the_state_its_slug_promises(slug):
    kind, has_preset, _ = _EXPECTED[slug]
    spec = preview_doctor.SCENARIOS[slug]
    with preview_doctor._scenario(slug) as (_out, _irs, autoload_dir, _rc):
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
    # A scenario with no default sink resolves through the (cleared) rc.
    source = "live" if spec["default"] else "saved"
    assert (live.sink, live.sink_source) == (spec["default"], source)


def test_scenario_stages_the_presets_the_report_globs():
    """TRAP (CI 2026-08-29): the report reads the presets and impulse files
    off disk, and those were still the real install's. On a machine without
    EasyEffects the folder is empty, so the generated-preset set was empty
    too and every "is the loaded preset one of ours?" branch answered no —
    two scenarios rendered the wrong status under the right slug, and it
    passed on the one laptop that happened to have presets."""
    with preview_doctor._scenario("output-speakers") as (out, irs, _autoload, _rc):
        stems = {p.stem for p in out.glob("*.json")}
        assert stems == set(preview_doctor._PRESETS) | {BYPASS_PRESET_NAME}
        # Each preset's convolver must find its impulse, or the per-preset
        # checks FAIL and the block stops looking like a healthy install.
        assert {p.stem for p in irs.glob("*.irs")}
        for preset in out.glob("*.json"):
            if preset.stem == BYPASS_PRESET_NAME:
                continue
            data = json.loads(preset.read_text())
            kernel = data["output"]["convolver#0"]["kernel-name"]
            assert (irs / f"{kernel}.irs").exists(), kernel
            # And each must read as one of ours, or the report checks none
            # of them and every scenario renders the "nothing here is this
            # tool's" branch under the right slug (issue #84 scoping).
            assert environment.is_generated_preset(data, preset.stem), preset


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_rendered_block_shows_the_status_the_scenario_is_for(slug, capsys,
                                                             monkeypatch):
    """The one that matters: assert on what a reviewer actually reads."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render(slug)
    out = capsys.readouterr().out
    # The check line, not the setup block's row that carries the same
    # words: only the check is tagged, and the tag is what is under test.
    line = next(ln for ln in out.splitlines()
                if re.match(r"\s*\[.+\]\s+Selected preset", ln))
    assert tag(_EXPECTED[slug][2]) in line, line


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_summary_counts_the_lines_the_report_printed(slug, capsys, monkeypatch):
    """TRAP: the summary used to total the checks behind the folded preset
    line, so it read two higher than the [PASS] lines on screen — the top
    finding of two /user-review rounds, in a report whose whole job is to be
    pasted into an issue.

    Every scenario stages two presets, so the fold is live in all of them:
    asserting the folded line is here keeps the count from passing vacuously
    on a report that never collapsed anything."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render(slug)
    out = capsys.readouterr().out
    assert "Presets (2 of 2 passed)" in out, "nothing was folded to count"
    assert_summary_counts_the_printed_lines(out)


@pytest.mark.parametrize("slug", list(_EXPECTED))
def test_rendered_block_never_names_the_staging_tree(slug, capsys, monkeypatch):
    """The presets are staged in a temp tree because a preview must not write
    into the reader's own install — but a reviewer seeing "writes to /tmp/…"
    reports it as a fault, and it is ours, not the tool's."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render(slug)
    out = capsys.readouterr().out
    assert tempfile.gettempdir() not in out
    assert "/tmp" not in out


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
    before = (sinks._enumerate_audio_sinks, sinks.live_session,
              doctor_run._ee_query, autoload.read_ee_rc,
              ee_autostart.background_launch_source,
              ee_autostart.service_mode_launch, ee_autostart.own_processes)
    with preview_doctor._scenario("output-other-autoloaded"):
        assert sinks._enumerate_audio_sinks is not before[0]
        assert doctor_run._ee_query is not before[2]
        assert ee_autostart.background_launch_source is not before[4]
    assert (sinks._enumerate_audio_sinks, sinks.live_session,
            doctor_run._ee_query, autoload.read_ee_rc,
            ee_autostart.background_launch_source,
            ee_autostart.service_mode_launch, ee_autostart.own_processes) == before


def test_scenario_restores_the_probes_even_when_the_body_raises():
    from lib.preset import autoload
    before = (sinks._enumerate_audio_sinks, sinks.live_session,
              doctor_run._ee_query, autoload.read_ee_rc,
              ee_autostart.background_launch_source,
              ee_autostart.service_mode_launch, ee_autostart.own_processes)
    with pytest.raises(RuntimeError):
        with preview_doctor._scenario("output-speakers"):
            raise RuntimeError("boom")
    assert (sinks._enumerate_audio_sinks, sinks.live_session,
            doctor_run._ee_query, autoload.read_ee_rc,
            ee_autostart.background_launch_source,
            ee_autostart.service_mode_launch, ee_autostart.own_processes) == before


@pytest.mark.parametrize("slug, status", [
    ("session-launched", DOCTOR_PASS),
    ("service-mode-untraced", DOCTOR_UNKNOWN),
    ("compositor-toggle-only", DOCTOR_UNKNOWN),
])
def test_startup_scenarios_render_their_background_service_status(
        slug, status, capsys, monkeypatch):
    """Issue #117: each scenario stages its own rc, so the Background service
    check renders, and the status it reaches for, on a machine with no
    EasyEffects install too."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render(slug)
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines()
                if re.match(r"\s*\[.+\]\s+Background service", ln))
    assert tag(status) in line, line


# --- The PipeWire doctor's scenarios ----------------------------------------
#
# Same contract: each slug must reach its state on the rendered report. The
# checks asserted here read the staged graph, which the scenario answers as,
# so they hold with the machine off (no spa-json-dump, so the staged confs
# read as unparsed; the graph alone decides these lines).

from lib.doctor import DOCTOR_FAIL                       # noqa: E402
from lib.pipewire import checks, session                 # noqa: E402

# slug -> (check title, status) the report must carry, or None for a slug
# whose point is that neither multi-chain check fires.
_PW_EXPECTED = {
    "pw-healthy": None,
    "pw-stacked": ("Stacked filter chains", DOCTOR_FAIL),
    "pw-several-virtual-sinks": ("Several chains as separate outputs", DOCTOR_WARN),
    "pw-several-unpinned": ("Several chains as separate outputs", DOCTOR_WARN),
    "pw-chain-selected": ("Default output", DOCTOR_WARN),
}


@pytest.fixture(autouse=True)
def _staged_confs_parse(monkeypatch):
    """Read the staged confs' routing the way spa-json-dump would.

    The machine is off in the suite, so the real parse marks every staged conf
    unreadable, and the checks that only count this tool's own chains would
    see none. This stands in for the tool on the props the scenarios stage."""
    real = checks.parse_conf

    def parse(path):
        conf = real(path)
        if conf.readable:
            return conf
        text = Path(path).read_text()
        m = re.search(r'node\.name = "(effect_input\.[^"]+)"', text)
        conf.node_name = m.group(1) if m else ""
        conf.smart = "filter.smart = true" in text
        pin = re.search(r'target\.object = "([^"]+)"', text)
        conf.pinned = pin.group(1) if pin else ""
        conf.readable, conf.unreadable = True, ""
        return conf
    monkeypatch.setattr(checks, "parse_conf", parse)


def test_pw_registry_and_expectations_agree():
    assert set(_PW_EXPECTED) == set(preview_doctor.PW_SCENARIOS)


@pytest.mark.parametrize("slug", list(_PW_EXPECTED))
def test_pw_rendered_block_shows_the_check_the_scenario_is_for(slug, capsys,
                                                               monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render_pw(slug)
    out = capsys.readouterr().out
    expected = _PW_EXPECTED[slug]
    assert "=== PipeWire filter-chain doctor ===" in out, "nothing rendered"
    if expected is None:
        assert "Stacked filter chains" not in out
        assert "Several chains as separate outputs" not in out
        return
    title, status = expected
    line = next(ln for ln in out.splitlines()
                if re.match(rf"\s*\[.+\]\s+{re.escape(title)}", ln))
    assert tag(status) in line, line


def test_pw_unpinned_scenario_carries_the_pin_sentence(capsys, monkeypatch):
    """The two several-chains scenarios differ only in the pin, and the merged
    check words that difference: one has the sentence, the other must not."""
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render_pw("pw-several-unpinned")
    flat = " ".join(capsys.readouterr().out.split())
    assert "aren't tied to your speakers" in flat
    preview_doctor.render_pw("pw-several-virtual-sinks")
    flat = " ".join(capsys.readouterr().out.split())
    assert "aren't tied to your speakers" not in flat


@pytest.mark.parametrize("slug", list(_PW_EXPECTED))
def test_pw_rendered_block_never_names_the_staging_tree(slug, capsys,
                                                        monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    preview_doctor.render_pw(slug)
    out = capsys.readouterr().out
    assert tempfile.gettempdir() not in out
    assert "/tmp" not in out


def test_pw_scenario_restores_every_probe_even_when_the_body_raises():
    from lib import doctor
    before = (checks._pw_dump, checks.DEFAULT_OUTPUT_DIR,
              checks._UNSCANNED_CONF_DIR, doctor.tilde,
              session.read_settings, session.read_xruns, session.process_age)
    with pytest.raises(RuntimeError):
        with preview_doctor._pw_scenario("pw-stacked"):
            assert checks._pw_dump is not before[0]
            raise RuntimeError("boom")
    assert (checks._pw_dump, checks.DEFAULT_OUTPUT_DIR,
            checks._UNSCANNED_CONF_DIR, doctor.tilde,
            session.read_settings, session.read_xruns,
            session.process_age) == before
