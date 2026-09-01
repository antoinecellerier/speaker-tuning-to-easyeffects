"""The speaker-route quirk-table updater edits shipped source unattended.

A wrong entry here is worse than invisible: it tells a user their woofer is
stuck at a fixed level and to reconfigure their kernel for a fixup that doesn't
apply to their machine. So the tests lock that re-rendering the *current* table
reproduces the file byte-for-byte, that the quirk parse matches what upstream's
table actually says, and that every implausible input is refused rather than
written.

Membership is the one thing that works differently from the sibling pin table:
there is nothing to derive, because a reroute lives inside a helper's C. So the
fixture is built from ``_FUNC_FIXUP_ROUTES`` itself, and the tests below are
mostly about the two ways a listed helper reaches a machine — directly, or
through a wrapper that names no model.

All offline: kernel source comes from fixture strings, never the network.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tools.update_speaker_route_quirks import (
    MAX_ENTRIES,
    MIN_ENTRIES,
    apply_update,
    build_entries,
    parse_quirks,
    parse_table,
    render_table,
    route_fixups,
)
from tools.update_speaker_route_quirks import _FUNC_FIXUP_ROUTES, _RELEASE_WINDOW

# The helpers the fixture exercises by name; every *other* helper in
# _FUNC_FIXUP_ROUTES gets a stub below, because one missing from the source is
# what the rename guard aborts on.
EXERCISED = ("alc285_fixup_speaker2_to_dac1", "alc295_fixup_disable_dac3",
             "alc298_fixup_speaker_volume")

HELPER_STUBS = "".join(
    f"\t[STUB_{i}] = {{\n\t\t.type = HDA_FIXUP_FUNC,\n"
    f"\t\t.v.func = {helper},\n\t}},\n"
    for i, helper in enumerate(sorted(_FUNC_FIXUP_ROUTES))
    if helper not in EXERCISED)

ROOT = Path(__file__).resolve().parent.parent
TABLE_MODULE = ROOT / "lib" / "data" / "speaker_route_quirks.py"


# Excerpted from sound/hda/codecs/realtek/alc269.c. Which fixups qualify comes
# from the hand-verified _FUNC_FIXUP_ROUTES allowlist — the helper's C decides,
# and no regex over the fixup table can read it — so these definitions are here
# to exercise the two routes to a machine and the neighbours that must not
# qualify.
#
# ALC285_FIXUP_SPEAKER2_TO_DAC1, ALC295_FIXUP_DISABLE_DAC3 and
# ALC298_FIXUP_LENOVO_SPK_VOLUME are the direct shape;
# ALC285_FIXUP_ASUS_GA605K_HEADSET_MIC is the wrapper shape (a headset pin
# table that chains to the reroute, and has no name of its own). The two pin
# fixups belong to the sibling table — they declare a speaker pin rather than
# rerouting one — and ALC269_FIXUP_THINKPAD_ACPI is an ordinary neighbour.
FIXUP_DEFS = """\
\t[ALC285_FIXUP_SPEAKER2_TO_DAC1] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc285_fixup_speaker2_to_dac1,
\t},
\t[ALC285_FIXUP_ASUS_GA605K_HEADSET_MIC] = {
\t\t.type = HDA_FIXUP_PINS,
\t\t.v.pins = (const struct hda_pintbl[]) {
\t\t\t{ 0x19, 0x03a11050 },
\t\t\t{ 0x1b, 0x03a11c30 },
\t\t\t{ }
\t\t},
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_SPEAKER2_TO_DAC1
\t},
\t[ALC295_FIXUP_DISABLE_DAC3] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc295_fixup_disable_dac3,
\t},
\t[ALC298_FIXUP_LENOVO_SPK_VOLUME] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc298_fixup_speaker_volume,
\t},
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
\t},
\t[ALC269_FIXUP_THINKPAD_ACPI] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_thinkpad_acpi,
\t},
""" + HELPER_STUBS


# Two of the fixups have a name a user can force; the GA605K wrapper
# deliberately has none (see the test below).
MODELS = ('\t{.id = ALC285_FIXUP_SPEAKER2_TO_DAC1, '
          '.name = "alc285-speaker2-to-dac1"},\n'
          '\t{.id = ALC298_FIXUP_LENOVO_SPK_VOLUME, '
          '.name = "alc298-spk-volume"},\n')

# The quirk table itself, kept above MIN_ENTRIES so the plausibility guard
# doesn't fire on the fixture. One entry of each match shape: HDA_CODEC_QUIRK
# (matched on the codec's own subsystem id) and SND_PCI_QUIRK.
QUIRK_TABLE = """\
\tSND_PCI_QUIRK(0x1028, 0x075c, "Dell XPS 27 7760", ALC298_FIXUP_LENOVO_SPK_VOLUME),
\tSND_PCI_QUIRK(0x1028, 0x07b0, "Dell Precision 7520", ALC295_FIXUP_DISABLE_DAC3),
\tSND_PCI_QUIRK(0x1028, 0x0a61, "Dell XPS 15 9510", ALC285_FIXUP_SPEAKER2_TO_DAC1),
\tSND_PCI_QUIRK(0x1043, 0x1c62, "ASUS GU603", ALC285_FIXUP_ASUS_GA605K_HEADSET_MIC),
\tHDA_CODEC_QUIRK(0x17aa, 0x3906, "Legion Pro 7i 16IAX10H", ALC285_FIXUP_SPEAKER2_TO_DAC1),
\tSND_PCI_QUIRK(0x17aa, 0x386e, "Yoga Pro 7 14ARP8", ALC285_FIXUP_SPEAKER2_TO_DAC1),
\tSND_PCI_QUIRK(0x17aa, 0x384a, "Lenovo Yoga 7 15ITL5", ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),
\tSND_PCI_QUIRK(0x17aa, 0x3801, "Lenovo Yoga9 14IAP7", ALC290_FIXUP_SUBWOOFER),
\tSND_PCI_QUIRK(0x17aa, 0x3802, "Lenovo Yoga Pro 9 14IRP8", ALC269_FIXUP_THINKPAD_ACPI),
"""

# The six ids above that reach a listed helper, and the three that must not:
# two speaker-*pin* fixups, which the sibling table owns, and a plain neighbour.
ROUTE_IDS = [(0x1028, 0x075C), (0x1028, 0x07B0), (0x1028, 0x0A61),
             (0x1043, 0x1C62), (0x17AA, 0x3906), (0x17AA, 0x386E)]
IGNORED_IDS = [(0x17AA, 0x384A), (0x17AA, 0x3801), (0x17AA, 0x3802)]

# Filler rows so the fixture clears MIN_ENTRIES. The whole-table rail exists to
# catch a parse that collapses, so a fixture below it would only be testable by
# weakening the guard the shipped table depends on.
FILLER_IDS = [(0x17AA, i) for i in range(0x9000, 0x9000 + MIN_ENTRIES)]
FILLER = "".join(
    f'\tSND_PCI_QUIRK(0x{vendor:04x}, 0x{device:04x}, "filler {device:#06x}", '
    'ALC285_FIXUP_SPEAKER2_TO_DAC1),\n' for vendor, device in FILLER_IDS)

QUIRK_SOURCE = FIXUP_DEFS + MODELS + QUIRK_TABLE + FILLER

FIXTURE_IDS = sorted(ROUTE_IDS + FILLER_IDS)


def test_parse_quirks_keeps_only_rerouting_fixups():
    """A fixup that declares a speaker pin fixes a different fault and belongs
    to the sibling table; an ACPI fixup fixes none of ours. Neither may license
    a claim about where a pin takes its signal from."""
    assert sorted(parse_quirks(QUIRK_SOURCE)) == FIXTURE_IDS
    assert not set(IGNORED_IDS) & set(parse_quirks(QUIRK_SOURCE))


def test_route_fixups_records_the_pin_and_sources_the_helper_sets():
    """Straight off the allowlist: pin 0x17 fed from mixer 0x0c on the ALC298,
    from DACs 0x02/0x03 on the ALC295. The sources are what carries a volume
    amplifier where the codec's default did not."""
    found = route_fixups(QUIRK_SOURCE)
    assert found["ALC298_FIXUP_LENOVO_SPK_VOLUME"] == ("0x17", ("0x0c",))
    assert found["ALC295_FIXUP_DISABLE_DAC3"] == ("0x17", ("0x02", "0x03"))
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x1028, 0x075C)][1:3] == ("0x17", "0x0c")
    assert parsed[(0x1028, 0x07B0)][1:3] == ("0x17", "0x02 0x03")


