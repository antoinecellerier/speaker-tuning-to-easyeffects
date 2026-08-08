"""Unit tests for lib/pipewire/validate.py.

The slow corpus tier exercises the same code through its CLI front end,
tools/measure_pw/validate_conf.py, as a subprocess; these tests lock the
load-bearing xm-MUTE inversion detector directly and fast, with no
lv2info/spa-json-dump dependency. The detector is the cheap guard against the
bug that once produced ~30 dB extra bass (an active PEQ band silently muted).
"""

from __future__ import annotations

import subprocess

import pytest

from lib.pipewire import validate

LSP_PEQ_URI = "http://lsp-plug.in/plugins/lv2/para_equalizer_x16_lr"


def _peq_node(control: dict) -> dict:
    return {"type": "lv2", "name": "peq", "plugin": LSP_PEQ_URI,
            "control": control}


def test_check_peq_mute_flags_active_band_muted():
    # Bell (ft=1) paired with xm=1 is the inversion trap: an active filter
    # type that is silently muted.
    errs = validate._check_peq_mute(_peq_node({"ftl_0": 1, "xml_0": 1}))
    assert len(errs) == 1
    assert "xml_0=1" in errs[0]


def test_check_peq_mute_clean_when_active_band_unmuted():
    assert validate._check_peq_mute(_peq_node({"ftl_0": 1, "xml_0": 0})) == []


def test_check_peq_mute_ignores_off_band():
    # An Off (ft=0) band may carry xm=1 harmlessly — nothing to mute.
    assert validate._check_peq_mute(_peq_node({"ftl_0": 0, "xml_0": 1})) == []


def test_check_peq_mute_flags_both_channels_independently():
    errs = validate._check_peq_mute(_peq_node(
        {"ftl_0": 1, "xml_0": 1, "ftr_0": 1, "xmr_0": 1}))
    assert len(errs) == 2


def test_check_peq_mute_skips_bands_missing_keys():
    # A band with no ft/xm pair present is simply not checked.
    assert validate._check_peq_mute(_peq_node({"g_in": 1.0})) == []


_LV2INFO_BLOCK = (
    "\n\tPort 4:\n"
    "\t\tType:        http://lv2plug.in/ns/lv2core#ControlPort\n"
    "\t\t             http://lv2plug.in/ns/lv2core#InputPort\n"
    "\t\tSymbol:      g_in\n"
    "\t\tName:        Input gain\n"
    "\t\tMinimum:     {minimum}\n"
    "\t\tMaximum:     10.000000\n"
    "\t\tDefault:     1.000000\n"
)


def test_parse_lv2info_records_a_bound_it_cannot_read():
    """An unreadable bound used to raise `ValueError` out of the parser and
    abort the run before the conf was written. It now leaves the bound unknown
    — and says which field it was, because a range that silently stops being
    checked is false clearance.
    """
    # A decimal comma is what a non-C-locale lv2info prints.
    ports = validate._parse_lv2info(_LV2INFO_BLOCK.format(minimum="0,000000"))
    assert ports["g_in"].minimum is None
    assert ports["g_in"].unparsed == ("Minimum",)
    # The bounds either side of it still parsed.
    assert ports["g_in"].maximum == 10.0 and ports["g_in"].default == 1.0

    # A bound lv2info simply omits is not a parse failure: the port has no
    # such limit, so no check was forgone and nothing is recorded.
    omitted = _LV2INFO_BLOCK.format(minimum="0.0").replace(
        "\t\tMinimum:     0.0\n", "")
    assert validate._parse_lv2info(omitted)["g_in"].unparsed == ()


def test_validate_warns_once_per_node_about_unreadable_bounds():
    """The warning names the plugin, the port and the field, and is raised at
    the point a check is forgone — so a port the conf never writes is not
    reported, and one misformatted lv2info yields one line, not hundreds.
    """
    schema = validate._parse_lv2info(
        _LV2INFO_BLOCK.format(minimum="0,000000")
        + _LV2INFO_BLOCK.format(minimum="0,000000").replace("g_in", "g_out"))
    node = {"type": "lv2", "name": "peq", "plugin": LSP_PEQ_URI,
            "control": {"g_in": 1.0}}
    errors, warnings = validate.validate([node], {LSP_PEQ_URI: schema})

    assert errors == []
    assert len(warnings) == 1
    assert "peq: lv2info reported 1 port bound" in warnings[0]
    assert "para_equalizer_x16_lr" in warnings[0]
    assert "g_in Minimum" in warnings[0]
    # g_out is just as unreadable, but nothing was going to check it.
    assert "g_out" not in warnings[0]


def test_a_failed_lv2info_exec_degrades_to_a_warning(monkeypatch):
    """One plugin `lv2info` won't answer for is not a reason to fail a conf
    the others validated fine. Every exec failure — a vanished binary, a
    timeout, a fork that couldn't allocate — arrives as one `RuntimeError`
    naming the URI, which `run` turns into a warning.
    """
    for exc in (FileNotFoundError(2, "No such file or directory", "lv2info"),
                subprocess.TimeoutExpired(cmd="lv2info", timeout=10),
                subprocess.SubprocessError("fork failed")):
        def boom(*a, _exc=exc, **k):
            raise _exc
        monkeypatch.setattr(validate.subprocess, "run", boom)
        with pytest.raises(RuntimeError, match="lv2info 'urn:x' failed"):
            validate.lv2info_schema("urn:x")

    # The last exec stub above is still installed, so `run` meets a plugin it
    # can get no schema for. Both CLIs are faked present because the
    # missing-tooling gate runs first, and this is about what happens past it.
    monkeypatch.setattr(validate.shutil, "which",
                        lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(validate, "parse_conf", lambda text: [
        {"type": "lv2", "name": "peq", "plugin": LSP_PEQ_URI, "control": {}}])
    report = validate.run("")
    assert report.status == validate.CLEAN
    assert any("lv2info" in w and LSP_PEQ_URI in w for w in report.warnings)


def test_run_maps_only_a_tool_failure_to_unchecked(monkeypatch):
    """`UNCHECKED` is a skip, not a verdict, so nothing generic may reach it.

    `ee_to_pipewire.py` prints that status dim and writes the conf anyway, and
    `tests/corpus/test_ee_to_pipewire_corpus.py` turns the same outcome into
    `pytest.skip` — so a bug inside `parse_conf` that landed here would skip
    every XML in the corpus and leave the run green. A tool that failed maps
    to it; a `KeyError` in our own walk of the parsed conf does not.

    Both CLIs are faked as present because the missing-tooling gate runs
    first, and this is about what happens past it.
    """
    monkeypatch.setattr(validate.shutil, "which",
                        lambda name, *a, **k: f"/usr/bin/{name}")

    def raising(exc):
        def parse_conf(text):
            raise exc
        return parse_conf

    monkeypatch.setattr(validate, "parse_conf",
                        raising(RuntimeError("spa-json-dump failed: boom")))
    report = validate.run("")
    assert report.status == validate.UNCHECKED
    assert "spa-json-dump failed" in report.reason
    assert report.errors == () and report.warnings == ()

    monkeypatch.setattr(validate, "parse_conf",
                        raising(KeyError("filter.graph")))
    with pytest.raises(KeyError):
        validate.run("")
