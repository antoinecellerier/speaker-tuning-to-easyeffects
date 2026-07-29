"""The kernel-age table updater edits shipped source unattended.

A wrong entry here is invisible: it silently changes who gets the issue #33
old-kernel warning, and nothing fails. So the tests lock two things — that
re-rendering the *current* table reproduces the file byte-for-byte (an
appended entry must be a one-line diff, never a reformat), and that every
implausible input is refused rather than written.

All offline: upstream tags come from fixture strings, never the network.
"""

import ast
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from tools.update_kernel_releases import (
    MAX_NEW_SERIES,
    apply_update,
    new_series,
    parse_table,
    render_table,
    upstream_series,
)

ROOT = Path(__file__).resolve().parent.parent
CONVERTER = ROOT / "dolby_to_easyeffects.py"
SCRIPT = ROOT / "tools" / "update_kernel_releases.py"

TODAY = date(2026, 8, 20)

# A stand-in module: the same literal shape as the converter's, short enough
# to assert against verbatim. Nine entries → three full lines, so appending
# starts a fourth.
FIXTURE = '''"""Stand-in."""

_KERNEL_SERIES_RELEASES = {
    (6, 11): "2024-09", (6, 12): "2024-11", (6, 13): "2025-01",
    (6, 14): "2025-03", (6, 15): "2025-05", (6, 16): "2025-07",
    (6, 17): "2025-09", (6, 18): "2025-11", (6, 19): "2026-02",
}

OTHER = 1
'''


# --- the live table ---------------------------------------------------------

def test_render_reproduces_the_live_table_byte_for_byte():
    """The guard against reformatting: whatever the converter ships now must
    come back out of render_table unchanged, or --write would rewrite the
    whole block instead of appending one entry."""
    src = CONVERTER.read_text()
    (start, end), entries = parse_table(src)
    assert render_table(entries) == src[start:end]


def test_parse_table_reads_the_live_entries():
    _, entries = parse_table(CONVERTER.read_text())
    assert entries[(6, 12)] == "2024-11"   # the #33 reporter's series
    assert len(entries) > 25               # grows over time; never shrinks


def test_parse_table_refuses_a_partial_parse():
    """A literal the entry regex can't fully read must abort, not silently
    yield a short table that new_series would then 'top up'."""
    broken = FIXTURE.replace('(6, 19): "2026-02"', '(6, 19): SOME_CONST')
    with pytest.raises(ValueError, match="refusing to edit"):
        parse_table(broken)


def test_parse_table_needs_the_literal():
    with pytest.raises(ValueError, match="no _KERNEL_SERIES_RELEASES"):
        parse_table("x = 1\n")


# --- rendering --------------------------------------------------------------

def test_append_starts_a_new_line_when_the_last_is_full():
    _, entries = parse_table(FIXTURE)
    assert render_table({**entries, (7, 0): "2026-04"}).endswith(
        '\n    (6, 17): "2025-09", (6, 18): "2025-11", (6, 19): "2026-02",'
        '\n    (7, 0): "2026-04",')


def test_append_extends_a_partial_line():
    _, entries = parse_table(FIXTURE)
    del entries[(6, 19)]
    assert render_table({**entries, (6, 19): "2026-02"}).endswith(
        '\n    (6, 17): "2025-09", (6, 18): "2025-11", (6, 19): "2026-02",')


def test_render_orders_by_series_not_insertion():
    out = render_table({(7, 0): "2026-04", (6, 19): "2026-02"})
    assert out == '    (6, 19): "2026-02", (7, 0): "2026-04",'


# --- upstream tag parsing ---------------------------------------------------

# Real tag dates. The CLI tests can't pin `today`, and the updater refuses a
# release month in the future, so every date here must stay in the past —
# historical facts do, invented ones don't.
TAGS = """v6.19 2026-02-08
v7.0 2026-04-12
v7.0-rc1 2026-03-01
v7.1 2026-06-14
v7.1.5 2026-07-24
v2.6.39 2011-05-18
v7.2-rc1 2026-06-28
next-20260728 2026-07-28
v7.9
"""


def test_upstream_series_keeps_only_mainline_series_tags():
    """-rc, stable point releases, three-component ancients, non-version tags
    and a dateless (lightweight) tag all sort into this list; only vX.Y finals
    are release dates."""
    assert upstream_series(TAGS) == {
        (6, 19): "2026-02", (7, 0): "2026-04", (7, 1): "2026-06",
    }


