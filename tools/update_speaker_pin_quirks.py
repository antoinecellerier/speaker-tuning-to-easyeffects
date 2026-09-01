#!/usr/bin/env python3
"""Regenerate ``_SPEAKER_PIN_QUIRKS`` from the upstream kernel quirk table.

That table (in ``lib/data/speaker_pin_quirks.py``) drives the issue #53
warning: a laptop whose BIOS reports its woofer pin as unconnected exposes one
speaker pin instead of two, so the preset drives half the speakers and nothing
in the DAX XML can tell. The kernel fixes this per machine, keyed by subsystem
id — so the only way to know a *given* machine should have two pins is to
carry upstream's list.

Which machines are listed is rebuilt wholesale each run, unlike the append-only
kernel-release table: entries disappear upstream (renamed fixups, merged SKUs)
and a stale entry would tell a user to apply a quirk their kernel no longer has.

Each entry also records which released series first carried the quirk, and an
entry that exists only in mainline records nothing: it must not produce
"upgrade your kernel" advice, there being no kernel to upgrade to. That flag
flips on its own as releases ship — the issue #53 machine's own quirk
(``b70f007a9fc6``) sat mainline-only until 7.2 came out — so nothing
downstream may assume a given entry's value.

Unlike the rest of the row, that one field is *carried forward* rather than
rebuilt: what a released kernel contains cannot change, so re-deriving it each
week only risks losing it. An entry older than the oldest release we scan can
be recorded no earlier than that release, so re-deriving used to rewrite whole
blocks of the table to a stricter kernel than the one that actually fixed them
every time the scan window slid. ``--rescan`` re-derives anyway, for an audit
after a parser change.

Each entry also records the upstream commit that last wrote its line, so the
warning can link the fix it claims exists rather than the whole 9000-line
driver. That needs a blame, which only GitHub's API serves for a repository
this size — hence ``--blame`` and a token. It is carried forward on the same
terms as ``since``: blame can only move when the line moved, so a row whose
content is unchanged keeps the commit an earlier run resolved and costs
nothing.

    python3 tools/update_speaker_pin_quirks.py            # report, change nothing
    python3 tools/update_speaker_pin_quirks.py --write    # apply
    GH_TOKEN=$(gh auth token) \\
        python3 tools/update_speaker_pin_quirks.py --write --blame
"""

import argparse
import ast
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
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
    "alc285_fixup_hp_envy_x360": ("0x14", "0x17"),
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


def fetch_blob(ref: str, path: str) -> str:
    """One file's contents at *ref* from the kernel mirror.

    googlesource serves raw blobs base64-encoded via ``?format=TEXT``; the
    same host the tag sweep already uses, chosen because git.kernel.org blocks
    anonymous fetches. *ref* is a tag, a branch ref or a plain commit sha —
    the mirror serves all three. Its ``+log`` pages are 403, so history has to
    come from somewhere else (see ``github_blame``).
    """
    url = f"{MIRROR}/+/{ref}/{path}?format=TEXT"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return base64.b64decode(resp.read()).decode("utf-8", "replace")


def fetch_source(ref: str) -> str:
    """The Realtek quirk-table source at *ref*: the current path, then the
    pre-7.2 one."""
    errors = []
    for path in _SOURCE_PATHS:
        try:
            return fetch_blob(ref, path)
        except urllib.error.HTTPError as exc:
            errors.append(f"{path}: HTTP {exc.code}")
        except (urllib.error.URLError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
    raise ValueError(f"no quirk source at {ref} ({'; '.join(errors)})")


# googlesource guards its JSON against cross-origin script inclusion by
# prefixing this line, which is not JSON and has to come off before parsing.
_ANTI_XSSI = ")]}'"


def fetch_master_sha() -> str:
    """The commit ``refs/heads/master`` points at right now.

    A run resolves one revision up front and asks for everything at it: the
    branch moves several times an hour, so parsing one fetch and blaming
    another would attribute lines by their numbers in a file nobody read.
    """
    url = f"{MIRROR}/+/refs/heads/master?format=JSON"
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = resp.read().decode("utf-8", "replace")
    sha = json.loads(body.removeprefix(_ANTI_XSSI)).get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"no mainline commit sha at {url}")
    return sha