def test_route_fixups_ignores_a_fixup_that_declares_a_pin():
    """Both shapes the sibling table owns: an ``hda_pintbl`` writing a
    ``0x9017xxxx`` speaker config, and an ``HDA_FIXUP_FUNC`` whose helper is on
    *its* allowlist. Neither reroutes anything, and admitting one here would
    report a volume-path fault on a machine whose fault was a hidden pin."""
    found = route_fixups(QUIRK_SOURCE)
    assert "ALC290_FIXUP_SUBWOOFER" not in found
    assert "ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN" not in found


def test_a_fixup_delivers_the_route_its_chain_sets():
    """Upstream extends a machine by wrapping the rerouting fixup, not editing
    it: ALC285_FIXUP_ASUS_GA605K_HEADSET_MIC adds a headset pin table and
    chains to ALC285_FIXUP_SPEAKER2_TO_DAC1. Reading only a fixup's own body
    would drop the wrapper's machines while every kernel carrying them
    reroutes 0x17 — and here that is most of the table, not an edge case."""
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x1043, 0x1C62)][1:3] == ("0x17", "0x02")


def test_a_wrapper_reached_row_records_no_forcible_model():
    """The majority case, and the one worth pinning: the wrapper has no name in
    the models table, so the row carries none. Borrowing the inner fixup's
    ``alc285-speaker2-to-dac1`` would reroute the pin and skip the headset (on
    the real TAS2781 wrappers, the amplifier) setup the wrapper also performs —
    a half-fix presented as a fix."""
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x1043, 0x1C62)][0] == ""
    assert parsed[(0x17AA, 0x3906)][0] == "alc285-speaker2-to-dac1"


