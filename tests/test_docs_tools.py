"""The gates in tools/docs/ that check a prose rewrite loses no data."""

import textwrap

from tools.docs import check_comment_only, docinv, docstats, docwrap

DOC = """# Chain

The regulator reads `threshold_high` from the XML.

| Plugin | Field | Level |
|---|---|---|
| Convolver | `ieq_balanced` | −6 dB |
| Limiter | `volmax` | −1 dBFS |

It was validated in Finding 9 on 2026-06-13 (issue #44).
"""


def test_docinv_flags_a_deleted_table_row():
    """Deleting a row loses its figure and its field name."""
    cut = DOC.replace("| Convolver | `ieq_balanced` | −6 dB |\n", "")
    assert set(docinv.missing(DOC, cut)) == {"ieq_balanced", "-6dB"}


def test_docinv_passes_a_rewording_that_keeps_every_token():
    """A rewording that keeps the data leaves nothing missing either way."""
    reworded = (DOC.replace("The regulator reads `threshold_high` from the XML.",
                            "`threshold_high` comes from the XML.")
                .replace("It was validated in Finding 9 on 2026-06-13 (issue #44).",
                         "Finding 9 validated it on\n2026-06-13, in issue #44."))
    assert docinv.missing(DOC, reworded) == {}
    assert docinv.missing(reworded, DOC) == {}


def test_docinv_does_not_find_a_figure_inside_a_longer_one():
    """A 20 dB cut rewritten as 120 dB is a lost token, not a present one."""
    assert "20dB" in docinv.missing("a 20 dB cut", "a 120 dB cut")


def test_docinv_counts_mode_sees_a_deleted_repeat():
    """A token still present elsewhere is listed with its drop when counts are on."""
    old = "Use `volmax` here.\n\nAnd `volmax` there.\n"
    new = "Use `volmax` here.\n"
    assert docinv.missing(old, new) == {}
    assert docinv.missing(old, new, counts=True) == {"volmax": "2->1"}


def test_docinv_counts_mode_sees_a_deleted_hedge():
    """A deleted hedge sentence shows under counts, and a rewording that keeps
    the hedges does not."""
    old = "The regulator maps `threshold_high`. The mapping is a hypothesis, measured on one device.\n"
    cut = "The regulator maps `threshold_high`.\n"
    kept = ("The regulator maps `threshold_high`. Measured on one device, the mapping\n"
            "stays a hypothesis.\n")
    assert docinv.missing(old, cut) == {}
    assert docinv.missing(old, cut, counts=True) == {
        "[hedge]hypothesis": "1->0", "[hedge]onedevice": "1->0", "[hedge]onone": "1->0"}
    assert docinv.missing(old, kept, counts=True) == {}
    assert docinv.missing(kept, old, counts=True) == {}


def test_docinv_counts_mode_sees_an_invented_universal_in_reverse():
    """Run NEW against OLD, a quantifier the rewrite added is reported."""
    old, new = "Some devices clip.\n", "Every device clips.\n"
    assert docinv.missing(old, new, counts=True) == {}
    assert docinv.missing(new, old, counts=True) == {"[quantifier]every": "1->0"}


def test_docinv_counts_nothing_for_a_rewrap():
    """Moving line breaks inside a paragraph changes no count, in either direction."""
    old = ("Finding 9 holds on one device at 35 Hz; the `ieq_balanced` curve is\n"
           "likely the same on every\nprofile (issue #44).\n")
    new = ("Finding\n9 holds on one\ndevice at 35\nHz; the `ieq_balanced` curve is likely the\n"
           "same on every profile (issue\n#44).\n")
    assert docinv.missing(old, new, counts=True) == {}
    assert docinv.missing(new, old, counts=True) == {}


SOURCE = '''"""Cites Finding 10."""
import os  # attack 150 ms, see #44


def slot():
    """Return the `volmax` slot."""
    return "--enable autogain"
'''


def test_docinv_py_mode_reads_comments_and_docstrings_only():
    """`--py` keeps comment and docstring tokens on their lines and drops code."""
    found = docinv.tokens(docinv.py_text(SOURCE))
    assert found["150ms"].lines[0][0] == 2
    assert {"#44", "Finding10", "volmax"} <= set(found)
    assert "--enableautogain" not in found