_FIXUP_BLOCK_RE = re.compile(r"^\t\[(\w+)\] = \{$", re.M)
_PINTBL_RE = re.compile(r"\{\s*(0x[0-9a-f]{2}),\s*(0x[0-9a-f]{8})\s*\}")
_FUNC_RE = re.compile(r"\.v\.func = (\w+)")
# A fixup that adds no pins itself can still deliver them by chaining to one
# that does — how upstream adds a headset or amp step to an existing speaker
# fixup without touching it (ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN_HEADSET).
# ``.chain_id`` is inert without a ``.chained``/``.chained_before`` flag, so
# both parts are required. ``chained_before`` delivers the same pins by the
# other order — __snd_hda_apply_fixup recurses into chain_id first, then
# applies the wrapper — so for "which pins end up declared" the two are one
# case.
_CHAINED_RE = re.compile(r"\.chained(?:_before)? = true\b")
_CHAIN_ID_RE = re.compile(r"\.chain_id = (\w+)")
_MODEL_NAME_RE = re.compile(r'\{\.id = (\w+), \.name = "([^"]+)"\}')


def pin_adding_fixups(src: str,
                      require_helpers: bool = False) -> dict[str, tuple[str, ...]]:
    """``{fixup name: target pin nodes}`` for every pin-adding fixup in *src*.

    Two shapes: an ``HDA_FIXUP_PINS`` table we read directly, and an
    ``HDA_FIXUP_FUNC`` whose helper is in ``_FUNC_FIXUP_PINS``.

    Both are then resolved through ``.chain_id``, because a fixup delivers its
    whole chain — ``snd_hda_apply_fixup`` walks it — and upstream extends a
    machine by wrapping its speaker fixup in a new one rather than editing it.
    Reading a fixup's own body alone loses those: 17aa:390d (Yoga Pro 7 14ASP10)
    moved to ``..._BASS_SPK_PIN_HEADSET``, which adds a headset jack and chains
    to the pin fixup, and the machine dropped out of the table while still
    getting pin 0x17 from every kernel that carries it. Each link is filtered on
    its own — a headset link's mic pins are not speaker pins and contribute
    nothing — and the surviving pins are unioned in chain order.

    ``require_helpers`` asserts every hand-listed helper is still present, and
    belongs only to the *mainline* parse. The historical release sources are
    read with it off: a helper legitimately does not exist in releases older
    than the one that introduced it (``alc285_fixup_hp_spectre_x360_df1``
    first appears in 6.15), and treating that as a rename would abort every
    run.
    """
    own: dict[str, tuple[str, ...]] = {}
    chain: dict[str, str] = {}
    seen_helpers: set[str] = set()
    starts = [(m.group(1), m.start()) for m in _FIXUP_BLOCK_RE.finditer(src)]
    for i, (name, start) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(src)
        body = src[start:end]
        target = _CHAIN_ID_RE.search(body)
        if target and _CHAINED_RE.search(body):
            chain[name] = target.group(1)
        func = _FUNC_RE.search(body)
        if func and func.group(1) in _FUNC_FIXUP_PINS:
            own[name] = _FUNC_FIXUP_PINS[func.group(1)]
            seen_helpers.add(func.group(1))
            continue
        pins = _PINTBL_RE.findall(body)
        if (pins and len(pins) <= _MAX_PINS
                and all(_SPEAKER_PINCFG.match(cfg) for _, cfg in pins)):
            own[name] = tuple(node for node, _ in pins)

    def through_chain(name: str) -> tuple[str, ...]:
        """*name*'s pins plus every pin its chain goes on to add."""
        nodes: list[str] = []
        walked: set[str] = set()
        while name and name not in walked:
            walked.add(name)
            nodes += [n for n in own.get(name, ()) if n not in nodes]
            name = chain.get(name, "")
        return tuple(nodes)

    found = {name: nodes
             for name in set(own) | set(chain)
             if (nodes := through_chain(name))}

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


