#!/usr/bin/env python3
"""Regenerate ``_SPEAKER_ROUTE_QUIRKS`` from the upstream kernel quirk table.

That table (in ``lib/data/speaker_route_quirks.py``) covers the fault next door
to the hidden woofer pin: the pin is present and correctly declared, but the
codec routes it to a widget with no volume amplifier, so that speaker plays at
a fixed level while everything else follows the volume. Nothing in the DAX XML
can tell, and nothing on the machine looks wrong. The kernel fixes it per
machine with an ``snd_hda_override_conn_list`` or a ``preferred_dacs`` pair,
keyed by subsystem id — so the only way to know a *given* machine should have
its pin rerouted is to carry upstream's list.

Which machines are listed is rebuilt wholesale each run, unlike the append-only
kernel-release table: entries disappear upstream (renamed fixups, merged SKUs)
and a stale entry would tell a user to apply a quirk their kernel no longer has.

Each entry also records which released series first carried the quirk, and an
entry that exists only in mainline records nothing: it must not produce
"upgrade your kernel" advice, there being no kernel to upgrade to. That flag
flips on its own as releases ship, so nothing downstream may assume a given
entry's value.

Unlike the rest of the row, that one field is *carried forward* rather than
rebuilt: what a released kernel contains cannot change, so re-deriving it each
week only risks losing it. ``--rescan`` re-derives anyway, for an audit after a
parser change.

Each entry also records the upstream commit that last wrote its line, so the
warning can link the fix it claims exists rather than the whole driver.
``--blame`` resolves it and a token buys it; it is carried forward on the same
terms as ``since``.

The upstream reading — the source fetch, the fixup-block and quirk-entry
regexes, the release walk, the blame — is the speaker-*pin* updater's, imported
wholesale so the two tables cannot come to disagree about what upstream's table
says. Only what makes a fixup qualify differs, and that is the whole of the
interesting part below.

    python3 tools/update_speaker_route_quirks.py            # report, change nothing
    python3 tools/update_speaker_route_quirks.py --write    # apply
    GH_TOKEN=$(gh auth token) \\
        python3 tools/update_speaker_route_quirks.py --write --blame
"""

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_kernel_releases import fetch_tag_lines
from update_speaker_pin_quirks import (
    _CHAIN_ID_RE,
    _CHAINED_RE,
    _FIXUP_BLOCK_RE,
    _FUNC_RE,
    _line_of,
    _MODEL_NAME_RE,
    _QUIRK_RE,
    _RELEASE_WINDOW,
    blame_backend,
    fetch_master_sha,
    fetch_source,
    release_tags,
    resolve_commits,
    resolve_since,
)

DEFAULT_SCRIPT = (Path(__file__).resolve().parent.parent
                  / "lib" / "data" / "speaker_route_quirks.py")

# {helper: (pin, allowed source widgets)}. Bar: fixes a speaker-pin
# volume-path fault — the pin would otherwise take signal from a widget with
# no volume amplifier — and constrains one internal speaker pin's source.
# Each verified by reading the helper in sound/hda/codecs/realtek/.
# Unlisted = uncovered, which is the safe direction: no entry, no warning.
#
# Hand-listed rather than derived, unlike the pin table's HDA_FIXUP_PINS half:
# these helpers are C, and which pin an `snd_hda_override_conn_list(codec, ...)`
# call names, whether the widgets it allows carry an amplifier, and whether the
# helper also does something a user would lose by forcing it, are all questions
# a regex over the call site cannot answer.
_FUNC_FIXUP_ROUTES = {
    "alc285_fixup_speaker2_to_dac1":                ("0x17", ("0x02",)),
    "alc295_fixup_disable_dac3":                    ("0x17", ("0x02", "0x03")),
    "alc298_fixup_speaker_volume":                  ("0x17", ("0x0c",)),
    "alc294_fixup_bass_speaker_15":                 ("0x15", ("0x02", "0x03")),
    "alc285_fixup_thinkpad_x1_gen7":                ("0x17", ("0x02", "0x03")),
    "alc274_fixup_hp_aio_bind_dacs":                ("0x17", ("0x02", "0x03")),
    "alc287_fixup_bind_dacs":                       ("0x17", ("0x02", "0x03")),
    "alc287_fixup_legion_16iax10h_aw88399":         ("0x17", ("0x02",)),
    "alc245_fixup_hp_zbook_firefly_g12a":           ("0x17", ("0x02",)),
    "alc245_tas2781_spi_hp_fixup_muteled":          ("0x17", ("0x02",)),
    "alc245_tas2781_i2c_hp_fixup_muteled":          ("0x17", ("0x02",)),
    "alc245_tas2781_i2c_hp_fixup_muteled_inverted": ("0x17", ("0x02",)),
    # preferred_dacs, not a conn override: its own comment reads "avoid DAC
    # 0x06 for bass speaker 0x17; it has no volume control". It also pins
    # speaker 0x14 to 0x02, which has volume either way — the row models only
    # the bass-speaker half, and the runtime gate observes that pin directly.
    "alc289_fixup_asus_ga401":                      ("0x17", ("0x02",)),
    # Deliberately absent (recorded in docs/design-notes.md): the pincfg
    # writers (_FUNC_FIXUP_PINS owns those machines); alc290_fixup_mono_speakers
    # (two pins, and the fault is mono output, not a lost volume control);
    # alc_fixup_tpt470_dacs (a level regression, and upstream ships an opt-out
    # model for the very same SSIDs); alc295_fixup_asus_dacs (fixes a silent
    # headphone); alc274_fixup_bind_dacs (DAC steering "for EQ");
    # alc288_fixup_surface_swap_dacs (a plain swap).
}

