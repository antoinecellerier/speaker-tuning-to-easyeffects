"""Unit tests for lib/pipewire/validate.py.

The slow corpus tier exercises the same code through its CLI front end,
tools/measure_pw/validate_conf.py, as a subprocess; these tests lock the
load-bearing xm-MUTE inversion detector directly and fast, with no
lv2info/spa-json-dump dependency. The detector is the cheap guard against the
bug that once produced ~30 dB extra bass (an active PEQ band silently muted).
"""

from __future__ import annotations

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