PARAGRAPH = ("An intro paragraph that is long enough to need wrapping because it "
             "runs well past the eighty column limit of the checker.")
NESTED = ("- A parent item that is long enough to need wrapping because it also "
          "runs past the column limit.\n"
          "  - A nested child item that runs long and carries a "
          "[link with spaces](https://example.com/a/b) across the edge.\n"
          "- A second parent.")
CODE = "```\n" + "code " * 20 + "\n```"
TABLE = "| a table row | " + "cell " * 20 + "|"
MARKDOWN = "\n\n".join([PARAGRAPH, NESTED, CODE, TABLE]) + "\n"


def test_docwrap_fix_is_whitespace_only_and_keeps_nesting(tmp_path, capsys):
    """The fix rewraps prose, keeps each item at its depth and leaves the link,
    the code block and the table alone."""
    link = "[link with spaces](https://example.com/a/b)"
    child = NESTED.split("\n")[1]
    naive = textwrap.wrap(child[4:], 80, initial_indent="  - ", subsequent_indent="    ")
    assert not any(link in line for line in naive), "the fixture no longer straddles"

    path = tmp_path / "doc.md"
    path.write_text(MARKDOWN)
    assert docwrap.check(str(path)) > 0
    assert docwrap.fix(str(path)) == 3
    fixed = path.read_text()
    assert docwrap.collapse(fixed) == docwrap.collapse(MARKDOWN)
    assert docwrap.check(str(path)) == 0
    capsys.readouterr()

    lines = fixed.split("\n")
    assert CODE in fixed and TABLE in lines
    assert any(link in line for line in lines)
    start = next(k for k, x in enumerate(lines) if x.startswith("  - A nested"))
    assert lines[start + 1].startswith("    ") and not lines[start + 1].startswith("     ")
    assert lines[start - 1].startswith("  ") and not lines[start - 1].startswith("  -")
    assert all(len(x) <= 80 for x in lines if x != TABLE and "code" not in x)
    assert docwrap.reflow(fixed) == (fixed, 0)


def test_docwrap_fix_never_starts_a_line_with_a_block_marker():
    """A dash, a number with a dot or a hash is glued to the word before it,
    so the wrap cannot turn prose into a list item or a heading."""
    markers = ("- ", "2. ", "# ")
    naive_hits = 0
    for n in range(10, 20):
        text = "word " * n + "then - a dash, then 2. a number, then # a hash " + "word " * 14
        naive = textwrap.wrap(text, 80, break_on_hyphens=False)
        naive_hits += sum(line.startswith(markers) for line in naive)
        fixed, _ = docwrap.reflow(text + "\n")
        assert not any(line.startswith(markers) for line in fixed.split("\n")), fixed
    assert naive_hits, "no plain wrap put a marker first; the fixture tests nothing"


OLD_CODE = '''"""Module doc."""
import os


class Stage:
    """Stage doc."""

    def gain(self, x):
        """Old doc."""
        return x + 1  # add one


def empty():
    """Only a docstring."""
'''

NEW_DOCS = '''"""Module doc, reworded."""
import os


class Stage:
    # A comment where the docstring was.

    def gain(self, x):
        return x + 1  # adds one


def empty():
    pass
'''


def test_check_comment_only_passes_a_comment_and_docstring_edit():
    """Reworded, removed and replaced docstrings and comments are not code."""
    assert check_comment_only.compare(OLD_CODE, NEW_DOCS) is None


def test_check_comment_only_fails_a_code_edit_and_names_it():
    """A changed constant is code, and the report names the method."""
    edited = NEW_DOCS.replace("x + 1", "x + 2")
    assert check_comment_only.compare(OLD_CODE, edited) == "Stage.gain"


def test_docstats_counts_prose_only():
    """Code, tables and headings are not prose; a list item ends a sentence."""
    text = ("# Title words\n\nOne short sentence. A second one — with an aside (and"
            " a paren).\n\n- An item without a stop\n\n```\nnot prose\n```\n\n| a | b |\n")
    stats = docstats.stats(text)
    assert stats["prose words"] == 17
    assert stats["sentences"] == 3
    assert stats["em-dash asides"] == 1 and stats["parentheticals"] == 1