# Whole-table sanity rails, for a parse that collapses outright. They are a
# backstop, not the real guard: a table this size can lose an entire fixup
# family and still sit comfortably inside them, which is why route_fixups()
# checks each hand-listed helper individually.
MIN_ENTRIES = 50
MAX_ENTRIES = 500


# --- upstream source --------------------------------------------------------

def route_fixups(src: str,
                 require_helpers: bool = False) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``{fixup name: (pin, source widgets)}`` for every rerouting fixup in *src*.

    One shape only: an ``HDA_FIXUP_FUNC`` whose helper is in
    ``_FUNC_FIXUP_ROUTES``. There is no derived half here — a reroute lives
    entirely inside the helper's C, where the pin table the sibling updater
    reads has no equivalent.

    Resolved through ``.chain_id`` for the same reason as the pin table: a
    fixup delivers its whole chain, and upstream extends a machine by wrapping
    its rerouting fixup in a new one — an amp binding, a mute LED — rather than
    editing it. Reading a fixup's own body alone loses every machine that
    reaches the reroute that way, which here is most of them.

    A chain contributes *one* route, the first found walking it, where the pin
    table unions its links: two links each overriding the same pin's source
    list would mean upstream contradicting itself about where that speaker's
    signal comes from, and picking one of the two answers would be a guess. No
    chain upstream does this today — this is a guard against the day one does,
    not a policy for it.

    ``require_helpers`` asserts every hand-listed helper is still present, and
    belongs only to the *mainline* parse. The historical release sources are
    read with it off: a helper legitimately does not exist in releases older
    than the one that introduced it, and treating that as a rename would abort
    every run.
    """
    own: dict[str, tuple[str, tuple[str, ...]]] = {}
    helpers: dict[str, str] = {}
    chain: dict[str, str] = {}
    starts = [(m.group(1), m.start()) for m in _FIXUP_BLOCK_RE.finditer(src)]
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(src)
        body = src[start:end]
        target = _CHAIN_ID_RE.search(body)
        if target and _CHAINED_RE.search(body):
            chain[name] = target.group(1)
        func = _FUNC_RE.search(body)
        if func and func.group(1) in _FUNC_FIXUP_ROUTES:
            own[name] = _FUNC_FIXUP_ROUTES[func.group(1)]
            helpers[name] = func.group(1)

    def through_chain(name: str) -> tuple[str, tuple[str, ...]] | None:
        """The route *name* delivers, taken from the first link that has one."""
        route = None
        source = ""
        walked: set[str] = set()
        while name and name not in walked:
            walked.add(name)
            if name in own:
                if route is not None:
                    raise ValueError(
                        f"a fixup chain reaches both {source} and "
                        f"{helpers[name]} — upstream now gives one pin two "
                        "source lists. Read both helpers and record in "
                        "_FUNC_FIXUP_ROUTES which one the chain leaves in "
                        "place; guessing would report a route the machine "
                        "does not have.")
                route, source = own[name], helpers[name]
            name = chain.get(name, "")
        return route

    found = {name: route
             for name in set(own) | set(chain)
             if (route := through_chain(name))}

    # A hand-listed helper that no longer appears is the one failure the
    # whole-table size rails cannot see: renaming alc285_fixup_speaker2_to_dac1
    # upstream would silently drop every machine that reaches it while the
    # total stayed comfortably inside its bounds, and the weekly PR would look
    # clean. Fail here instead, per helper.
    # `starts` gates it so a source with no fixup table at all — a wrong blob,
    # a failed fetch — still falls through to the size rails and is reported as
    # the parse failure it is, rather than blamed on a rename.
    if (require_helpers and starts
            and (missing := sorted(set(_FUNC_FIXUP_ROUTES) - set(helpers.values())))):
        raise ValueError(
            "these fixup helpers are no longer in the kernel source: "
            + ", ".join(missing)
            + " — renamed or removed upstream. Update _FUNC_FIXUP_ROUTES "
              "(re-read each helper for its route) rather than dropping them.")
    return found


def parse_quirks(src: str, require_helpers: bool = False, lines: dict | None = None
                 ) -> dict[tuple[int, int], tuple[str, str, str, bool]]:
    """``{(subvendor, subdevice): (hda_model, pin, sources, codec_only)}`` for
    every entry in *src* pointing at a rerouting fixup.

    ``hda_model`` is the name of the entry's *own* fixup, empty when the kernel
    gives it none — which here is the usual case, most machines reaching the
    reroute through an unnamed wrapper. We deliberately never substitute the
    inner fixup's name: forcing ``alc285-speaker2-to-dac1`` on a machine whose
    entry points at a TAS2781 wrapper would reroute the pin and skip the
    amplifier setup, which is a half-fix presented as a fix.

    A duplicate key keeps the first entry, matching the kernel: the quirk table
    is walked in order and the first match wins.

    Pass a dict as *lines* to also collect ``{key: source line}`` for the entry
    that won — the same one the row describes, which is the only line whose
    blame says anything about the row.
    """
    fixups = route_fixups(src, require_helpers)
    names = dict(_MODEL_NAME_RE.findall(src))
    found: dict[tuple[int, int], tuple[str, str, str, bool]] = {}
    for m in _QUIRK_RE.finditer(src):
        kind, vendor, device, _name, fixup = m.groups()
        if fixup not in fixups:
            continue
        key = (int(vendor, 16), int(device, 16))
        if key in found:
            continue
        pin, sources = fixups[fixup]
        found[key] = (names.get(fixup, ""), pin, " ".join(sources),
                      kind == "HDA_CODEC_QUIRK")
        if lines is not None:
            lines[key] = _line_of(src, m.start())
    return found


# --- the table in lib/data/speaker_route_quirks.py --------------------------

_TABLE_RE = re.compile(r"^_SPEAKER_ROUTE_QUIRKS = \{\n(.*?)\n\}$", re.M | re.S)
# Rendered with keyword arguments: four bare fields per row, three of them
# strings of hex, are unreadable for the human who has to check an entry
# against the kernel source, and that review is the only thing standing
# between a bad parse and wrong advice.
_ENTRY_RE = re.compile(
    r'\(0x([0-9A-Fa-f]{4}), 0x([0-9A-Fa-f]{4})\): '
    r'RouteQuirk\("([^"]*)", pin="([0-9a-fx]*)", sources="([0-9a-fx ]*)", '
    r'since="([\d.]*)", codec_only=(True|False), commit="([0-9a-f]*)"\)')


def parse_table(src: str) -> tuple[tuple[int, int], dict]:
    """``(span, entries)`` for the table literal in *src*, where ``span`` is the
    offset of the dict body so a caller can splice a re-render back in."""
    m = _TABLE_RE.search(src)
    if m is None:
        raise ValueError("no _SPEAKER_ROUTE_QUIRKS literal found")
    body = m.group(1)
    entries = {
        (int(v, 16), int(d, 16)): (model, pin, sources, since,
                                   codec == "True", commit)
        for v, d, model, pin, sources, since, codec, commit
        in _ENTRY_RE.findall(body)
    }
    # Every "): RouteQuirk(" is one entry; a mismatch means the regex skipped a
    # reformatted line and the table we'd diff against would be wrong.
    if len(entries) != body.count("): RouteQuirk("):
        raise ValueError(f"parsed {len(entries)} entries but the literal has "
                         f"{body.count('): RouteQuirk(')} — refusing to edit it")
    return (m.start(1), m.end(1)), entries


def render_table(entries: dict) -> str:
    """The dict body for *entries*, one machine per line, sorted by id."""
    return "\n".join(
        f'    (0x{vendor:04X}, 0x{device:04X}): RouteQuirk("{model}", '
        f'pin="{pin}", sources="{sources}", since="{since}", '
        f'codec_only={codec_only}, commit="{commit}"),'
        for (vendor, device), (model, pin, sources, since, codec_only, commit)
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


def build_entries(master_src: str, releases, current: dict | None = None, *,
                  master_sha: str = "", blame=None, blob=None) -> dict:
    """Merge the mainline parse with per-release parses into table entries.

    *releases* yields ``(tag, source)`` newest-first and is consumed **lazily**:
    each item pulled is a blob fetched, and the walk stops as soon as every
    entry that needs an answer has one.

    *current* is the table as it ships. A ``since`` an earlier run derived is
    carried forward rather than re-derived (see ``resolve_since`` in the pin
    updater): only entries we have never dated are looked up — ones recorded
    empty (in no release yet) and ones appearing here for the first time. Pass
    ``None`` to re-derive every value from scratch, which is what ``--rescan``
    is for.

    Carrying it is what keeps the weekly diff honest. Re-deriving reaches the
    same answer for most rows, but an entry older than the oldest release we
    reach can only be recorded as *that* release — so each time the window slid
    forward, rows would be rewritten to a newer, stricter kernel than the one
    that actually fixed them, and the genuine changes would hide among them.

    ``since`` ends up the oldest release we saw still carrying the entry — the
    kernel version a user actually has to reach. Empty means no release has it
    yet, which makes "upgrade" a dead end rather than advice.

    ``commit`` is carried the same way and resolved by *blame* when it is not
    (see ``resolve_commits`` in the pin updater). Without one, recorded links
    survive and new rows get none — the table is still correct, it just links
    the driver rather than the fix.
    """
    mainline_lines: dict[tuple[int, int], int] = {}
    mainline = parse_quirks(master_src, require_helpers=True,
                            lines=mainline_lines)
    if not MIN_ENTRIES <= len(mainline) <= MAX_ENTRIES:
        raise ValueError(f"parsed {len(mainline)} mainline entries "
                         f"(expected {MIN_ENTRIES}–{MAX_ENTRIES}) — suspect a "
                         "parse bug or a renamed fixup")

    since = resolve_since(
        mainline, releases, current, parse_quirks,
        identity=lambda row: row[3],
        recorded=lambda row: (row[3], row[4]))
    commits = resolve_commits(
        mainline, mainline_lines, current,
        content=lambda row: (row[0], row[1], row[2], row[3]),
        recorded=lambda row: (row[5], (row[0], row[1], row[2], row[4])),
        master_sha=master_sha, master_src=master_src, blame=blame, blob=blob)

    return {key: (model, pin, sources, since.get(key, ""), codec_only,
                  commits.get(key, ""))
            for key, (model, pin, sources, codec_only) in mainline.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true",
                        help="apply the rebuilt table (default: report only)")
    parser.add_argument("--blame", action="store_true",
                        help="resolve each row's upstream commit through "
                             "GitHub's blame API, so the warning can link the "
                             "fix rather than the whole driver (needs a token "
                             "in GH_TOKEN or GITHUB_TOKEN). Without it, "
                             "recorded links are carried forward and rows new "
                             "to the table get none")
    parser.add_argument("--rescan", action="store_true",
                        help="re-derive every since= and commit= from upstream "
                             "instead of carrying the recorded ones forward "
                             "(slow, and rewrites entries older than the "
                             "oldest release scanned — for an audit after a "
                             "parser change, not for the weekly run)")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                        help="file holding the table (default: the shipped "
                             "lib/data/speaker_route_quirks.py)")
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
        blame, blob = blame_backend() if args.blame else (None, None)
        src = args.script.read_text()
        _, current = parse_table(src)
        master_sha = ""
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
            # Resolved once and used for every mainline read: master moves
            # under a run, and a blame keyed by line number is only meaningful
            # against the revision that was parsed.
            master_sha = fetch_master_sha()
            print(f"mainline: {master_sha[:12]}", file=sys.stderr)
            master_src = fetch_source(master_sha)
            # A generator, not a list: build_entries stops pulling once every
            # undated entry has an answer, and a release it never pulls is a
            # blob never fetched. In a week where nothing new needs dating that
            # is the whole release walk skipped.
            releases = ((tag, fetch_source(f"refs/tags/{tag}"))
                        for tag in release_tags(tag_lines))
        entries = build_entries(master_src, releases,
                                None if args.rescan else current,
                                master_sha=master_sha, blame=blame, blob=blob)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if (unlinked := sum(1 for row in entries.values() if not row[-1])):
        print(f"{unlinked} rows without a commit link", file=sys.stderr)

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
