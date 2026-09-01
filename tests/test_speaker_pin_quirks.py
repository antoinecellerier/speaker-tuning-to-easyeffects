"""The speaker-pin quirk-table updater edits shipped source unattended.

A wrong entry here is worse than invisible: it tells a user to reconfigure
their kernel for a fixup that doesn't apply to their machine. So the tests lock
that re-rendering the *current* table reproduces the file byte-for-byte, that
the quirk parse matches what upstream's table actually says, and that every
implausible input is refused rather than written.

All offline: kernel source comes from fixture strings, never the network.
"""

import ast
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from tools import update_speaker_pin_quirks as pin_updater
from tools.update_speaker_pin_quirks import (
    MAX_ENTRIES,
    MIN_ENTRIES,
    apply_update,
    build_entries,
    entry_lines,
    fetch_master_sha,
    github_blame,
    main,
    parse_quirks,
    parse_table,
    release_tags,
    render_table,
)
from tools.update_speaker_pin_quirks import (
    _FILE_MOVES,
    _FUNC_FIXUP_PINS,
    _MAX_LINES_PER_COMMIT,
    _RELEASE_WINDOW,
    _SOURCE_PATHS,
)

# Every helper named in _FUNC_FIXUP_PINS must appear somewhere in the source or
# the generator aborts — a rename upstream is exactly the silent-drop failure
# that guard exists for. The fixture carries a stub for each helper it does not
# otherwise exercise, read from the real mapping so adding one can't be missed.
HELPER_STUBS = "".join(
    f"\t[STUB_{i}] = {{\n\t\t.type = HDA_FIXUP_FUNC,\n"
    f"\t\t.v.func = {helper},\n\t}},\n"
    for i, helper in enumerate(sorted(_FUNC_FIXUP_PINS))
    if helper != "alc287_fixup_yoga9_14iap7_bass_spk_pin")

ROOT = Path(__file__).resolve().parent.parent
TABLE_MODULE = ROOT / "lib" / "data" / "speaker_pin_quirks.py"


# Excerpted from sound/hda/codecs/realtek/alc269.c. The fixup *definitions*
# come first because which fixups qualify is derived from them, not from a
# hand-kept list: a pin table is pin-adding when every pin it touches is an
# internal speaker (0x9017xxxx) and there are at most two.
#
# ALC290_FIXUP_SUBWOOFER is the HDA_FIXUP_PINS shape (read directly);
# the two BASS_SPK_PIN fixups are the HDA_FIXUP_FUNC shape (their helper is
# listed in _FUNC_FIXUP_PINS); TAS2781_I2C and YOGA7_14ITL_SPEAKERS are the
# neighbours that must be ignored.
FIXUP_DEFS = """\
\t[ALC290_FIXUP_SUBWOOFER] = {
\t\t.type = HDA_FIXUP_PINS,
\t\t.v.pins = (const struct hda_pintbl[]) {
\t\t\t{ 0x17, 0x90170112 }, /* subwoofer */
\t\t\t{ }
\t\t},
\t},
\t[ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc287_fixup_yoga9_14iap7_bass_spk_pin,
\t\t.chained = true,
\t\t.chain_id = ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK,
\t},
\t[ALC287_FIXUP_YOGA9_14IMH9_BASS_SPK_PIN] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc287_fixup_yoga9_14iap7_bass_spk_pin,
\t\t.chained = true,
\t\t.chain_id = ALC287_FIXUP_CS35L41_I2C_2,
\t},
\t[ALC287_FIXUP_TAS2781_I2C] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc287_fixup_bind_dacs,
\t},
\t[ALC287_FIXUP_YOGA7_14ITL_SPEAKERS] = {
\t\t.type = HDA_FIXUP_PINS,
\t\t.v.pins = (const struct hda_pintbl[]) {
\t\t\t{ 0x14, 0x90170110 },
\t\t\t{ 0x19, 0x03a11030 },
\t\t\t{ 0x21, 0x03211020 },
\t\t\t{ }
\t\t},
\t},
""" + HELPER_STUBS


# Only ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN has a name a user can force;
# the IMH9 variant deliberately has none (see the test below).
MODELS = ('\t{.id = ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN, '
          '.name = "alc287-yoga9-bass-spk-pin"},\n')

# The quirk table itself, kept above MIN_ENTRIES so the plausibility guard
# doesn't fire on the fixture. One entry of each match shape: HDA_CODEC_QUIRK
# (matched on the codec's own subsystem id) and SND_PCI_QUIRK.
QUIRK_TABLE = """\
\tSND_PCI_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x3802, "Lenovo Yoga Pro 9 14IRP8", ALC287_FIXUP_TAS2781_I2C),
\tHDA_CODEC_QUIRK(0x17aa, 0x386a, "Lenovo Yoga 7 16IAP7", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x38d2, "Lenovo Yoga 9 14IMH9", ALC287_FIXUP_YOGA9_14IMH9_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x384a, "Lenovo Yoga 7 15ITL5", ALC287_FIXUP_YOGA7_14ITL_SPEAKERS),
\tSND_PCI_QUIRK(0x17aa, 0x3869, "Lenovo Yoga7 14IAL7", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x3882, "Lenovo Yoga Pro 7 14APH8", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x3891, "Lenovo Yoga Pro 7 14AHP9", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tHDA_CODEC_QUIRK(0x17aa, 0x38b1, "Lenovo Yoga Pro 7 14IRH8", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x38d7, "Lenovo Yoga 9 14IMH9", ALC287_FIXUP_YOGA9_14IMH9_BASS_SPK_PIN),
\tHDA_CODEC_QUIRK(0x17aa, 0x391d, "Lenovo Yoga 7 2-in-1 16AKP10", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
"""

