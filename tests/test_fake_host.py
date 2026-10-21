"""Guards tools/fake_host.py, the tree that makes a run see another device.

It is only useful while what it writes is what `lib/` reads, so each case
reads the tree back through the functions a standalone run picks its preset
with, rather than checking the file text.
"""
import argparse
import importlib.util
from pathlib import Path

import pytest

from lib import host
from lib.hardware import codecs, speakers

_REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "fake_host", _REPO / "tools" / "fake_host.py")
fake_host = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fake_host)


@pytest.mark.parametrize("sof", [True, False])
def test_the_tree_reads_back_as_that_codec_and_driver(tmp_path, monkeypatch,
                                                      capsys, sof):
    root = tmp_path / "host"
    flag = "--sof" if sof else "--no-sof"
    assert fake_host.main(["0x10ec0285:0x103C860F", flag,
                           "--root", str(root)]) == 0
    assert capsys.readouterr().out == f"{host.HOST_ROOT}={root}\n"
    monkeypatch.setenv(host.HOST_ROOT, str(root))
    assert [(v, s) for v, s, _ in codecs.get_hda_codec_ids()] == [
        ("10EC0285", "103C860F")]
    cards = host.path("/proc/asound/cards").read_text().splitlines()
    assert speakers._card_uses_sof(cards) is sof


@pytest.mark.parametrize("text", ["10EC0285", "10EC0285:103C86GZ",
                                  "10EC285:103C860F",
                                  "HDAUDIO:10EC0285:103C860F"])
def test_ids_that_are_not_two_hex_ids_are_refused(text):
    with pytest.raises(argparse.ArgumentTypeError):
        fake_host.parse_ids(text)


def test_the_driver_must_be_said(tmp_path):
    with pytest.raises(SystemExit):
        fake_host.main(["10EC0285:103C860F", "--root", str(tmp_path)])
