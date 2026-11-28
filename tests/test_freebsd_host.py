"""The FreeBSD route to the machine's audio ids.

No FreeBSD box is needed to run these: the two tools the readers use, ``sysctl``
and the Linux ``/proc`` tree, are replaced with canned output. Each case reads
the result back through the same functions a converter calls, so it fails if a
Linux path stops working or a FreeBSD path stops being consulted.
"""

import subprocess

import pytest

from lib import tool_env
from lib.dax import discover
from lib.hardware import codecs

_HDACC_NODES = ("dev.hdacc.1\ndev.hdacc.1.%pnpinfo\n"
                "dev.hdacc.0\ndev.hdacc.0.%pnpinfo\n")

# The ThinkPad L14 Gen 1 this was reported on: an ATI HDMI codec on hdac0 and
# the Realtek speaker codec on hdac1, both with the machine's subsystem id.
_CODEC_SYSCTL = {
    ("sysctl", "-N", "dev.hdacc"): _HDACC_NODES,
    ("sysctl", "-b", "dev.hdacc.0.%pnpinfo"):
        "vendor=0x1002 device=0xaa01 revision=0x07 stepping=0x00",
    ("sysctl", "-b", "dev.hdacc.0.%desc"): "ATI R6xx HDA CODEC",
    ("sysctl", "-b", "dev.hdaa.0.%pnpinfo"): "type=0x01 subsystem=0x17aa507e",
    ("sysctl", "-b", "dev.hdacc.1.%pnpinfo"):
        "vendor=0x10ec device=0x0257 revision=0x00 stepping=0x01",
    ("sysctl", "-b", "dev.hdacc.1.%desc"): "Realtek ALC257 HDA CODEC",
    ("sysctl", "-b", "dev.hdaa.1.%pnpinfo"): "type=0x01 subsystem=0x17aa507e",
}

_PCI_SYSCTL = {
    ("sysctl", "-N", "dev.hdac"):
        "dev.hdac.1\ndev.hdac.1.%pnpinfo\ndev.hdac.0\ndev.hdac.0.%pnpinfo\n",
    ("sysctl", "-b", "dev.hdac.0.%pnpinfo"):
        "vendor=0x1002 device=0x1637 subvendor=0x17aa subdevice=0x0001",
    ("sysctl", "-b", "dev.hdacc.0.%desc"): "ATI R6xx HDMI HDA CODEC",
    ("sysctl", "-b", "dev.hdaa.0.%desc"): "ATI R6xx HDMI Audio Function Group",
    ("sysctl", "-b", "dev.hdac.1.%pnpinfo"):
        "vendor=0x1022 device=0x15e3 subvendor=0x17aa subdevice=0x507e",
    ("sysctl", "-b", "dev.hdacc.1.%desc"): "Realtek ALC257 HDA CODEC",
    ("sysctl", "-b", "dev.hdaa.1.%desc"): "Realtek ALC257 Audio Function Group",
}


def _sysctl(monkeypatch, outputs):
    """Serve *outputs* from ``tool_env.run``; return the argv list it saw."""
    seen = []

    def run(argv, **kwargs):
        seen.append(tuple(argv))
        text = outputs.get(tuple(argv))
        return subprocess.CompletedProcess(argv, 0 if text is not None else 1,
                                          text or "", "")

    monkeypatch.setattr(tool_env, "run", run)
    return seen


def _codec_dump(fake_host, vendor="10ec0285", subsys="103c860f"):
    fake_host("/proc/asound/card0/codec#0",
              f"Codec: fake codec {vendor}\nVendor Id: 0x{vendor}\n"
              f"Subsystem Id: 0x{subsys}\n")


def test_freebsd_codec_ids_come_from_hdacc_and_hdaa(monkeypatch):
    seen = _sysctl(monkeypatch, _CODEC_SYSCTL)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_hda_codec_ids() == [
        ("1002AA01", "17AA507E", "ATI R6xx"),
        ("10EC0257", "17AA507E", "Realtek ALC257"),
    ]
    # Numeric order, not the order sysctl -N happened to list the nodes in.
    assert seen[0] == ("sysctl", "-N", "dev.hdacc")
    assert ("sysctl", "-b", "dev.hdaa.1.%pnpinfo") in seen


def test_freebsd_codec_without_an_afg_subsystem_is_skipped(monkeypatch):
    outputs = dict(_CODEC_SYSCTL)
    del outputs[("sysctl", "-b", "dev.hdaa.1.%pnpinfo")]
    _sysctl(monkeypatch, outputs)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_hda_codec_ids() == [("1002AA01", "17AA507E", "ATI R6xx")]


def test_freebsd_with_no_sysctl_sees_no_codecs(monkeypatch):
    # The suite's ATMOS_NO_LIVE_TOOLS is still on: the real tool isn't run.
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_hda_codec_ids() == []


def test_a_proc_codec_dump_still_wins_on_freebsd(monkeypatch, fake_host):
    _codec_dump(fake_host)
    seen = _sysctl(monkeypatch, _CODEC_SYSCTL)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_hda_codec_ids() == [
        ("10EC0285", "103C860F", "fake codec 10ec0285")]
    assert seen == [], "sysctl was read even though /proc/asound had a codec"


def test_linux_never_reads_freebsd_sysctls(monkeypatch, fake_host):
    _codec_dump(fake_host)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: False)
    monkeypatch.setattr(codecs, "_freebsd_sysctl_nodes",
                        lambda *_: pytest.fail("Linux path read sysctl"))
    assert codecs.get_hda_codec_ids() == [
        ("10EC0285", "103C860F", "fake codec 10ec0285")]


def test_freebsd_pci_subsystem_prefers_the_analog_controller(monkeypatch):
    _sysctl(monkeypatch, _PCI_SYSCTL)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_pci_audio_subsystem() == ("17AA", "507E")


def test_freebsd_pci_subsystem_with_no_controller(monkeypatch):
    _sysctl(monkeypatch, {})
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert codecs.get_pci_audio_subsystem() is None


def test_freebsd_sysctls_select_the_matching_tuning(tmp_path, monkeypatch):
    xml = tmp_path / "DEV_0257_SUBSYS_17AA507E_PCI_SUBSYS_507E17AA.xml"
    xml.write_text("<device_data/>")
    _sysctl(monkeypatch, _CODEC_SYSCTL)
    monkeypatch.setattr(codecs, "_is_freebsd", lambda: True)
    assert discover.find_tuning_xml(tmp_path) == xml
