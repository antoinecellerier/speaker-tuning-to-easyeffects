#!/usr/bin/env python3
"""Regenerate ``_SPEAKER_PIN_QUIRKS`` from the upstream kernel quirk table.

That table (in ``lib/data/speaker_pin_quirks.py``) drives the issue #53
warning: a laptop whose BIOS reports its woofer pin as unconnected exposes one
speaker pin instead of two, so the preset drives half the speakers and nothing
in the DAX XML can tell. The kernel fixes this per machine, keyed by subsystem
id — so the only way to know a *given* machine should have two pins is to
carry upstream's list.

Unlike the kernel-release table, this one is rebuilt wholesale each run:
entries disappear upstream (renamed fixups, merged SKUs) and a stale entry
would tell a user to apply a quirk their kernel no longer has.

Each entry also records which released series first carried the quirk, and an
entry that exists only in mainline records nothing: it must not produce
"upgrade your kernel" advice, there being no kernel to upgrade to. That flag
flips on its own as releases ship — the issue #53 machine's own quirk
(``b70f007a9fc6``) sat mainline-only until 7.2 came out — so nothing
downstream may assume a given entry's value.

    python3 tools/update_speaker_pin_quirks.py            # report, change nothing
    python3 tools/update_speaker_pin_quirks.py --write    # apply
"""

import argparse
import ast
import base64
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_kernel_releases import MIRROR, fetch_tag_lines, upstream_series

DEFAULT_SCRIPT = (Path(__file__).resolve().parent.parent
                  / "lib" / "data" / "speaker_pin_quirks.py")

# Which fixups count is *derived*, not listed: any whose whole effect is to
# declare one or two pins as internal speakers (default config 0x9017xxxx).
# That is precisely the failure this warns about — a driver the firmware hid.
# Fixups that rewrite a machine's entire pin map are excluded by the "all the
# pins it touches are speakers, and there are at most two" test: those
# rearrange working hardware, where "the quirk isn't applied" implies nothing
# about any particular pin.
_MAX_PINS = 2
_SPEAKER_PINCFG = re.compile(r"^0x9017[0-9a-f]{4}$")

# HDA_FIXUP_FUNC fixups run C we can't read, so their target pins are listed
# here — each verified by reading the helper in sound/hda/codecs/realtek/.
# A helper that isn't listed is simply not covered, which is the safe
# direction: no entry, no warning.
_FUNC_FIXUP_PINS = {
    "alc287_fixup_yoga9_14iap7_bass_spk_pin": ("0x17",),
    "alc285_fixup_hp_spectre_x360": ("0x14",),
    "alc285_fixup_hp_spectre_x360_eb1": ("0x14", "0x17"),
    "alc285_fixup_hp_spectre_x360_df1": ("0x14", "0x17"),
    "alc245_fixup_hp_spectre_x360_eu0xxx": ("0x14", "0x17"),
    "alc245_fixup_hp_spectre_x360_16_aa0xxx": ("0x14", "0x17"),
    "alc295_fixup_dell_inspiron_top_speakers": ("0x14", "0x17"),
}

# The Realtek quirk table moved in 7.2; older refs still need the old path.
_SOURCE_PATHS = (
    "sound/hda/codecs/realtek/alc269.c",
    "sound/pci/hda/patch_realtek.c",
)

# Whole-table sanity rails, for a parse that collapses outright. They are a
# backstop, not the real guard: a table this size can lose an entire fixup
# family and still sit comfortably inside them, which is why
# pin_adding_fixups() checks each hand-listed helper individually.
MIN_ENTRIES = 30
MAX_ENTRIES = 300


# --- upstream source --------------------------------------------------------

# HDA_CODEC_QUIRK matches the codec's own subsystem id; SND_PCI_QUIRK matches
# the PCI subsystem id (see snd_hda_pick_fixup, sound/hda/common/auto_parser.c).
# The distinction is load-bearing on SOF machines, where the PCI subsystem id
# the kernel sees is zeroed and only codec-SSID matching can fire.
_QUIRK_RE = re.compile(
    r"\b(HDA_CODEC_QUIRK|SND_PCI_QUIRK)\(\s*"
    r"0x([0-9a-fA-F]{4}),\s*0x([0-9a-fA-F]{4}),\s*"
    r'"([^"]*)",\s*(\w+)\s*\)')