# Filler rows so the fixture clears MIN_ENTRIES. The whole-table rail exists to
# catch a parse that collapses, so a fixture below it would only be testable by
# weakening the guard the shipped table depends on.
FILLER_IDS = list(range(0x9000, 0x9000 + MIN_ENTRIES))
FILLER = "".join(
    f'\tSND_PCI_QUIRK(0x17aa, 0x{i:04x}, "filler {i:#06x}", '
    'ALC290_FIXUP_SUBWOOFER),\n' for i in FILLER_IDS)

QUIRK_SOURCE = FIXUP_DEFS + MODELS + QUIRK_TABLE + FILLER

# The nine hand-written pin-adding ids in QUIRK_SOURCE.
PIN_IDS = [0x3801, 0x3869, 0x386A, 0x3882, 0x3891, 0x38B1, 0x38D2, 0x38D7,
           0x391D]


def test_parse_quirks_keeps_only_pin_adding_fixups():
    """TAS2781_I2C sets up an amp and YOGA7_14ITL_SPEAKERS rearranges existing
    pins — neither adds one, so neither may license the warning."""
    assert sorted(parse_quirks(QUIRK_SOURCE)) == [
        (0x17AA, i) for i in sorted(PIN_IDS + FILLER_IDS)]


def test_parse_quirks_records_the_codec_only_match_key():
    """HDA_CODEC_QUIRK is matched against the codec's subsystem id only. Losing
    this would let us claim a PCI-id match the kernel never makes."""
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x17AA, 0x386A)][2] is True    # HDA_CODEC_QUIRK
    assert parsed[(0x17AA, 0x3801)][2] is False   # SND_PCI_QUIRK


def test_model_is_empty_when_the_fixup_has_no_forcible_name():
    """Only the IAP7 fixup has a name in the kernel's models table. Borrowing
    it for the IMH9 machines would set the pin and skip the amplifier setup
    their chain also performs — a half-fix presented as a fix — so those get
    no command at all."""
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x17AA, 0x386A)][0] == "alc287-yoga9-bass-spk-pin"
    assert parsed[(0x17AA, 0x38D2)][0] == ""


def test_parse_quirks_records_the_pins_the_fixup_declares():
    assert parse_quirks(QUIRK_SOURCE)[(0x17AA, 0x386A)][1] == "0x17"


def test_pin_adding_fixups_ignores_whole_machine_pin_maps():
    """A fixup rewriting a machine's entire pin map rearranges working
    hardware; "not applied" implies nothing about any one pin there."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    src = FIXUP_DEFS + """\
\t[ALC269_FIXUP_WHOLE_MACHINE] = {
\t\t.type = HDA_FIXUP_PINS,
\t\t.v.pins = (const struct hda_pintbl[]) {
\t\t\t{ 0x12, 0x90a60130 },
\t\t\t{ 0x14, 0x90170110 },
\t\t\t{ 0x21, 0x02211020 },
\t\t\t{ }
\t\t},
\t},
"""
    assert "ALC269_FIXUP_WHOLE_MACHINE" not in pin_adding_fixups(src)
    assert pin_adding_fixups(src)["ALC290_FIXUP_SUBWOOFER"] == ("0x17",)


def test_a_fixup_delivers_the_pins_its_chain_adds():
    """Upstream extends a machine by wrapping its speaker fixup, not editing
    it: 17aa:390d moved to ..._BASS_SPK_PIN_HEADSET, which adds a headset jack
    and chains to the pin fixup. Reading only a fixup's own body drops that
    machine from the table while every kernel carrying it still sets pin 0x17.
    The wrapper also has no name in the models table, so the row correctly
    loses its hda_model= — forcing the inner fixup by hand would skip the
    headset step."""
    src = FIXUP_DEFS + """\
\t[ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN_HEADSET] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN,
\t},
""" + MODELS + """\
\tSND_PCI_QUIRK(0x17aa, 0x390d, "Lenovo Yoga Pro 7 14ASP10", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN_HEADSET),
"""
    model, pins, _codec_only = parse_quirks(src)[(0x17AA, 0x390D)]
    assert pins == "0x17"
    assert model == ""


def test_a_chain_link_that_is_not_a_speaker_fixup_contributes_nothing():
    """Each link is filtered on its own terms. A headset-mic pin table is not
    a speaker pin table, so wrapping a speaker fixup in one must add 0x17 and
    nothing else — pulling in 0x19 would tell a user their mic pin is a
    missing speaker."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    src = FIXUP_DEFS + """\
