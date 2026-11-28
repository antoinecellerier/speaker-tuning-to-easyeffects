"""The ids that say which machine this is: HDA codecs, SoundWire, PCI subsystem.

Everything downstream keys on these. A Dolby tuning filename carries a
subsystem id (``..._SUBSYS_17AA22E6.xml``), so the autoprobe matches on them;
the kernel's speaker-pin quirks are keyed by codec SSID and, off SOF, by the
PCI subsystem id; and ``--speaker-info`` prints all three so a pasted device
report identifies its own hardware.

``get_pci_audio_subsystem`` carries the one piece of policy here — which
controller to believe on a machine that has several — because the answer has
to be the analog codec's rather than a discrete GPU's HDMI function (issue
#33).

Standard library only, and nothing runs at import: a handful of reads under
``/proc/asound`` and ``/sys`` on Linux, of ``sysctl`` through
``lib.tool_env`` on FreeBSD, so importing it costs nothing
(``tests/test_layout.py``'s ``STDLIB_ONLY``).
"""

import platform
import re
from pathlib import Path

from lib import host, tool_env


def _is_freebsd() -> bool:
    """True where the machine's audio ids come from sysctl, not /proc and /sys."""
    return platform.system() == "FreeBSD"


# FreeBSD's HDA driver exposes the same two ids that Linux prints in
# /proc/asound/card*/codec#*, split across the codec and its audio function
# group: dev.hdacc.N.%pnpinfo holds vendor/device, dev.hdaa.N.%pnpinfo the
# subsystem. Both are read through tool_env so the suite's tool gate applies.
_FREEBSD_VENDOR_RE = re.compile(r"\bvendor=0x([0-9a-fA-F]{4})\b")
_FREEBSD_DEVICE_RE = re.compile(r"\bdevice=0x([0-9a-fA-F]{4})\b")
_FREEBSD_SUBSYS_RE = re.compile(r"\bsubsystem=0x([0-9a-fA-F]{8})\b")
_FREEBSD_SUBVENDOR_RE = re.compile(r"\bsubvendor=0x([0-9a-fA-F]{4})\b")
_FREEBSD_SUBDEVICE_RE = re.compile(r"\bsubdevice=0x([0-9a-fA-F]{4})\b")