def fetch_source(ref: str) -> str:
    """The Realtek quirk-table source at *ref* from the kernel mirror.

    googlesource serves raw blobs base64-encoded via ``?format=TEXT``; the
    same host the tag sweep already uses, chosen because git.kernel.org blocks
    anonymous fetches. Tries the current path, then the pre-7.2 one.
    """
    errors = []
    for path in _SOURCE_PATHS:
        url = f"{MIRROR}/+/{ref}/{path}?format=TEXT"
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return base64.b64decode(resp.read()).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            errors.append(f"{path}: HTTP {exc.code}")
        except (urllib.error.URLError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    raise ValueError(f"no quirk source at {ref} ({'; '.join(errors)})")


_FIXUP_BLOCK_RE = re.compile(r"^\t\[(\w+)\] = \{$", re.M)
_PINTBL_RE = re.compile(r"\{\s*(0x[0-9a-f]{2}),\s*(0x[0-9a-f]{8})\s*\}")
_FUNC_RE = re.compile(r"\.v\.func = (\w+)")
_MODEL_NAME_RE = re.compile(r'\{\.id = (\w+), \.name = "([^"]+)"\}')


def pin_adding_fixups(src: str,
                      require_helpers: bool = False) -> dict[str, tuple[str, ...]]:
    """``{fixup name: target pin nodes}`` for every pin-adding fixup in *src*.

    Two shapes: an ``HDA_FIXUP_PINS`` table we read directly, and an
    ``HDA_FIXUP_FUNC`` whose helper is in ``_FUNC_FIXUP_PINS``.

    ``require_helpers`` asserts every hand-listed helper is still present, and
    belongs only to the *mainline* parse. The historical release sources are
    read with it off: a helper legitimately does not exist in releases older
    than the one that introduced it (``alc285_fixup_hp_spectre_x360_df1``
    first appears in 6.15), and treating that as a rename would abort every
    run.
    """
    found: dict[str, tuple[str, ...]] = {}
    seen_helpers: set[str] = set()
    starts = [(m.group(1), m.start()) for m in _FIXUP_BLOCK_RE.finditer(src)]
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(src)
        body = src[start:end]
        func = _FUNC_RE.search(body)
        if func and func.group(1) in _FUNC_FIXUP_PINS:
            found[name] = _FUNC_FIXUP_PINS[func.group(1)]
            seen_helpers.add(func.group(1))
            continue
        pins = _PINTBL_RE.findall(body)
        if (pins and len(pins) <= _MAX_PINS
                and all(_SPEAKER_PINCFG.match(cfg) for _, cfg in pins)):
            found[name] = tuple(node for node, _ in pins)

    # A hand-listed helper that no longer appears is the one failure the
    # whole-table size rails cannot see: renaming
    # alc287_fixup_yoga9_14iap7_bass_spk_pin upstream would silently drop every
    # Lenovo Yoga row while the total stayed comfortably inside its bounds, and
    # the weekly PR would look clean. Fail here instead, per helper.
    # `starts` gates it so a source with no fixup table at all — a wrong blob,
    # a failed fetch — still falls through to the size rails and is reported as
    # the parse failure it is, rather than blamed on a rename.
    if (require_helpers and starts
            and (missing := sorted(set(_FUNC_FIXUP_PINS) - seen_helpers))):
        raise ValueError(
            "these fixup helpers are no longer in the kernel source: "
            + ", ".join(missing)
            + " — renamed or removed upstream. Update _FUNC_FIXUP_PINS "
              "(re-read each helper for its pins) rather than dropping them.")
    return found


def parse_quirks(src: str, require_helpers: bool = False
                 ) -> dict[tuple[int, int], tuple[str, str, bool]]:
    """``{(subvendor, subdevice): (hda_model, pins, codec_only)}`` for every
    entry in *src* pointing at a pin-adding fixup.

    ``hda_model`` is empty when the fixup has no name in the kernel's models
    table, i.e. the user cannot force it by hand and a kernel upgrade is the
    only route. We deliberately never substitute a *related* fixup's name: on
    the IMH9 machines that would set the pin and skip the amplifier setup its
    chain also performs, which is a half-fix presented as a fix.

    A duplicate key keeps the first entry, matching the kernel: the quirk table
    is walked in order and the first match wins.
    """
    fixups = pin_adding_fixups(src, require_helpers)
    names = dict(_MODEL_NAME_RE.findall(src))
    found: dict[tuple[int, int], tuple[str, str, bool]] = {}
    for kind, vendor, device, _name, fixup in _QUIRK_RE.findall(src):
        if fixup not in fixups:
            continue
        key = (int(vendor, 16), int(device, 16))
        if key in found:
            continue
        found[key] = (names.get(fixup, ""), " ".join(fixups[fixup]),
                      kind == "HDA_CODEC_QUIRK")
    return found


# How many released series back to look for each quirk's first appearance.
# ~2 years, which comfortably covers every entry added since laptops started
# needing these fixups. An entry already present in the oldest series we look
# at is recorded as "since that one" — an understatement of its age, and a
# harmless one: it can only ever make the advice more conservative, never tell
# somebody their kernel is new enough when it isn't.
_RELEASE_WINDOW = 12


def release_tags(tag_lines: str) -> list[str]:
    """The newest ``_RELEASE_WINDOW`` mainline ``vX.Y`` tags, newest first.

    Release candidates are excluded by ``upstream_series``' tag pattern, so a
    quirk merged into an -rc is correctly reported as not yet released.
    """
    series = upstream_series(tag_lines)
    if not series:
        raise ValueError("no mainline release tags found")
    return [f"v{major}.{minor}"
            for major, minor in sorted(series, reverse=True)[:_RELEASE_WINDOW]]


# --- the table in lib/data/speaker_pin_quirks.py ----------------------------

_TABLE_RE = re.compile(r"^_SPEAKER_PIN_QUIRKS = \{\n(.*?)\n\}$", re.M | re.S)
# Rendered with keyword arguments: three bare booleans per row are unreadable
# for the human who has to check an entry against the kernel source, and that
# review is the only thing standing between a bad parse and wrong advice.
_ENTRY_RE = re.compile(
    r'\(0x([0-9A-Fa-f]{4}), 0x([0-9A-Fa-f]{4})\): '
    r'PinQuirk\("([^"]*)", pins="([0-9a-fx ]*)", '
    r'since="([\d.]*)", codec_only=(True|False)\)')


def parse_table(src: str) -> tuple[tuple[int, int], dict]:
    """``(span, entries)`` for the table literal in *src*, where ``span`` is the
    offset of the dict body so a caller can splice a re-render back in."""
    m = _TABLE_RE.search(src)
    if m is None:
        raise ValueError("no _SPEAKER_PIN_QUIRKS literal found")
    body = m.group(1)
    entries = {
        (int(v, 16), int(d, 16)): (model, pins, since, codec == "True")
        for v, d, model, pins, since, codec in _ENTRY_RE.findall(body)
    }
    # Every "): PinQuirk(" is one entry; a mismatch means the regex skipped a
    # reformatted line and the table we'd diff against would be wrong.
    if len(entries) != body.count("): PinQuirk("):
        raise ValueError(f"parsed {len(entries)} entries but the literal has "
                         f"{body.count('): PinQuirk(')} — refusing to edit it")
    return (m.start(1), m.end(1)), entries


def render_table(entries: dict) -> str:
    """The dict body for *entries*, one machine per line, sorted by id."""
    return "\n".join(
        f'    (0x{vendor:04X}, 0x{device:04X}): PinQuirk("{model}", '
        f'pins="{pins}", since="{since}", codec_only={codec_only}),'
        for (vendor, device), (model, pins, since, codec_only)
        in sorted(entries.items()))


def apply_update(src: str, entries: dict) -> str:
    """*src* with *entries* spliced into the table literal, verified to parse
    as Python and to round-trip — a wrong table here silently changes who gets
    told to reconfigure their kernel."""
    (start, end), _ = parse_table(src)
    updated = src[:start] + render_table(entries) + src[end:]
    ast.parse(updated)
    if parse_table(updated)[1] != entries:
        raise ValueError("rewritten table does not round-trip")
    return updated


def build_entries(master_src: str, releases: list[tuple[str, str]]) -> dict:
    """Merge the mainline parse with per-release parses into table entries.

    *releases* is ``(tag, source)`` newest-first. ``since`` becomes the oldest
    release in that window still carrying the entry — the kernel version a
    user actually has to reach. Empty means no release has it yet, which makes
    "upgrade" a dead end rather than advice.
    """
    mainline = parse_quirks(master_src, require_helpers=True)
    if not MIN_ENTRIES <= len(mainline) <= MAX_ENTRIES:
        raise ValueError(f"parsed {len(mainline)} mainline entries "
                         f"(expected {MIN_ENTRIES}–{MAX_ENTRIES}) — suspect a "
                         "parse bug or a renamed fixup")

    since: dict[tuple[int, int], str] = {}
    for tag, src in releases:
        present = parse_quirks(src)
        # Walking newest→oldest, each release the entry survives in overwrites
        # the previous answer, so the last write is the oldest one carrying it.
        # A re-appearance after a gap (an entry reverted then restored) would
        # therefore report the older window — deliberately the conservative
        # direction, and no such case exists upstream today.
        for key in present:
            if key in mainline:
                since[key] = tag.lstrip("v")

    return {key: (model, pins, since.get(key, ""), codec_only)
            for key, (model, pins, codec_only) in mainline.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true",
                        help="apply the rebuilt table (default: report only)")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                        help="file holding the table (default: the shipped "
                             "lib/data/speaker_pin_quirks.py)")
    parser.add_argument("--offline-master", type=Path, metavar="FILE",
                        help="read mainline quirk source from FILE instead of "
                             "fetching (for tests and reruns)")
    parser.add_argument("--offline-release", type=Path, metavar="FILE",
                        help="read one release's quirk source from FILE")
    parser.add_argument("--offline-release-tag", default="v0.0", metavar="TAG",
                        help="version to attribute --offline-release to")
    parser.add_argument("--cache-dir", type=Path,
                        help="git dir for the tag fetch (default: a temp dir)")
    args = parser.parse_args(argv)

    try:
        src = args.script.read_text()
        _, current = parse_table(src)
        if args.offline_master and args.offline_release:
            master_src = args.offline_master.read_text()
            releases = [(args.offline_release_tag,
                         args.offline_release.read_text())]
        else:
            if args.cache_dir:
                tag_lines = fetch_tag_lines(args.cache_dir)
            else:
                with tempfile.TemporaryDirectory() as tmp:
                    tag_lines = fetch_tag_lines(Path(tmp) / "linux-tags")
            master_src = fetch_source("refs/heads/master")
            releases = [(tag, fetch_source(f"refs/tags/{tag}"))
                        for tag in release_tags(tag_lines)]
        entries = build_entries(master_src, releases)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if entries == current:
        print(f"up to date: {len(current)} machines listed", file=sys.stderr)
        return 0

    if args.write:
        try:
            args.script.write_text(apply_update(src, entries))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # stdout is the machine-readable part: one "<+|-|~> <vendor>:<device>" per
    # changed machine, for the workflow to name the branch and PR from.
    for key in sorted(set(current) | set(entries)):
        if current.get(key) == entries.get(key):
            continue
        mark = "+" if key not in current else "-" if key not in entries else "~"
        print(f"{mark} {key[0]:04x}:{key[1]:04x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
