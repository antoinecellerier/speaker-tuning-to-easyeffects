"""The speaker-pin quirk-table updater edits shipped source unattended.

A wrong entry here is worse than invisible: it tells a user to reconfigure
their kernel for a fixup that doesn't apply to their machine. So the tests lock
that re-rendering the *current* table reproduces the file byte-for-byte, that
the quirk parse matches what upstream's table actually says, and that every
implausible input is refused rather than written.

All offline: kernel source comes from fixture strings, never the network.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tools.update_speaker_pin_quirks import (
    MAX_ENTRIES,
    MIN_ENTRIES,
    apply_update,
    build_entries,
    parse_quirks,
    parse_table,
    release_tags,
    render_table,
)
from tools.update_speaker_pin_quirks import _FUNC_FIXUP_PINS, _RELEASE_WINDOW

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

def _dated(since, entries=None):
    """*entries* (default: the fixture's) with every row recorded as *since*."""
    entries = entries or build_entries(QUIRK_SOURCE, [("v7.1", QUIRK_SOURCE)])
    return {key: (model, pins, since, codec_only)
            for key, (model, pins, _old, codec_only) in entries.items()}


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
    assert {since for _m, _p, since, _c in entries.values()} == {"6.5"}


def test_an_undated_entry_stops_at_the_first_release_without_it():
    """A run that missed a release must still name the *oldest* one carrying
    the entry, not the newest it happens to look at — and must stop as soon as
    a release lacks it, since everything older lacks it too."""
    without = QUIRK_SOURCE.replace(
        '\tHDA_CODEC_QUIRK(0x17aa, 0x386a, "Lenovo Yoga 7 16IAP7", '
        'ALC287_FIXUP_YOGA9_14IAP7_BASS_SPK_PIN),\n', "")
    current = _dated("6.5")
    current[(0x17AA, 0x386A)] = ("m", "0x17", "", True)  # in no release yet

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
    entries[(0x17AA, 0x9999)] = ("alc287-yoga9-bass-spk-pin", "0x17", "7.2", True)
    updated = apply_update(src, entries)
    ast.parse(updated)
    assert parse_table(updated)[1] == entries
    # Everything outside the literal is untouched.
    assert updated.replace(render_table(entries), "") == \
        src.replace(render_table(parse_table(src)[1]), "")


def test_apply_update_round_trips_an_empty_since():
    src = TABLE_MODULE.read_text()
    _, entries = parse_table(src)
    entries[(0x17AA, 0x9998)] = ("", "0x14 0x17", "", False)
    assert parse_table(apply_update(src, entries))[1] == entries


# --- CLI --------------------------------------------------------------------

def _run(args, cwd):
    return subprocess.run([sys.executable, str(ROOT / "tools" /
                           "update_speaker_pin_quirks.py"), *args],
                          capture_output=True, text=True, cwd=cwd)


def _offline(tmp_path, master_src=QUIRK_SOURCE):
    """A copy of the shipped table plus offline sources, and the argv to
    drive them."""
    script = tmp_path / "speaker_pin_quirks.py"
    script.write_text(TABLE_MODULE.read_text())
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
    # Every id present in the release fixture resolves to that release.
    assert entries[(0x17AA, 0x386A)][2] == "7.1"


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