def test_parse_quirks_records_the_codec_only_match_key():
    """HDA_CODEC_QUIRK is matched against the codec's subsystem id only. Losing
    this would let us claim a PCI-id match the kernel never makes."""
    parsed = parse_quirks(QUIRK_SOURCE)
    assert parsed[(0x17AA, 0x3906)][3] is True    # HDA_CODEC_QUIRK
    assert parsed[(0x17AA, 0x386E)][3] is False   # SND_PCI_QUIRK


def test_a_chain_reaching_two_listed_helpers_is_refused():
    """A route is taken from the first listed helper in the chain, where the
    pin table unions its links — two links overriding the same pin's source
    list would be upstream contradicting itself about where that speaker's
    signal comes from, and picking one answer would be a guess. No chain
    upstream does this today; this is the guard for the day one does."""
    src = FIXUP_DEFS + """\
\t[ALC285_FIXUP_TWO_ROUTES] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc295_fixup_disable_dac3,
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_SPEAKER2_TO_DAC1
\t},
"""
    with pytest.raises(ValueError, match="two source lists"):
        route_fixups(src)


def test_a_chained_before_wrapper_delivers_its_target_route():
    """__snd_hda_apply_fixup recurses into chain_id *before* applying a
    `.chained_before` fixup, so the reroute lands either way. Reading only
    `.chained` would drop such a machine the moment upstream wraps a rerouting
    fixup that way."""
    src = FIXUP_DEFS + """\
\t[ALC285_FIXUP_BEFORE_ROUTE] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained_before = true,
\t\t.chain_id = ALC285_FIXUP_SPEAKER2_TO_DAC1
\t},
"""
    assert route_fixups(src)["ALC285_FIXUP_BEFORE_ROUTE"] == ("0x17", ("0x02",))


def test_an_inert_chain_id_is_not_followed():
    """``.chain_id`` without ``.chained = true`` is dead source the kernel
    never walks, so neither may we."""
    src = FIXUP_DEFS + """\
\t[ALC285_FIXUP_NOT_CHAINED] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chain_id = ALC285_FIXUP_SPEAKER2_TO_DAC1,
\t},
"""
    assert "ALC285_FIXUP_NOT_CHAINED" not in route_fixups(src)