def _line_of(src: str, offset: int) -> int:
    """The 1-based line *offset* falls on. Every quirk entry is a single source
    line, so this is the line a blame has to be read at."""
    return src.count("\n", 0, offset) + 1


def parse_quirks(src: str, require_helpers: bool = False, lines: dict | None = None
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

    Pass a dict as *lines* to also collect ``{key: source line}`` for the entry
    that won — the same one the row describes, which is the only line whose
    blame says anything about the row.
    """
    fixups = pin_adding_fixups(src, require_helpers)
    names = dict(_MODEL_NAME_RE.findall(src))
    found: dict[tuple[int, int], tuple[str, str, bool]] = {}
    for m in _QUIRK_RE.finditer(src):
        kind, vendor, device, _name, fixup = m.groups()
        if fixup not in fixups:
            continue
        key = (int(vendor, 16), int(device, 16))
        if key in found:
            continue
        found[key] = (names.get(fixup, ""), " ".join(fixups[fixup]),
                      kind == "HDA_CODEC_QUIRK")
        if lines is not None:
            lines[key] = _line_of(src, m.start())
    return found


def entry_lines(src: str) -> dict[tuple[int, int], int]:
    """``{(subvendor, subdevice): source line}`` for every quirk entry in *src*,
    unfiltered — first occurrence wins, as the kernel's own walk does.

    Where ``parse_quirks``' *lines* records the line of the entry that
    *qualifies*, this records where the id appears at all. That is what a
    file-move hop needs: at the far side of a move the machine's entry is
    there, but the era's fixup chain need not qualify under today's rules, and
    the only question being asked there is which line to blame.
    """
    lines: dict[tuple[int, int], int] = {}
    for m in _QUIRK_RE.finditer(src):
        key = (int(m.group(2), 16), int(m.group(3), 16))
        lines.setdefault(key, _line_of(src, m.start()))
    return lines


# How far back a walk may go looking for an undated quirk's first appearance.
# ~2 years, which comfortably covers every entry added since laptops started
# needing these fixups. An entry still present in the oldest series we reach is
# recorded as "since that one" — an understatement of its age, and a harmless
# one: it can only ever make the advice more conservative, never tell somebody
# their kernel is new enough when it isn't. It is a rail, not a budget: the
# walk stops as soon as every undated entry has an answer, which in a normal
# week is after the newest release alone.
#
# Raising it is cheap in fetches (they are lazy) but not free: the mirror
# starts returning HTTP 429 somewhere above ~30 rapid blob fetches, so a run
# that legitimately needs a deep walk sits closer to that than a normal one.
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
    r'since="([\d.]*)", codec_only=(True|False), commit="([0-9a-f]*)"\)')


def parse_table(src: str) -> tuple[tuple[int, int], dict]:
    """``(span, entries)`` for the table literal in *src*, where ``span`` is the
    offset of the dict body so a caller can splice a re-render back in."""
    m = _TABLE_RE.search(src)
    if m is None:
        raise ValueError("no _SPEAKER_PIN_QUIRKS literal found")
    body = m.group(1)
    entries = {
        (int(v, 16), int(d, 16)): (model, pins, since, codec == "True", commit)
        for v, d, model, pins, since, codec, commit in _ENTRY_RE.findall(body)
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
        f'pins="{pins}", since="{since}", codec_only={codec_only}, '
        f'commit="{commit}"),'
        for (vendor, device), (model, pins, since, codec_only, commit)
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


def resolve_since(mainline: dict, releases, current: dict | None,
                  parse, identity, recorded) -> dict[tuple[int, int], str]:
    """``{key: oldest released series known to carry that entry}``.

    *parse* turns one release's quirk source into ``{key: row}``; *identity*
    extracts from a parsed row the part a re-key must invalidate (the match
    kind); *recorded* extracts ``(since, identity)`` from a shipped table row.
    Parameterized so the speaker-route updater shares it and the carry-forward
    rules cannot drift between the two tables.

    A value *current* already records is carried forward rather than
    re-derived — what a released kernel contains is a historical fact — unless
    the entry's identity changed: one that flips from ``SND_PCI_QUIRK`` to
    ``HDA_CODEC_QUIRK`` starts reaching the machine through a different id, so
    a date recorded against the old kind describes a fix the user was never
    getting, and a release only counts as carrying an entry if it carries it
    the same way. Upstream reads the flip the same way — ``75dc2eda659f``
    re-keyed the Yoga Slim 7 14AKP10 with a ``Cc: stable`` because its
    PCI-keyed entry had been shadowed, and dead, since it landed.
    """
    since = {}
    for key, row in (current or {}).items():
        rec_since, rec_identity = recorded(row)
        if (rec_since and key in mainline
                and rec_identity == identity(mainline[key])):
            since[key] = rec_since
    undated = set(mainline) - set(since)

    # Walking newest→oldest, each release the entry survives in overwrites the
    # previous answer, so the last write is the oldest one carrying it. The
    # first release *without* it settles the entry — everything older lacks it
    # too, so there is nothing left to learn and it leaves the walk. A
    # re-appearance after a gap (an entry reverted then restored) therefore
    # reports the newer run of releases — deliberately the conservative
    # direction, and no such case exists upstream today.
    scanned = 0
    if undated:
        for tag, src in releases:
            present = parse(src)
            scanned += 1
            for key in sorted(undated):
                match = present.get(key)
                if match and identity(match) == identity(mainline[key]):
                    since[key] = tag.lstrip("v")
                else:
                    undated.discard(key)
            # Tested after the work, not before it: breaking here means the
            # next release is never asked of *releases*, so it is never
            # fetched. Testing first would spend a fetch to find out it had
            # nothing to do.
            if not undated or scanned >= _RELEASE_WINDOW:
                break
    return since


# One query returns every range in the file, so a blame costs one round trip
# per *file* however many rows need one — 1944 ranges for the 9338-line
# alc269.c, about eight seconds.
_BLAME_QUERY = (
    'query($sha:String!,$path:String!){repository(owner:"torvalds",'
    'name:"linux"){object(expression:$sha){... on Commit{blame(path:$path)'
    '{ranges{startingLine endingLine commit{oid}}}}}}}')
_BLAME_RETRY_WAIT = 5


def github_blame(token: str):
    """A ``(sha, path) -> {line: commit oid}`` reader over GitHub's GraphQL API.

    The one backend available, for CI and for a local run alike: the
    googlesource mirror serves blobs but 403s every history page, and a blame
    of mainline the other way means cloning it. All the network lives in here
    so the callers can be driven by a plain callable returning a dict.

    Retried once, because the alternative to a five-second wait is a weekly run
    that drops every commit link on one 502.
    """
    def blame(sha: str, path: str) -> dict[int, str]:
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": _BLAME_QUERY,
                             "variables": {"sha": sha, "path": path}}).encode(),
            headers={"Authorization": f"bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "atmos-speaker-quirks"})
        for retries_left in (1, 0):
            try:
                with urllib.request.urlopen(request, timeout=120) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code < 500 or not retries_left:
                    raise ValueError(f"blame of {path}: HTTP {exc.code}") from exc
            except OSError as exc:      # URLError, and the read timing out
                if not retries_left:
                    raise ValueError(f"blame of {path}: {exc}") from exc
            time.sleep(_BLAME_RETRY_WAIT)

        # A GraphQL error is an HTTP 200, and so is a sha the mirror has not
        # yet fetched — both come back as an absent `object`.
        if payload.get("errors"):
            raise ValueError("blame of {}: {}".format(path, "; ".join(
                str(e.get("message", e)) for e in payload["errors"])))
        obj = ((payload.get("data") or {}).get("repository") or {}).get("object")
        ranges = ((obj or {}).get("blame") or {}).get("ranges")
        if ranges is None:
            raise ValueError(f"blame of {path}: GitHub knows no commit "
                             f"{sha[:12]}")
        return {line: r["commit"]["oid"]
                for r in ranges
                for line in range(r["startingLine"], r["endingLine"] + 1)}
    return blame


# Blame follows a rename but not a split, and the Realtek driver has had both.
# `aeeb85f26c3b` ("ALSA: hda: Split Realtek HD-audio codec driver", 2025-07-11)
# carved alc269.c out of sound/hda/codecs/realtek.c, and 1003 of mainline's
# 1251 quirk lines blame to it — the whole pre-split table, attributed to the
# split. Its parent `6014e9021b28` ("ALSA: hda: Move codec drivers into
# sound/hda/codecs directory") is a pure rename of sound/pci/hda/patch_realtek.c
# and blame *does* follow that, so blaming realtek.c there resolves all 1360 of
# its quirk lines to real authors (662 distinct, the largest a 2011 bulk
# rewrite owning 52 lines).
#
# Hence the rail: a commit owning more than a hundred quirk lines is not an
# author, it is a move blame stopped at, and recording it would tell every one
# of those machines the fix they need is a refactor. Refused, and reported, so
# the next move upstream makes is a warning to add a hop for rather than a
# table of wrong links.
_FILE_MOVES = {"aeeb85f26c3bbef6f702ac20167c45812251501d":
               ("6014e9021b28e634935c776c0271b5cbcabdc5d6",
                "sound/hda/codecs/realtek.c")}
_MAX_LINES_PER_COMMIT = 100


def _mass_owners(src: str, owners: dict[int, str], path: str) -> set[str]:
    """The commits in *owners* that own too much of *src* to be authors.

    Counted over every quirk line in the blob, not only the rows that reach
    the table: a move is visible in the whole table's blame long before it is
    visible in ours, and the count is what makes the warning actionable.
    """
    counts = Counter(
        oid for m in _QUIRK_RE.finditer(src)
        if (oid := owners.get(_line_of(src, m.start()))))
    refused = {oid: n for oid, n in counts.items()
               if n > _MAX_LINES_PER_COMMIT and oid not in _FILE_MOVES}
    if refused:
        print("warning: " + "; ".join(
            f"{oid[:12]} owns {n} quirk lines of {path}" for oid, n in
            sorted(refused.items(), key=lambda kv: -kv[1]))
            + f" — more than {_MAX_LINES_PER_COMMIT}, so that is a file move "
              "blame stopped at rather than an author. Add it to _FILE_MOVES "
              "with the parent and path to hop to; those rows keep no commit "
              "link meanwhile.", file=sys.stderr)
    return set(refused)


def resolve_commits(mainline: dict, mainline_lines: dict, current: dict | None,
                    content, recorded, master_sha: str, master_src: str,
                    blame, blob) -> dict[tuple[int, int], str]:
    """``{key: 12-hex commit that last wrote that entry's line upstream}``.

    *content* extracts from a parsed mainline row everything but ``since`` and
    ``commit``; *recorded* extracts ``(commit, content)`` from a shipped table
    row. Parameterized like ``resolve_since`` above so the speaker-route
    updater shares it and the carry-forward rules cannot drift.

    Carried forward on the same terms, for a different reason: blame can only
    move when the line moved, and a row whose content is unchanged is the same
    line. Rows that changed, rows recorded empty and rows new to the table are
    the only ones resolved — which in a normal week is none of them, and then
    nothing is fetched at all.

    A blame that fails is not allowed to hold back a table update: the rows it
    covered keep no link, one warning says so, and the run continues. Who gets
    warned about their machine does not depend on being able to link the
    commit that fixed it. *blame* and *blob* left None (no ``--blame``) is the
    same outcome reached without asking — both are required, because the hop
    below reads a blob as well as a blame.
    """
    commits = {}
    for key, row in (current or {}).items():
        rec_commit, rec_content = recorded(row)
        if (rec_commit and key in mainline
                and rec_content == content(mainline[key])):
            commits[key] = rec_commit
    unresolved = sorted(set(mainline) - set(commits))
    if not unresolved or blame is None or blob is None:
        return commits

    try:
        owners = blame(master_sha, _SOURCE_PATHS[0])
    except (ValueError, OSError) as exc:
        print(f"warning: {exc} — those rows keep no commit link",
              file=sys.stderr)
        return commits
    refused = _mass_owners(master_src, owners, _SOURCE_PATHS[0])

    # Rows the split swallowed are batched per move, so the hop costs one blob
    # and one blame however many of them there are.
    hops: dict[str, list] = {}
    for key in unresolved:
        oid = owners.get(mainline_lines.get(key, 0), "")
        if not oid or oid in refused:
            continue
        if oid in _FILE_MOVES:
            hops.setdefault(oid, []).append(key)
        else:
            commits[key] = oid[:12]

    for oid, keys in hops.items():
        parent, path = _FILE_MOVES[oid]
        try:
            hop_src = blob(parent, path)
            hop_owners = blame(parent, path)
        except (ValueError, OSError) as exc:
            print(f"warning: {exc} — the {len(keys)} rows behind "
                  f"{oid[:12]} keep no commit link", file=sys.stderr)
            continue
        hop_lines = entry_lines(hop_src)
        hop_refused = _mass_owners(hop_src, hop_owners, path)
        for key in keys:
            # A second move behind the first would need its own hop entry;
            # until someone reads that history, no link beats a wrong one.
            hop_oid = hop_owners.get(hop_lines.get(key, 0), "")
            if hop_oid and hop_oid not in hop_refused and hop_oid not in _FILE_MOVES:
                commits[key] = hop_oid[:12]
    return commits


def build_entries(master_src: str, releases, current: dict | None = None, *,
                  master_sha: str = "", blame=None, blob=None) -> dict:
    """Merge the mainline parse with per-release parses into table entries.

    *releases* yields ``(tag, source)`` newest-first and is consumed **lazily**:
    each item pulled is a blob fetched, and the walk stops as soon as every
    entry that needs an answer has one.

    *current* is the table as it ships. A ``since`` an earlier run derived is
    carried forward rather than re-derived (see ``resolve_since``): only
    entries we have never dated are looked up — ones recorded empty (in no
    release yet) and ones appearing here for the first time. Pass ``None`` to
    re-derive every value from scratch, which is what ``--rescan`` is for.

    Carrying it is what keeps the weekly diff honest. Re-deriving reaches the
    same answer for most rows, but an entry older than the oldest release we
    reach can only be recorded as *that* release — so each time the window slid
    forward, rows were rewritten to a newer, stricter kernel than the one that
    actually fixed them, and the genuine changes hid among them.

    ``since`` ends up the oldest release we saw still carrying the entry — the
    kernel version a user actually has to reach. Empty means no release has it
    yet, which makes "upgrade" a dead end rather than advice.

    ``commit`` is carried the same way and resolved by *blame* when it is not
    (see ``resolve_commits``). Without one, recorded links survive and new rows
    get none — the table is still correct, it just links the driver rather than
    the fix.
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
        identity=lambda row: row[2],
        recorded=lambda row: (row[2], row[3]))
    commits = resolve_commits(
        mainline, mainline_lines, current,
        content=lambda row: (row[0], row[1], row[2]),
        recorded=lambda row: (row[4], (row[0], row[1], row[3])),
        master_sha=master_sha, master_src=master_src, blame=blame, blob=blob)

    return {key: (model, pins, since.get(key, ""), codec_only,
                  commits.get(key, ""))
            for key, (model, pins, codec_only) in mainline.items()}


def blame_backend():
    """``(blame, blob)`` for ``--blame``, or a ValueError naming the token it
    needs. Shared with the speaker-route updater, which offers the same flag."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "--blame needs a GitHub token in GH_TOKEN or GITHUB_TOKEN — "
            "`GH_TOKEN=$(gh auth token)` locally, the workflow's own token in "
            "CI. Blame is served by no other host we can reach")
    return github_blame(token), fetch_blob


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