\t[ALC287_FIXUP_HEADSET_THEN_BASS] = {
\t\t.type = HDA_FIXUP_PINS,
\t\t.v.pins = (const struct hda_pintbl[]) {
\t\t\t{ 0x19, 0x03a11050 },
\t\t\t{ }
\t\t},
\t\t.chained = true,
\t\t.chain_id = ALC290_FIXUP_SUBWOOFER,
\t},
"""
    assert pin_adding_fixups(src)["ALC287_FIXUP_HEADSET_THEN_BASS"] == ("0x17",)


def test_a_chained_before_wrapper_delivers_its_target_pins():
    """__snd_hda_apply_fixup recurses into chain_id *before* applying a
    `.chained_before` fixup, so the target's pins land either way. Reading only
    `.chained` would drop such a machine the moment upstream wraps a speaker
    fixup that way — the silent drop this whole rule exists to stop."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    src = FIXUP_DEFS + """\
\t[ALC287_FIXUP_BEFORE_BASS] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained_before = true,
\t\t.chain_id = ALC290_FIXUP_SUBWOOFER,
\t},
"""
    assert pin_adding_fixups(src)["ALC287_FIXUP_BEFORE_BASS"] == ("0x17",)


def test_an_inert_chain_id_is_not_followed():
    """``.chain_id`` without ``.chained = true`` is dead source the kernel
    never walks, so neither may we."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    src = FIXUP_DEFS + """\
\t[ALC287_FIXUP_NOT_CHAINED] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chain_id = ALC290_FIXUP_SUBWOOFER,
\t},
"""
    assert "ALC287_FIXUP_NOT_CHAINED" not in pin_adding_fixups(src)


def test_a_chain_that_loops_terminates():
    """A malformed source must fail the size rails, not hang the weekly run."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    src = FIXUP_DEFS + """\
\t[ALC287_FIXUP_LOOP_A] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC287_FIXUP_LOOP_B,
\t},
\t[ALC287_FIXUP_LOOP_B] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC287_FIXUP_LOOP_A,
\t},
"""
    found = pin_adding_fixups(src)
    assert "ALC287_FIXUP_LOOP_A" not in found
    assert found["ALC290_FIXUP_SUBWOOFER"] == ("0x17",)


def test_parse_quirks_first_match_wins():
    """The kernel walks its table in order, so a duplicate id must resolve the
    same way here."""
    duped = QUIRK_SOURCE + (
        '\tSND_PCI_QUIRK(0x17aa, 0x386a, "dup", '
        'ALC287_FIXUP_YOGA9_14IMH9_BASS_SPK_PIN),\n')
    assert parse_quirks(duped)[(0x17AA, 0x386A)][0] == "alc287-yoga9-bass-spk-pin"


def test_parse_quirks_ignores_an_unrelated_file():
    assert parse_quirks("static int foo(void) { return 0; }\n") == {}


# --- since: the released series a user has to reach -------------------------

def test_since_is_the_oldest_release_still_carrying_the_entry():
    older = FIXUP_DEFS + MODELS + FILLER + (
        '\tSND_PCI_QUIRK(0x17aa, 0x3801, "x", '
        'ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),\n')
    entries = build_entries(QUIRK_SOURCE, [
        ("v7.1", QUIRK_SOURCE),   # newest first
        ("v7.0", QUIRK_SOURCE),
        ("v6.19", older),         # only 3801 goes this far back
    ])
    assert entries[(0x17AA, 0x3801)][2] == "6.19"
    assert entries[(0x17AA, 0x386A)][2] == "7.0"


def test_since_is_empty_for_a_mainline_only_entry():
    """The issue #53 case: merged for 7.2, in no release yet — so telling the
    user to upgrade would send them after something that doesn't exist."""
    without = QUIRK_SOURCE.replace(
        '\tHDA_CODEC_QUIRK(0x17aa, 0x386a, "Lenovo Yoga 7 16IAP7", '
        'ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),\n', "")
    entries = build_entries(QUIRK_SOURCE, [("v7.1", without)])
    assert entries[(0x17AA, 0x386A)][2] == ""
    assert entries[(0x17AA, 0x3801)][2] == "7.1"


# What a released kernel contains cannot change, so a `since` already recorded
# is carried, not re-derived. These lock the two halves of that: the recorded
# value survives *and* costs no fetch, and an entry we have never dated is
# still dated correctly however many releases back that takes.

def _dated(since, entries=None, commit=""):
    """*entries* (default: the fixture's) with every row recorded as *since*,
    and as *commit* where a test needs a link to carry."""
    entries = entries or build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)])
    return {key: (model, pins, since, codec_only, commit)
            for key, (model, pins, _old, codec_only, _c) in entries.items()}


def _counting(*releases):
    """A release feed that records which tags were actually pulled from it.

    A generator, like the real one in ``main``: pulling an item is fetching a
    blob, so what this list ends up holding *is* the network cost.
    """
    pulled = []

    def feed():
        for tag, src in releases:
            pulled.append(tag)
            yield tag, src
    return feed(), pulled


def test_a_dated_entry_costs_no_release_fetch():
    """The saving, and the part a regression would undo silently: with every
    entry already dated there is nothing to look up, so the walk must not pull
    a single release — and must hand back the recorded values untouched."""
    feed, pulled = _counting(("v7.2", QUIRK_SOURCE), ("v7.1", QUIRK_SOURCE))
    entries = build_entries(QUIRK_SOURCE, feed, _dated("6.5"))
    assert pulled == []
    assert {since for _m, _p, since, _c, _l in entries.values()} == {"6.5"}