def test_a_chain_that_loops_terminates():
    """A malformed source must fail the size rails, not hang the weekly run.
    The second loop also has to come back with its route rather than raising:
    reaching the *same* helper twice is a walk revisiting one link, not
    upstream giving one pin two source lists."""
    src = FIXUP_DEFS + """\
\t[ALC285_FIXUP_LOOP_A] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_LOOP_B,
\t},
\t[ALC285_FIXUP_LOOP_B] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_LOOP_A,
\t},
\t[ALC285_FIXUP_LOOP_C] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc285_fixup_speaker2_to_dac1,
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_LOOP_D,
\t},
\t[ALC285_FIXUP_LOOP_D] = {
\t\t.type = HDA_FIXUP_FUNC,
\t\t.v.func = alc_fixup_headset_jack,
\t\t.chained = true,
\t\t.chain_id = ALC285_FIXUP_LOOP_C,
\t},
"""
    found = route_fixups(src)
    assert "ALC285_FIXUP_LOOP_A" not in found
    assert found["ALC285_FIXUP_LOOP_C"] == ("0x17", ("0x02",))


def test_parse_quirks_first_qualifying_entry_wins():
    """The kernel walks its table in order, so a duplicate id resolves to the
    first entry here too — but only among the entries that qualify. That
    residual is deliberate and shared with the sibling table: 17aa:3802 and
    17aa:386e are in the shipped table *because* their first entry points at a
    fixup we skip, which is also why the fixture holds 17aa:3802 pointing at a
    non-qualifying fixup."""
    duped = QUIRK_SOURCE + (
        '\tSND_PCI_QUIRK(0x17aa, 0x3906, "dup", ALC295_FIXUP_DISABLE_DAC3),\n'
        '\tSND_PCI_QUIRK(0x17aa, 0x3802, "Lenovo Yoga Pro 9 14IRP8", '
        'ALC295_FIXUP_DISABLE_DAC3),\n')
    parsed = parse_quirks(duped)
    assert parsed[(0x17AA, 0x3906)][2] == "0x02"
    assert parsed[(0x17AA, 0x3802)][2] == "0x02 0x03"


def test_parse_quirks_ignores_an_unrelated_file():
    assert parse_quirks("static int foo(void) { return 0; }\n") == {}


# --- since: the released series a user has to reach -------------------------

def test_since_is_the_oldest_release_still_carrying_the_entry():
    older = FIXUP_DEFS + MODELS + FILLER + (
        '\tSND_PCI_QUIRK(0x1028, 0x0a61, "x", '
        'ALC285_FIXUP_SPEAKER2_TO_DAC1),\n')
    entries = build_entries(QUIRK_SOURCE, [
        ("v7.1", QUIRK_SOURCE),   # newest first
        ("v7.0", QUIRK_SOURCE),
        ("v6.19", older),         # only 1028:0a61 goes this far back
    ])
    assert entries[(0x1028, 0x0A61)][3] == "6.19"
    assert entries[(0x17AA, 0x3906)][3] == "7.0"


def test_since_is_empty_for_a_mainline_only_entry():
    """A quirk merged for the next series is in no release yet — so telling the
    user to upgrade would send them after something that doesn't exist."""
    without = QUIRK_SOURCE.replace(
        '\tHDA_CODEC_QUIRK(0x17aa, 0x3906, "Legion Pro 7i 16IAX10H", '
        'ALC285_FIXUP_SPEAKER2_TO_DAC1),\n', "")
    entries = build_entries(QUIRK_SOURCE, [("v7.1", without)])
    assert entries[(0x17AA, 0x3906)][3] == ""
    assert entries[(0x1028, 0x0A61)][3] == "7.1"


# What a released kernel contains cannot change, so a `since` already recorded
# is carried, not re-derived. These lock the two halves of that: the recorded
# value survives *and* costs no fetch, and an entry we have never dated is
# still dated correctly however many releases back that takes.

def _dated(since, entries=None):
    """*entries* (default: the fixture's) with every row recorded as *since*."""
    entries = entries or build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)])
    return {key: (model, pin, sources, since, codec_only)
            for key, (model, pin, sources, _old, codec_only) in entries.items()}


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
    assert {since for _m, _p, _s, since, _c in entries.values()} == {"6.5"}


def test_an_undated_entry_stops_at_the_first_release_without_it():
    """A run that missed a release must still name the *oldest* one carrying
    the entry, not the newest it happens to look at — and must stop as soon as
    a release lacks it, since everything older lacks it too."""
    without = QUIRK_SOURCE.replace(
        '\tHDA_CODEC_QUIRK(0x17aa, 0x3906, "Legion Pro 7i 16IAX10H", '
        'ALC285_FIXUP_SPEAKER2_TO_DAC1),\n', "")
    current = _dated("6.5")
    current[(0x17AA, 0x3906)] = ("m", "0x17", "0x02", "", True)

    feed, pulled = _counting(("v7.2", QUIRK_SOURCE), ("v7.1", QUIRK_SOURCE),
                             ("v7.0", without), ("v6.19", without))
    entries = build_entries(QUIRK_SOURCE, feed, current)
    assert entries[(0x17AA, 0x3906)][3] == "7.1"
    assert pulled == ["v7.2", "v7.1", "v7.0"]  # v6.19 had nothing left to say


