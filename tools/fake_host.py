#!/usr/bin/env python3
"""Write a fake host tree that makes a run see another device's audio codec.

A standalone tuning such as Sonitude's picks its preset by this machine's codec
and subsystem IDs and by whether it runs Intel's audio DSP driver (SOF). To
build a reporter's preset on a development machine, point `ATMOS_HOST_ROOT` at
the tree this writes: `lib/host.py` then reads `/proc`, `/sys` and `/etc` under
it, and the tree holds only the codec and the card list. The run sees no other
hardware, no DMI and no distro, so pass `--skip-ee-check` along with it.

    python3 tools/fake_host.py 0x10EC0285:0x103C860F --sof
    ATMOS_HOST_ROOT=… python3 dolby_to_easyeffects.py DOWNLOAD --skip-ee-check \\
        --output-dir DIR --irs-dir DIR

The IDs are taken as `--speaker-info` prints them on the reporter's device.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib import host  # noqa: E402

# The card lines `lib/hardware/speakers.py` `_card_uses_sof` reads the driver
# from, in the shape /proc/asound/cards prints them.
_CARDS = {
    True: " 0 [sofhdadsp      ]: sof-hda-dsp - sof-hda-dsp\n",
    False: " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n",
}


def parse_ids(text: str) -> tuple[str, str]:
    """``CODEC:SUBSYS``, 8 hex digits each with an optional 0x, as
    ``("10EC0285", "103C860F")``."""
    parts = [p.strip().upper().removeprefix("0X") for p in text.split(":")]
    if len(parts) != 2 or not all(
            len(p) == 8 and all(c in "0123456789ABCDEF" for c in p)
            for p in parts):
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected CODEC:SUBSYS, 8 hex digits each, such as "
            "0x10EC0285:0x103C860F")
    return parts[0], parts[1]


def write(root: Path, codec: str, subsys: str, sof: bool) -> Path:
    """Write the codec and card list under *root*; return *root*.

    Codec 0 on card 0, in the lines `lib/hardware/codecs.py`
    `get_hda_codec_ids` reads.
    """
    root = Path(root)
    codec_file = root / "proc/asound/card0/codec#0"
    codec_file.parent.mkdir(parents=True, exist_ok=True)
    codec_file.write_text(f"Codec: fake codec {codec}\n"
                          f"Vendor Id: 0x{codec.lower()}\n"
                          f"Subsystem Id: 0x{subsys.lower()}\n")
    (root / "proc/asound/cards").write_text(_CARDS[sof])
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ids", type=parse_ids, metavar="CODEC:SUBSYS",
                        help="the audio codec and subsystem ID, as "
                             "--speaker-info prints them")
    parser.add_argument("--sof", action=argparse.BooleanOptionalAction,
                        required=True,
                        help="whether the device runs Intel's audio DSP "
                             "driver: --speaker-info says so")
    parser.add_argument("--root", type=Path, default=None,
                        help="where to write the tree (default: "
                             "localresearch/fake-host/CODEC_SUBSYS[_sof])")
    args = parser.parse_args(argv)
    codec, subsys = args.ids
    root = args.root or (REPO_ROOT / "localresearch" / "fake-host"
                         / f"{codec}_{subsys}{'_sof' if args.sof else ''}")
    write(root, codec, subsys, args.sof)
    print(f"{host.HOST_ROOT}={shlex.quote(str(root.resolve()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