def test_an_undated_entry_stops_at_the_first_release_without_it():
    """A run that missed a release must still name the *oldest* one carrying
    the entry, not the newest it happens to look at — and must stop as soon as
    a release lacks it, since everything older lacks it too."""
    without = QUIRK_SOURCE.replace(
        '\tHDA_CODEC_QUIRK(0x17aa, 0x386a, "Lenovo Yoga 7 16IAP7", '
        'ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),\n', "")
    current = _dated("6.5")
    current[(0x17AA, 0x386A)] = ("m", "0x17", "", True, "")  # in no release yet

    feed, pulled = _counting(("v7.2", QUIRK_SOURCE), ("v7.1", QUIRK_SOURCE),
                             ("v7.0", without), ("v6.19", without))
    entries = build_entries(QUIRK_SOURCE, feed, current)
    assert entries[(0x17AA, 0x386A)][2] == "7.1"
    assert pulled == ["v7.2", "v7.1", "v7.0"]  # v6.19 had nothing left to say


def test_an_entry_new_to_the_table_is_dated_from_the_releases():
    """No recorded value to carry, so it takes the walk like anything else."""
    current = _dated("6.5")
    del current[(0x17AA, 0x386A)]
    entries = build_entries(QUIRK_SOURCE, [("v7.2", QUIRK_SOURCE)], current)
    assert entries[(0x17AA, 0x386A)][2] == "7.2"


def test_a_row_that_changes_match_kind_is_re_dated_not_carried():
    """Upstream re-keying an entry from SND_PCI_QUIRK to HDA_CODEC_QUIRK means
    the PCI-keyed one had never matched — 75dc2eda659f found exactly that on
    the Yoga Slim 7 14AKP10, whose PCI id belongs to another machine. Carrying
    the old date would tell that owner the fix reached them releases ago and
    something local is blocking it, when nothing ever reached them."""
    recoded = QUIRK_SOURCE.replace(
        '\tSND_PCI_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ',
        '\tHDA_CODEC_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ')
    entries = build_entries(recoded, [("v7.2", QUIRK_SOURCE),
                                      ("v7.1", QUIRK_SOURCE)], _dated("6.5"))
    assert entries[(0x17AA, 0x3801)][3] is True
    assert entries[(0x17AA, 0x3801)][2] == ""   # no release carries it this way
    assert entries[(0x17AA, 0x386A)][2] == "6.5"  # kind unchanged, still carried


def test_re_dating_a_flipped_row_finds_the_release_carrying_the_new_kind():
    """The other half: once a release ships the re-keyed entry, that release is
    the answer — not the older ones that carried the dead PCI-keyed one."""
    recoded = QUIRK_SOURCE.replace(
        '\tSND_PCI_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ',
        '\tHDA_CODEC_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ')
    entries = build_entries(recoded, [("v7.3", recoded),
                                      ("v7.2", QUIRK_SOURCE)], _dated("6.5"))
    assert entries[(0x17AA, 0x3801)][2] == "7.3"


def test_a_recorded_value_newer_than_the_releases_is_still_carried():
    """Carrying is unconditional, not a min(): the recorded value came from a
    release that still contains what it contained. Re-deriving it against a
    shorter walk is exactly the rewrite this exists to stop."""
    entries = build_entries(QUIRK_SOURCE, [("v6.19", QUIRK_SOURCE)],
                            _dated("7.1"))
    assert entries[(0x17AA, 0x386A)][2] == "7.1"


def test_the_walk_stops_at_the_release_window():
    """The one case that can walk deep — a fixup family the parser only just
    learned to read, so every row is undated at once — is railed, because the
    mirror rate-limits a long run of blob fetches."""
    feed, pulled = _counting(*[(f"v9.{n}", QUIRK_SOURCE) for n in range(40)])
    build_entries(QUIRK_SOURCE, feed, None)
    assert len(pulled) == _RELEASE_WINDOW


def test_release_tags_excludes_candidates_and_orders_newest_first():
    tags = release_tags("v7.2-rc1 2026-07-01\nv7.1 2026-06-01\n"
                        "v7.0 2026-04-01\nv6.19 2026-02-01\n")
    assert tags[:3] == ["v7.1", "v7.0", "v6.19"]
    assert not any("rc" in t for t in tags)


def test_release_tags_needs_at_least_one_release():
    with pytest.raises(ValueError):
        release_tags("v7.2-rc1 2026-07-01\n")


# --- commit: the upstream change the warning links --------------------------

# Both look like real shas (the issue #53 fixup and the re-key that made the
# match-kind rule) so a failure message reads as one; only the first 12 hex
# digits ever reach a table.
OWNER = "b70f007a9fc6" + "0" * 28
OTHER = "75dc2eda659f" + "0" * 28
MASS = "aabbccdd1122" + "0" * 28
MASTER_SHA = "f" * 40
ALC269_PATH = _SOURCE_PATHS[0]
SPLIT_SHA, (HOP_PARENT, HOP_PATH) = next(iter(_FILE_MOVES.items()))