def test_a_row_that_changes_match_kind_is_re_dated_not_carried():
    """Upstream re-keying an entry from SND_PCI_QUIRK to HDA_CODEC_QUIRK means
    the PCI-keyed one had never matched — 75dc2eda659f found exactly that on
    the Yoga Slim 7 14AKP10, whose PCI id belongs to another machine. Carrying
    the old date would tell that owner the fix reached them releases ago and
    something local is blocking it, when nothing ever reached them."""
    recoded = QUIRK_SOURCE.replace(
        '\tSND_PCI_QUIRK(0x17aa, 0x386e, "Yoga Pro 7 14ARP8", ',
        '\tHDA_CODEC_QUIRK(0x17aa, 0x386e, "Yoga Pro 7 14ARP8", ')
    entries = build_entries(recoded, [("v7.2", QUIRK_SOURCE),
                                      ("v7.1", QUIRK_SOURCE)], _dated("6.5"))
    assert entries[(0x17AA, 0x386E)][4] is True
    assert entries[(0x17AA, 0x386E)][3] == ""   # no release carries it this way
    assert entries[(0x17AA, 0x3906)][3] == "6.5"  # kind unchanged, still carried


def test_re_dating_a_flipped_row_finds_the_release_carrying_the_new_kind():
    """The other half: once a release ships the re-keyed entry, that release is
    the answer — not the older ones that carried the dead PCI-keyed one."""
    recoded = QUIRK_SOURCE.replace(
        '\tSND_PCI_QUIRK(0x17aa, 0x386e, "Yoga Pro 7 14ARP8", ',
        '\tHDA_CODEC_QUIRK(0x17aa, 0x386e, "Yoga Pro 7 14ARP8", ')
    entries = build_entries(recoded, [("v7.3", recoded),
                                      ("v7.2", QUIRK_SOURCE)], _dated("6.5"))
    assert entries[(0x17AA, 0x386E)][3] == "7.3"


def test_the_walk_stops_at_the_release_window():
    """The one case that can walk deep — a helper the allowlist only just
    learned, so every row it brings in is undated at once — is railed, because
    the mirror rate-limits a long run of blob fetches."""
    feed, pulled = _counting(*[(f"v9.{n}", QUIRK_SOURCE) for n in range(40)])
    build_entries(QUIRK_SOURCE, feed, None)
    assert len(pulled) == _RELEASE_WINDOW


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
    # Lenovo, HP, Dell and ASUS all ship machines whose speaker pin upstream
    # has had to reroute.
    assert {0x17AA, 0x103C, 0x1028, 0x1043} <= {v for v, _ in entries}


def test_parse_table_refuses_a_partial_parse():
    """Refusing beats diffing against a table we only half understood."""
    src = TABLE_MODULE.read_text().replace(
        'pin="0x17", sources="', 'pin="0x17", from_widgets="', 1)
    with pytest.raises(ValueError, match="refusing to edit"):
        parse_table(src)


def test_parse_table_needs_the_literal():
    with pytest.raises(ValueError, match="no _SPEAKER_ROUTE_QUIRKS"):
        parse_table("x = 1\n")


def test_build_entries_refuses_an_implausible_parse():
    """A renamed fixup upstream would empty the table; better to fail the
    weekly run than to silently stop warning anybody."""
    with pytest.raises(ValueError, match="suspect a parse bug"):
        build_entries("no quirks here\n", [("v7.1", QUIRK_SOURCE)])


def test_apply_update_touches_only_the_table():
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    entries[(0x17AA, 0x9999)] = ("alc285-speaker2-to-dac1", "0x17", "0x02",
                                 "7.2", True)
    updated = apply_update(src, entries)
    ast.parse(updated)
    assert parse_table(updated)[1] == entries
    # Everything outside the literal is untouched.
    assert updated.replace(render_table(entries), "") == \
        src.replace(render_table(parse_table(src)[1]), "")


