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

Standard library only, and no subprocesses: a handful of reads under
``/proc/asound`` and ``/sys``, so importing it costs nothing
(``tests/test_layout.py``'s ``STDLIB_ONLY``).
"""

import re
from pathlib import Path


def get_hda_codec_ids():
    """Read HDA codec names and subsystem IDs from /proc/asound.

    Returns a list of (vendor_id, subsystem_id, codec_name) tuples, e.g.
    [("10EC0287", "17AA22E6", "Realtek ALC287")].
    """
    results = []
    for codec_path in sorted(Path("/proc/asound").glob("card*/codec*")):
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
    return results


# Where the SoundWire bus lives in sysfs, and the shape of a slave device's
# name under it: "sdw:L:N:MMMM:PPPP:VV", capturing the manufacturer and part
# ids the Dolby SOUNDWIRE_MAN_*_FUNC_* filenames key on.
#
# Both names are shared rather than re-spelled at each reader. ``iterdir`` the
# bus, keep what the regex matches: that is how ``get_soundwire_ids`` below
# opens, and how ``lib.hardware.speakers._detect_soundwire_speakers``'s
# amplifier probe opens; ``get_pci_audio_subsystem``'s ``sdw_bus`` default
# takes the path on its own. Only the entry point is shared — the loop bodies
# read different things and are deliberately kept apart.
SDW_BUS = Path("/sys/bus/soundwire/devices")
SDW_SLAVE_RE = re.compile(r"sdw:\d+:\d+:([0-9a-fA-F]{4}):([0-9a-fA-F]{4}):\d+")


def get_soundwire_ids():
    """Read SoundWire device IDs from /sys/bus/soundwire/devices.

    Returns a list of (manufacturer_id, part_id) tuples as uppercase hex
    strings, e.g. [("025D", "1318")].
    """
    results = []
    if not SDW_BUS.is_dir():
        return results
    for dev_dir in sorted(SDW_BUS.iterdir()):
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
    """Rank a sound card for the PCI-subsystem probe: 0 = has a non-HDMI HDA
    codec (the analog controller quirks and Dolby SKU ids key on), 1 = no HDA
    codec info (e.g. USB), 2 = HDMI/DP codecs only (a GPU audio function whose
    PCI subsystem is the GPU's, not the machine SKU's)."""
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


def get_pci_audio_subsystem(
    sound_class=Path("/sys/class/sound"),
    proc_asound=Path("/proc/asound"),
    sdw_bus=SDW_BUS,
):
    """Get the PCI subsystem ID of the audio controller.

    Returns (subsys_vendor, subsys_device) as uppercase 4-char hex strings,
    e.g. ("17AA", "2339"), or None if not found.

    Prefers the PCI ancestor of a SoundWire device when present so we pick
    the controller that actually hosts the speaker amplifiers, rather than
    whichever /sys/class/sound card sorts first (which may be HDMI audio
    on a discrete GPU). Falls back to walking up from sound cards for
    traditional HDA systems — ranked so the analog codec's controller wins
    over a GPU HDMI function: on AMD dual-controller laptops card0 is the
    GPU audio function with its own PCI subsystem id (issue #33: 17AA:3823
    reported where the analog controller — the id kernel quirks and Dolby
    PCI-keyed filenames use — was a different device).
    """
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