def _fixture_lines(src=QUIRK_SOURCE):
    """``{key: source line}``, collected the way ``build_entries`` collects it."""
    lines = {}
    parse_quirks(src, lines=lines)
    return lines


def _blaming(per_path, calls=None):
    """A blame callable answering from ``{path: {line: oid}}``.

    A plain dict rather than a recorded HTTP exchange: what the callers have
    to get right is which file and which line they ask about, and *calls*
    counts the round trips a real run would pay for.
    """
    def blame(sha, path):
        if calls is not None:
            calls.append((sha, path))
        return per_path[path]
    return blame


def test_the_recorded_line_is_the_entry_that_won():
    """Blame is read at one line per row, so it has to be the line the row was
    built from. A duplicate id resolves to the first *qualifying* entry — and
    an entry that qualifies is not always the first one mentioning the id, so
    both halves are pinned here."""
    duped = QUIRK_SOURCE + (
        '\tSND_PCI_QUIRK(0x17aa, 0x386a, "dup", '
        'ALC287_FIXUP_YOGA9_14IMH9_BASS_SPK_PIN),\n'
        '\tSND_PCI_QUIRK(0x17aa, 0x3999, "skipped", ALC287_FIXUP_TAS2781_I2C),\n'
        '\tSND_PCI_QUIRK(0x17aa, 0x3999, "kept", ALC290_FIXUP_SUBWOOFER),\n')
    lines = _fixture_lines(duped)
    rows = duped.splitlines()
    assert "Lenovo Yoga 7 16IAP7" in rows[lines[(0x17AA, 0x386A)] - 1]
    assert '"kept"' in rows[lines[(0x17AA, 0x3999)] - 1]


def test_entry_lines_takes_the_first_mention_of_an_id_unfiltered():
    """The hop reads a file from before the split, where the era's fixup chain
    need not qualify under today's rules while the entry is plainly there. So
    that side asks only where the id is written, and takes the kernel's own
    first-match answer."""
    src = ('\tSND_PCI_QUIRK(0x17aa, 0x3801, "first", ALC269_FIXUP_UNKNOWN),\n'
           '\tSND_PCI_QUIRK(0x17aa, 0x3801, "second", ALC290_FIXUP_SUBWOOFER),\n')
    assert entry_lines(src) == {(0x17AA, 0x3801): 1}
    assert parse_quirks(src) == {}      # neither qualifies, the line still does


def test_a_row_records_the_commit_that_owns_its_line():
    lines = _fixture_lines()
    entries = build_entries(
        QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)], master_sha=MASTER_SHA,
        blame=_blaming({ALC269_PATH: {lines[(0x17AA, 0x3801)]: OWNER}}),
        blob=lambda ref, path: "")
    assert entries[(0x17AA, 0x3801)][4] == OWNER[:12]
    assert entries[(0x17AA, 0x386A)][4] == ""    # blame knows nothing of it


def test_a_row_the_file_split_swallowed_resolves_through_the_hop():
    """1003 of mainline's 1251 quirk lines blame to the 2025 split that carved
    alc269.c out of realtek.c, because GitHub's blame does not cross a split.
    Blaming the file it came from, at the split's parent, reaches the author —
    and the hop costs one blob and one blame however many rows need it."""
    lines = _fixture_lines()
    calls = []
    entries = build_entries(
        QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)], master_sha=MASTER_SHA,
        blame=_blaming({ALC269_PATH: {lines[(0x17AA, 0x3801)]: SPLIT_SHA,
                                      lines[(0x17AA, 0x386A)]: SPLIT_SHA},
                        HOP_PATH: {2: OWNER}}, calls),
        blob=lambda ref, path: (
            '\tSND_PCI_QUIRK(0x17aa, 0x386a, "x", ALC287_FIXUP_ANY),\n'
            '\tSND_PCI_QUIRK(0x17aa, 0x3801, "x", ALC287_FIXUP_ANY),\n'))
    assert entries[(0x17AA, 0x3801)][4] == OWNER[:12]
    assert entries[(0x17AA, 0x386A)][4] == ""     # line 1 has no owner
    assert calls == [(MASTER_SHA, ALC269_PATH), (HOP_PARENT, HOP_PATH)]


def test_a_row_missing_from_the_pre_split_file_keeps_no_link():
    """An entry added after the split cannot be in the file it was split from.
    No link beats the wrong one — the warning falls back to naming the file."""
    lines = _fixture_lines()
    entries = build_entries(
        QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)], master_sha=MASTER_SHA,
        blame=_blaming({ALC269_PATH: {lines[(0x17AA, 0x3801)]: SPLIT_SHA},
                        HOP_PATH: {1: OWNER}}),
        blob=lambda ref, path: '\tSND_PCI_QUIRK(0x17aa, 0x9999, "other", X),\n')
    assert entries[(0x17AA, 0x3801)][4] == ""


# One commit owning more than a hundred quirk lines is not an author.
MASS_FILLER = "".join(
    f'\tSND_PCI_QUIRK(0x17aa, 0x{i:04x}, "mass {i:#06x}", '
    'ALC290_FIXUP_SUBWOOFER),\n'
    for i in range(0x9500, 0x9500 + _MAX_LINES_PER_COMMIT + 1))