def test_apply_update_round_trips_an_empty_since():
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    entries[(0x17AA, 0x9998)] = ("", "0x15", "0x02 0x03", "", False)
    assert parse_table(apply_update(src, entries))[1] == entries


# --- CLI --------------------------------------------------------------------

def _run(args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "tools" /
                           "update_speaker_route_quirks.py"), *args],
                          capture_output=True, text=True, cwd=cwd)


UNDATED_ID = (0x17AA, 0x3906)
DATED_ID = (0x1028, 0x075C)


def _offline(tmp_path, master_src=QUIRK_SOURCE):
    """A copy of the shipped table plus offline sources, and the argv to
    drive them.

    ``UNDATED_ID``'s recorded series is blanked in the copy, so the fixture
    holds one entry of each kind: ``DATED_ID``, which must be carried through
    untouched, and an undated one, which must be looked up in the release.
    Blanked rather than chosen — which entries ship undated is the weekly
    refresh's to decide, and a test that picked one by name would fail the week
    it was dated.
    """
    script = tmp_path / "speaker_route_quirks.py"
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    model, pin, sources, _since, codec_only = entries[UNDATED_ID]
    entries[UNDATED_ID] = (model, pin, sources, "", codec_only)
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
    _, shipped = parse_table(before)
    dropped = next(key for key in sorted(shipped) if key not in FIXTURE_IDS)
    assert f"- {dropped[0]:04x}:{dropped[1]:04x}" in result.stdout
    assert script.read_text() == before


def test_cli_writes_with_the_flag(tmp_path):
    script, argv = _offline(tmp_path)
    assert _run([*argv, "--write"], cwd=ROOT).returncode == 0
    _, entries = parse_table(script.read_text())
    assert sorted(entries) == FIXTURE_IDS
    # An undated id present in the release fixture resolves to that release.
    assert entries[UNDATED_ID][3] == "7.1"


def test_cli_carries_a_recorded_since_but_rescan_re_derives_it(tmp_path):
    """End to end, because carrying only helps if `main` passes the shipped
    table down. DATED_ID is dated in the table and present in the release
    fixture, so a re-derivation would visibly move it to that release — which
    is what --rescan is the only way to ask for. Its recorded value is read,
    not written down here: it is machine-written and moves on upstream's
    schedule."""
    script, argv = _offline(tmp_path)
    _, shipped = parse_table(script.read_text())
    recorded = shipped[DATED_ID][3]
    assert recorded and recorded != "7.1", "fixture no longer discriminates"

    assert _run([*argv, "--write"], cwd=ROOT).returncode == 0
    assert parse_table(script.read_text())[1][DATED_ID][3] == recorded

    assert _run([*argv, "--write", "--rescan"], cwd=ROOT).returncode == 0
    assert parse_table(script.read_text())[1][DATED_ID][3] == "7.1"


def test_cli_fails_closed_on_an_implausible_parse(tmp_path):
    """A renamed fixup upstream empties the parse. The run must abort with the
    shipped table intact rather than leave users unwarned."""
    script, argv = _offline(tmp_path, master_src="nothing to parse here\n")
    before = script.read_text()

    result = _run([*argv, "--write"], cwd=ROOT)
    assert result.returncode == 1
    assert "suspect a parse bug" in result.stderr
    assert script.read_text() == before


def test_a_renamed_helper_aborts_instead_of_dropping_its_family():
    """The size rails count the whole table, so losing one helper's machines
    leaves the total inside them and the weekly PR looks clean. Most of the
    shipped rows hang off a single helper, so that has to be checked per
    helper."""
    renamed = QUIRK_SOURCE.replace("alc285_fixup_speaker2_to_dac1",
                                   "alc285_fixup_speaker2_to_dac1_v2")
    with pytest.raises(ValueError, match="no longer in the kernel source"):
        route_fixups(renamed, require_helpers=True)


def test_helper_check_is_off_for_historical_releases():
    """The check runs against mainline only. A helper legitimately does not
    exist in releases older than the one that introduced it, and treating that
    as a rename would abort every real run."""
    older = QUIRK_SOURCE.replace(
        "\t\t.v.func = alc287_fixup_bind_dacs,\n", "")
    assert route_fixups(older) is not None       # no raise without the flag
    assert route_fixups("static int foo(void) { return 0; }\n") == {}