# --- choosing what to append ------------------------------------------------

def test_new_series_appends_above_the_max():
    _, table = parse_table(FIXTURE)
    assert new_series(table, upstream_series(TAGS), TODAY) == {
        (7, 0): "2026-04", (7, 1): "2026-06",
    }


def test_new_series_crosses_a_major_bump():
    """6.19 → 7.0 is the real sequence; tuple order, not arithmetic."""
    table = {(6, 18): "2025-11", (6, 19): "2026-02"}
    assert new_series(table, upstream_series(TAGS), TODAY)[(7, 0)] == "2026-04"


def test_append_only_leaves_a_gap_below_the_max_alone():
    """A hand correction (or deliberate omission) below the max must survive
    every future run."""
    _, table = parse_table(FIXTURE)
    del table[(6, 15)]
    assert (6, 15) not in new_series(table, upstream_series(TAGS), TODAY)


def test_nothing_new_is_not_an_error():
    assert new_series({(7, 1): "2026-06"}, upstream_series(TAGS), TODAY) == {}


def test_rejects_an_implausible_batch():
    """Mainline ships one series per ~9 weeks; a pile of them at once means
    the table parse went wrong, not that the watch slept for a year."""
    table = {(6, 0): "2022-10"}
    tags = "\n".join(f"v6.{n} 2023-0{n}-01" for n in range(1, MAX_NEW_SERIES + 3))
    with pytest.raises(ValueError, match="new series at once"):
        new_series(table, upstream_series(tags), TODAY)


def test_rejects_a_date_before_the_current_max():
    table = {(7, 1): "2027-01"}
    with pytest.raises(ValueError, match="out of order"):
        new_series(table, upstream_series("v7.2 2026-08-16\n"), TODAY)


def test_rejects_a_date_in_the_future():
    _, table = parse_table(FIXTURE)
    with pytest.raises(ValueError, match="in the future"):
        new_series(table, upstream_series("v7.0 2026-09-01\n"),
                   date(2026, 8, 20))


def test_refuses_an_empty_table():
    with pytest.raises(ValueError, match="empty table"):
        new_series({}, upstream_series(TAGS), TODAY)


# --- writing ----------------------------------------------------------------

def test_apply_update_touches_only_the_table():
    updated = apply_update(FIXTURE, {(7, 0): "2026-04"})
    ast.parse(updated)
    assert parse_table(updated)[1][(7, 0)] == "2026-04"
    before, after = FIXTURE.splitlines(), updated.splitlines()
    assert before[:6] == after[:6]        # docstring + first three rows
    assert before[-3:] == after[-3:]      # closing brace, blank line, OTHER


def test_apply_update_is_a_no_op_without_additions():
    assert apply_update(FIXTURE, {}) == FIXTURE


# --- CLI --------------------------------------------------------------------

def _run(tmp_path, *args):
    target = tmp_path / "converter.py"
    target.write_text(FIXTURE)
    tags = tmp_path / "tags.txt"
    tags.write_text(TAGS)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--script", str(target),
         "--offline-tags", str(tags), *args],
        capture_output=True, text=True, cwd=ROOT)
    return proc, target


def test_cli_reports_without_writing(tmp_path):
    proc, target = _run(tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == "7.0 2026-04\n7.1 2026-06\n"
    assert target.read_text() == FIXTURE


def test_cli_writes_with_the_flag(tmp_path):
    proc, target = _run(tmp_path, "--write")
    assert proc.returncode == 0
    assert parse_table(target.read_text())[1][(7, 1)] == "2026-06"


def test_cli_exits_nonzero_and_leaves_the_file_alone_on_a_bad_batch(tmp_path):
    target = tmp_path / "converter.py"
    target.write_text(FIXTURE)
    tags = tmp_path / "tags.txt"
    tags.write_text("".join(f"v7.{n} 2026-0{n}-01\n"
                            for n in range(1, MAX_NEW_SERIES + 3)))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--script", str(target),
         "--offline-tags", str(tags), "--write"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 1
    assert "new series at once" in proc.stderr
    assert target.read_text() == FIXTURE