MASS_SOURCE = QUIRK_SOURCE + MASS_FILLER


def test_a_commit_owning_most_of_the_file_is_refused_and_reported(capsys):
    """The rail, and the reason it is not just a hop: the next move upstream
    makes has no _FILE_MOVES entry yet, and the failure it would otherwise
    produce is a whole table of rows linking a refactor as the fix for the
    user's speaker. Refused, and named once so the entry can be added."""
    owners = {line: MASS for line in entry_lines(MASS_SOURCE).values()}
    entries = build_entries(MASS_SOURCE, [("v7.1", MASS_SOURCE)],
                            master_sha=MASTER_SHA,
                            blame=_blaming({ALC269_PATH: owners}),
                            blob=lambda ref, path: "")
    assert {row[4] for row in entries.values()} == {""}
    err = capsys.readouterr().err
    assert err.count("owns") == 1
    assert MASS[:12] in err and "_FILE_MOVES" in err


def test_a_blame_failure_leaves_the_links_empty_and_builds_the_table(capsys):
    """A blame outage must not hold back a table update: who gets warned about
    their machine does not depend on being able to link the commit that fixed
    it."""
    def blame(sha, path):
        raise ValueError("blame of alc269.c: HTTP 502")

    entries = build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)],
                            master_sha=MASTER_SHA, blame=blame,
                            blob=lambda ref, path: "")
    assert sorted(entries) == [(0x17AA, i) for i in sorted(PIN_IDS + FILLER_IDS)]
    assert {row[4] for row in entries.values()} == {""}
    assert "warning: blame of alc269.c: HTTP 502" in capsys.readouterr().err


def test_a_recorded_commit_costs_no_blame():
    """The saving, and the half a regression would undo silently: blame can
    only move when the line moved, so a table nothing changed in asks GitHub
    nothing at all."""
    calls = []
    entries = build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)],
                            _dated("6.5", commit=OWNER[:12]),
                            master_sha=MASTER_SHA, blame=_blaming({}, calls),
                            blob=lambda ref, path: "")
    assert calls == []
    assert {row[4] for row in entries.values()} == {OWNER[:12]}


def test_a_row_whose_content_changed_is_re_blamed():
    """The other half: upstream editing the entry moves what the row says, so
    the recorded link describes a line that no longer exists. A model change is
    enough — that is the fixup the entry points at changing."""
    current = _dated("6.5", commit=OTHER[:12])
    model, pins, since, codec_only, commit = current[(0x17AA, 0x3801)]
    current[(0x17AA, 0x3801)] = ("older-fixup-name", pins, since, codec_only,
                                 commit)
    lines = _fixture_lines()
    entries = build_entries(
        QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)], current,
        master_sha=MASTER_SHA,
        blame=_blaming({ALC269_PATH: {lines[(0x17AA, 0x3801)]: OWNER}}),
        blob=lambda ref, path: "")
    assert entries[(0x17AA, 0x3801)][4] == OWNER[:12]
    assert entries[(0x17AA, 0x386A)][4] == OTHER[:12]   # unchanged, carried


def test_rescan_re_derives_every_commit():
    """--rescan drops the recorded table, so both fields come back from
    upstream. Cheap here where it is not for `since`: blame is one query."""
    lines = _fixture_lines()
    owners = {line: OWNER for line in lines.values()}
    entries = build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)], None,
                            master_sha=MASTER_SHA,
                            blame=_blaming({ALC269_PATH: owners}),
                            blob=lambda ref, path: "")
    assert {row[4] for row in entries.values()} == {OWNER[:12]}