def _freebsd_sysctl(oid: str) -> str:
    """One FreeBSD sysctl's raw value (``sysctl -b``), or "" if unreadable.

    Through ``lib.tool_env``, the same door every external tool in lib/ uses:
    with the suite's ``ATMOS_NO_LIVE_TOOLS`` set this raises, which a test
    sees as a machine with no codecs rather than a call to the real hardware.
    """
    try:
        result = tool_env.run(["sysctl", "-b", oid],
                              capture_output=True, text=True)
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _freebsd_sysctl_nodes(prefix: str) -> list[tuple[int, str]]:
    """The numbered ``prefix.N`` device nodes, sorted by N.

    ``sysctl -N`` lists children and attributes in driver order, not numeric
    order (hdacc1 came before hdacc0 on the machine this was written on), so
    sort here to keep the result deterministic.
    """
    try:
        result = tool_env.run(["sysctl", "-N", prefix],
                              capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode != 0:
        return []
    node_re = re.compile(rf"{re.escape(prefix)}\.(\d+)")
    nodes = [(int(m.group(1)), line.strip())
             for line in result.stdout.splitlines()
             if (m := node_re.fullmatch(line.strip()))]
    return sorted(nodes)


def get_hda_codec_ids():
    """Read HDA codec names and subsystem IDs from the machine.

    Linux reads ``/proc/asound/card*/codec*``; FreeBSD reads the matching
    ``dev.hdacc.N`` and ``dev.hdaa.N`` sysctls. A codec dump under
    ``ATMOS_HOST_ROOT`` wins on either OS, which is what keeps
    ``tools/fake_host.py``'s synthetic tree readable everywhere.

    Returns a list of (vendor_id, subsystem_id, codec_name) tuples, e.g.
    [("10EC0287", "17AA22E6", "Realtek ALC287")].
    """
    results = []
    for codec_path in sorted(host.path("/proc/asound").glob("card*/codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        codec_name = ""
        vendor_id = None
        subsys_id = None
        for line in text.splitlines():
            if line.startswith("Codec:"):
                codec_name = line.split(":", 1)[1].strip()
            elif line.startswith("Vendor Id:"):
                vendor_id = line.split("0x", 1)[-1].strip().upper()
            elif line.startswith("Subsystem Id:"):
                subsys_id = line.split("0x", 1)[-1].strip().upper()
        if vendor_id and subsys_id:
            results.append((vendor_id, subsys_id, codec_name))
    if results or not _is_freebsd():
        return results

    ids = []
    for number, node in _freebsd_sysctl_nodes("dev.hdacc"):
        pnp = _freebsd_sysctl(f"{node}.%pnpinfo")
        vendor = _FREEBSD_VENDOR_RE.search(pnp)
        device = _FREEBSD_DEVICE_RE.search(pnp)
        subsys = _FREEBSD_SUBSYS_RE.search(
            _freebsd_sysctl(f"dev.hdaa.{number}.%pnpinfo"))
        if not (vendor and device and subsys):
            continue
        # The FreeBSD description ends in " HDA CODEC", a suffix Linux's
        # /proc/asound name doesn't carry; drop it so both read "ALC257".
        name = re.sub(r"\s+HDA CODEC$", "", _freebsd_sysctl(f"{node}.%desc"),
                      flags=re.I)
        ids.append((f"{vendor.group(1)}{device.group(1)}".upper(),
                    subsys.group(1).upper(), name))
    return ids


def get_hda_codec_ids_without_display():
    """`get_hda_codec_ids` as (vendor_id, subsystem_id) pairs, without the
    HDMI/DisplayPort codecs: the ones a speaker tuning can be keyed by."""
    return [(v, s) for v, s, name in get_hda_codec_ids()
            if "HDMI" not in name.upper()
            and "DISPLAYPORT" not in name.upper()]


# Where the SoundWire bus lives in sysfs, and the shape of a slave device's
# name under it: "sdw:L:N:MMMM:PPPP:VV", capturing the manufacturer and part
# ids the Dolby SOUNDWIRE_MAN_*_FUNC_* filenames key on.
#
# Both names are shared rather than re-spelled at each reader. ``iterdir`` the
# bus, keep what the regex matches: that is how ``get_soundwire_ids`` below
# opens, and how ``lib.hardware.speakers._detect_soundwire_speakers``'s
# amplifier probe opens. ``get_pci_audio_subsystem`` takes the path on its
# own, reading it in the body rather than as a parameter default. A default
# would hold this object for the life of the process, out of reach of any
# ``monkeypatch.setattr`` on this module (``tests/test_layout.py`` guards
# exactly that). Only the entry point is shared; the loop bodies read
# different things and are deliberately kept apart.
SDW_BUS = Path("/sys/bus/soundwire/devices")
SDW_SLAVE_RE = re.compile(r"sdw:\d+:\d+:([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):\d+")


def get_soundwire_ids():
    """Read SoundWire device IDs from /sys/bus/soundwire/devices.

    Returns a list of (manufacturer_id, part_id) tuples as uppercase hex
    strings, e.g. [("025D", "1318")].
    """
    results = []
    bus = host.path(SDW_BUS)
    if not bus.is_dir():
        return results
    for dev_dir in sorted(bus.iterdir()):
        match = SDW_SLAVE_RE.match(dev_dir.name)
        if match:
            man_id = match.group(1).upper()
            part_id = match.group(2).upper()
            results.append((man_id, part_id))
    return results


def _walk_to_pci_subsys(start: Path):
    """Walk up sysfs from `start` to find the nearest PCI subsystem IDs."""
    current = start.resolve()
    while current != Path("/"):
        subsys_vendor_path = current / "subsystem_vendor"
        subsys_device_path = current / "subsystem_device"
        if subsys_vendor_path.exists() and subsys_device_path.exists():
            try:
                vendor = subsys_vendor_path.read_text().strip()
                device = subsys_device_path.read_text().strip()
            except OSError:
                pass
            else:
                vendor = vendor.replace("0x", "").upper()
                device = device.replace("0x", "").upper()
                if vendor and device:
                    return (vendor, device)
        current = current.parent
    return None


def _card_pci_preference(card_name: str, proc_asound: Path) -> int:
    """Rank a sound card for the PCI-subsystem probe.

    0 = has a non-HDMI HDA codec (the analog controller quirks and Dolby SKU
    ids key on), 1 = no HDA codec info (e.g. USB), 2 = HDMI/DP codecs only (a
    GPU audio function whose PCI subsystem is the GPU's, not the machine
    SKU's)."""
    names = []
    for codec_path in sorted((proc_asound / card_name).glob("codec*")):
        try:
            text = codec_path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("Codec:"):
                names.append(line.split(":", 1)[1].strip())
    if not names:
        return 1
    if all("HDMI" in n.upper() for n in names):
        return 2
    return 0


def _freebsd_pci_subsystem():
    """PCI subsystem of the analog HDA controller, or None.

    FreeBSD puts the same ``subvendor``/``subdevice`` ids Linux walks up to in
    sysfs on each ``dev.hdac.N.%pnpinfo``. A display codec's controller is
    ranked last, as the Linux path ranks a GPU's HDMI function last: its
    subsystem identifies the GPU, not the machine's SKU.
    """
    ranked = []
    for number, node in _freebsd_sysctl_nodes("dev.hdac"):
        pnp = _freebsd_sysctl(f"{node}.%pnpinfo")
        vendor = _FREEBSD_SUBVENDOR_RE.search(pnp)
        device = _FREEBSD_SUBDEVICE_RE.search(pnp)
        if not (vendor and device):
            continue
        codec = _freebsd_sysctl(f"dev.hdacc.{number}.%desc").upper()
        afg = _freebsd_sysctl(f"dev.hdaa.{number}.%desc").upper()
        display = any(word in codec or word in afg
                      for word in ("HDMI", "DISPLAYPORT"))
        ranked.append((display, number,
                       (vendor.group(1).upper(), device.group(1).upper())))
    return min(ranked)[2] if ranked else None


def get_pci_audio_subsystem(
    sound_class=Path("/sys/class/sound"),
    proc_asound=Path("/proc/asound"),
    sdw_bus=None,
):
    """Get the PCI subsystem ID of the audio controller.

    Returns (subsys_vendor, subsys_device) as uppercase 4-char hex strings,
    e.g. ("17AA", "2339"), or None if not found.

    Prefers the PCI ancestor of a SoundWire device when present so we pick
    the controller that actually hosts the speaker amplifiers. Whichever
    /sys/class/sound card sorts first may be HDMI audio on a discrete GPU.
    On FreeBSD, where there is no sysfs, the same ids come from
    ``dev.hdac.N.%pnpinfo``. Traditional HDA systems on Linux fall back to
    walking up from sound cards, ranked so the analog codec's controller wins
    over a GPU HDMI function. On AMD
    dual-controller laptops card0 is the GPU audio function with its own PCI
    subsystem id. Issue #33 reported 17AA:3823 where the analog controller was
    a different device, and the analog controller's id is the one kernel
    quirks and Dolby PCI-keyed filenames use.

    ``sdw_bus=None`` means the module's own ``SDW_BUS``, read in the body
    rather than defaulted in the signature, the same way ``get_soundwire_ids``
    above reads it. A caller overriding the root still passes it outright.
    """
    if sdw_bus is None:
        sdw_bus = SDW_BUS
    sdw_bus = host.path(sdw_bus)
    sound_class, proc_asound = host.path(sound_class), host.path(proc_asound)
    if _is_freebsd() and not sound_class.is_dir():
        return _freebsd_pci_subsystem()
    if sdw_bus.is_dir():
        for dev_dir in sorted(sdw_bus.iterdir()):
            result = _walk_to_pci_subsys(dev_dir)
            if result:
                return result

    if not sound_class.is_dir():
        return None
    cards = [c for c in sorted(sound_class.glob("card*")) if (c / "device").exists()]
    cards.sort(key=lambda c: _card_pci_preference(c.name, proc_asound))
    for card_dir in cards:
        result = _walk_to_pci_subsys(card_dir / "device")
        if result:
            return result
    return None