class _Response:
    """Enough of an ``http.client.HTTPResponse`` for ``urlopen``'s callers."""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_github_blame_expands_ranges_and_retries_once(monkeypatch):
    """One query answers for the whole file, as ranges — the callers want them
    per line. The retry is why a single 502 does not cost a week's links."""
    payload = json.dumps({"data": {"repository": {"object": {"blame": {
        "ranges": [
            {"startingLine": 1, "endingLine": 3, "commit": {"oid": OWNER}},
            {"startingLine": 4, "endingLine": 4, "commit": {"oid": OTHER}},
        ]}}}}}).encode()
    attempts = []

    def urlopen(request, timeout=None):
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway",
                                         {}, None)
        return _Response(payload)

    monkeypatch.setattr(pin_updater.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(pin_updater, "_BLAME_RETRY_WAIT", 0)
    owners = github_blame("token")(MASTER_SHA, ALC269_PATH)
    assert owners == {1: OWNER, 2: OWNER, 3: OWNER, 4: OTHER}
    assert attempts == ["https://api.github.com/graphql"] * 2


def test_github_blame_reports_a_graphql_error(monkeypatch):
    """GraphQL answers a bad query with HTTP 200 and an `errors` list, so the
    happy path would otherwise read it as a file with no lines."""
    payload = json.dumps({"errors": [{"message": "Bad credentials"}]}).encode()
    monkeypatch.setattr(pin_updater.urllib.request, "urlopen",
                        lambda request, timeout=None: _Response(payload))
    with pytest.raises(ValueError, match="Bad credentials"):
        github_blame("token")(MASTER_SHA, ALC269_PATH)


def test_fetch_master_sha_strips_the_anti_xssi_line(monkeypatch):
    """googlesource prefixes its JSON with a line that is not JSON."""
    body = (")]}'\n" + json.dumps({"commit": OWNER, "tree": "0" * 40})).encode()
    monkeypatch.setattr(pin_updater.urllib.request, "urlopen",
                        lambda url, timeout=None: _Response(body))
    assert fetch_master_sha() == OWNER


def test_fetch_master_sha_refuses_a_body_without_one(monkeypatch):
    monkeypatch.setattr(pin_updater.urllib.request, "urlopen",
                        lambda url, timeout=None: _Response(b'{"log": []}'))
    with pytest.raises(ValueError, match="no mainline commit sha"):
        fetch_master_sha()


# --- the shipped table ------------------------------------------------------

def test_render_reproduces_the_live_table_byte_for_byte():
    """A refreshed entry must be a one-line diff, never a whole-block
    reformat — otherwise a real change hides inside the noise."""
    src = TABLE_MODULE.read_text()
    (start, end), entries = parse_table(src)
    assert render_table(entries) == src[start:end]


def test_live_table_spans_several_vendors():
    _, entries = parse_table(TABLE_MODULE.read_text())
    assert MIN_ENTRIES <= len(entries) <= MAX_ENTRIES
    # Lenovo, HP, Dell, ASUS, Acer and others all ship machines with a pin
    # their firmware hides.
    assert {0x17AA, 0x103C, 0x1028, 0x1043} <= {v for v, _ in entries}


def test_parse_table_refuses_a_partial_parse():
    """The guard that fired for real when the literal's shape changed: refusing
    beats diffing against a table we only half understood."""
    src = TABLE_MODULE.read_text().replace(
        'pins="0x17", since="', 'pins="0x17", released="', 1)
    with pytest.raises(ValueError, match="refusing to edit"):
        parse_table(src)


def test_parse_table_needs_the_literal():
    with pytest.raises(ValueError, match="no _SPEAKER_PIN_QUIRKS"):
        parse_table("x = 1\n")


def test_build_entries_refuses_an_implausible_parse():
    """A renamed fixup upstream would empty the table; better to fail the
    weekly run than to silently stop warning anybody."""
    with pytest.raises(ValueError, match="suspect a parse bug"):
        build_entries("no quirks here\n", [("v7.1", QUIRK_SOURCE)])


def test_apply_update_touches_only_the_table():
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    entries[(0x17AA, 0x9999)] = ("alc287-yoga9-bass-spk-pin", "0x17", "7.2",
                                True, "b70f007a9fc6")
    updated = apply_update(src, entries)
    ast.parse(updated)
    assert parse_table(updated)[1] == entries
    # Everything outside the literal is untouched.
    assert updated.replace(render_table(entries), "") == \
        src.replace(render_table(parse_table(src)[1]), "")


def test_apply_update_round_trips_an_empty_since_and_commit():
    """Both optional fields render and read back empty: a mainline-only entry
    has no release to name, and an unresolved one no commit to link."""
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    entries[(0x17AA, 0x9998)] = ("", "0x14 0x17", "", False, "")
    assert parse_table(apply_update(src, entries))[1] == entries


# --- CLI --------------------------------------------------------------------

def _run(args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "tools" /
                           "update_speaker_pin_quirks.py"), *args],
                          capture_output=True, text=True, cwd=cwd)


UNDATED_ID = (0x17AA, 0x386A)


def _offline(tmp_path, master_src=QUIRK_SOURCE):
    """A copy of the shipped table plus offline sources, and the argv to
    drive them.

    ``UNDATED_ID``'s recorded series is blanked in the copy, so the fixture
    holds one entry of each kind: dated, which must be carried through
    untouched, and undated, which must be looked up in the release. Blanked
    rather than chosen — which entries ship undated is the weekly refresh's to
    decide, and a test that picked one by name would fail the week it was
    dated. That is how this fixture broke: it asserted a derived value for an
    entry the run had just dated.
    """
    script = tmp_path / "speaker_pin_quirks.py"
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    model, pins, _since, codec_only, commit = entries[UNDATED_ID]
    entries[UNDATED_ID] = (model, pins, "", codec_only, commit)
    script.write_text(apply_update(src, entries))
    (tmp_path / "master.c").write_text(master_src)
    (tmp_path / "release.c").write_text(QUIRK_SOURCE)
    return script, ["--script", str(script),
                    "--offline-master", str(tmp_path / "master.c"),
                    "--offline-release", str(tmp_path / "release.c"),
                    "--offline-release-tag", "v7.1"]


def test_cli_reports_without_writing(tmp_path):
    script, argv = _offline(tmp_path)
    before = script.read_text()

    result = _run(argv, cwd=ROOT)
    assert result.returncode == 0
    # The fixture drops the ids the shipped table has beyond it, so the report
    # must name them — and must still not touch the file without --write.
    assert "- 17aa:394c" in result.stdout
    assert script.read_text() == before


def test_cli_writes_with_the_flag(tmp_path):
    script, argv = _offline(tmp_path)
    assert _run([*argv, "--write"], cwd=ROOT).returncode == 0
    _, entries = parse_table(script.read_text())
    assert sorted(entries) == [(0x17AA, i) for i in sorted(PIN_IDS + FILLER_IDS)]
    # An undated id present in the release fixture resolves to that release.
    assert entries[UNDATED_ID][2] == "7.1"


def test_cli_carries_a_recorded_since_but_rescan_re_derives_it(tmp_path):
    """End to end, because carrying only helps if `main` passes the shipped
    table down. 3801 is dated in the table and present in the release fixture,
    so a re-derivation would visibly move it to that release — which is what
    used to happen every week, and what --rescan is now the only way to ask
    for. Its recorded value is read, not written down here: it is machine-
    written and moves on upstream's schedule."""
    script, argv = _offline(tmp_path)
    _, shipped = parse_table(script.read_text())
    recorded = shipped[(0x17AA, 0x3801)][2]
    assert recorded and recorded != "7.1", "fixture no longer discriminates"

    assert _run([*argv, "--write"], cwd=ROOT).returncode == 0
    assert parse_table(script.read_text())[1][(0x17AA, 0x3801)][2] == recorded

    assert _run([*argv, "--write", "--rescan"], cwd=ROOT).returncode == 0
    assert parse_table(script.read_text())[1][(0x17AA, 0x3801)][2] == "7.1"


def test_cli_fails_closed_on_an_implausible_parse(tmp_path):
    """A renamed fixup upstream empties the parse. The run must abort with the
    shipped table intact rather than leave users unwarned."""
    script, argv = _offline(tmp_path, master_src="nothing to parse here\n")
    before = script.read_text()

    result = _run([*argv, "--write"], cwd=ROOT)
    assert result.returncode == 1
    assert "suspect a parse bug" in result.stderr
    assert script.read_text() == before


# The blame half runs `main` in-process: the fake reaches it by patching the
# module, which a subprocess would not see. Everything else about the fixture
# is the same offline table and sources.

def test_cli_blame_without_a_token_refuses(tmp_path, monkeypatch, capsys):
    """Failing here beats a run that quietly rewrites every link to empty."""
    script, argv = _offline(tmp_path)
    before = script.read_text()
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main([*argv, "--write", "--blame"]) == 1
    assert "error:" in capsys.readouterr().err
    assert script.read_text() == before


def test_cli_blame_writes_the_resolved_commits(tmp_path, monkeypatch):
    """End to end, because resolving only helps if `main` wires the backend
    down to build_entries and the render carries the field. With --rescan, so
    every row comes from this run rather than from whatever the shipped table
    already records — which is also the audit those two flags are for."""
    script, argv = _offline(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "token")
    owners = {line: OWNER for line in _fixture_lines().values()}
    monkeypatch.setattr(pin_updater, "github_blame",
                        lambda token: _blaming({ALC269_PATH: owners}))

    assert main([*argv, "--write", "--blame", "--rescan"]) == 0
    _, entries = parse_table(script.read_text())
    assert {row[4] for row in entries.values()} == {OWNER[:12]}


def test_cli_without_blame_carries_links_and_leaves_new_rows_empty(tmp_path):
    """The weekly run's shape: no token, no query, and the links an earlier
    run resolved survive the rebuild. A row new to the table gets none, which
    is what the warning's fallback is for."""
    script, argv = _offline(tmp_path)
    src = script.read_text()
    _, entries = parse_table(src)
    key = (0x17AA, 0x3801)
    # Recorded against what the fixture parse says, so the row's content
    # matches and the carry is what is being tested rather than the shipped
    # table happening to agree with it.
    model, pins, codec_only = parse_quirks(QUIRK_SOURCE)[key]
    entries[key] = (model, pins, entries[key][2], codec_only, OWNER[:12])
    script.write_text(apply_update(src, entries))

    assert main([*argv, "--write"]) == 0
    _, written = parse_table(script.read_text())
    assert written[key][4] == OWNER[:12]
    assert written[(0x17AA, FILLER_IDS[0])][4] == ""


def test_a_renamed_helper_aborts_instead_of_dropping_its_family():
    """The size rails count the whole table, so losing one fixup family leaves
    the total inside them and the weekly PR looks clean. Twenty of the shipped
    rows hang off a single helper, so that has to be checked per helper."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    renamed = QUIRK_SOURCE.replace(
        "alc287_fixup_yoga9_14iap7_bass_spk_pin",
        "alc287_fixup_yoga9_bass_spk_pin_v2")
    with pytest.raises(ValueError, match="no longer in the kernel source"):
        pin_adding_fixups(renamed, require_helpers=True)


def test_helper_check_is_off_for_historical_releases():
    """The check runs against mainline only. A helper legitimately does not
    exist in releases older than the one that introduced it
    (alc285_fixup_hp_spectre_x360_df1 first appears in 6.15), and treating that
    as a rename aborted every real run — which it did, on the first try."""
    from tools.update_speaker_pin_quirks import pin_adding_fixups
    older = QUIRK_SOURCE.replace(
        "\t\t.v.func = alc285_fixup_hp_spectre_x360_df1,\n", "")
    assert pin_adding_fixups(older) is not None       # no raise without the flag
    assert pin_adding_fixups("static int foo(void) { return 0; }\n") == {}
