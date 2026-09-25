"""Docs ↔ argparse sync traps (2026-07 README drift audit follow-up).

Each script's full CLI listing lives in exactly two mirrored surfaces: the
argparse declarations (source of truth for names, grouping, order) and the
options list in its docs page (same group labels, same order, one bullet per
flag): docs/dolby-to-easyeffects.md, and docs/dolby-to-pipewire.md for both
PipeWire scripts.
These traps lock flag names, ordering, and group labels together so drift
shows up as a test failure instead of a doc-review find. They deliberately
don't check *claims* (defaults, valid-name lists, prose) — when either side
changes, diff `--no-color --help` against the docs bullets by hand.
"""

import re
from pathlib import Path

import dolby_to_easyeffects
import dolby_to_pipewire
import ee_to_pipewire

REPO = Path(__file__).resolve().parent.parent
EE_DOC = (REPO / "docs/dolby-to-easyeffects.md").read_text(encoding="utf-8")
PW_DOC = (REPO / "docs/dolby-to-pipewire.md").read_text(encoding="utf-8")

# Flags deliberately undocumented in the docs. Extend only for a conscious
# omission (e.g. measurement-only routing) — never to quiet a failing trap.
DOLBY_README_OMITS: set[str] = set()
EE_README_OMITS: set[str] = {"--target-object"}
WRAPPER_README_OMITS: set[str] = set()


def _parser_groups(parser, omits):
    """[(title, (flag, ...)), ...] in render order; positionals get a None
    title, -h and deliberate omissions are dropped, empty groups skipped.
    Uses argparse's private group attributes — stable across every Python
    this repo supports, and introspection beats re-parsing help text.
    """
    groups = []
    for g in parser._action_groups:
        title = None if g.title == "positional arguments" else g.title
        names = []
        for action in g._group_actions:
            name = action.option_strings[0] if action.option_strings else action.dest
            if name != "-h" and name not in omits:
                names.append(name)
        if names:
            groups.append((title, tuple(names)))
    return groups


def _readme_groups(section):
    """Parse a docs options list into the same [(label, (flag, ...))]
    shape: bold lines start a group (None before the first), each bullet
    contributes its leading backticked token(s) (slash-joined bullets like
    `--node-name NAME` / `--node-description DESC` count as two flags).
    """
    groups = []
    label, flags = None, []
    for line in section.splitlines():
        bold = re.match(r"\*\*(.+?)\*\*", line)
        if bold:
            if flags:
                groups.append((label, tuple(flags)))
            label, flags = bold.group(1).lower(), []
            continue
        bullet = re.match(r"- (`[^`]+`(?: / `[^`]+`)*)", line)
        if bullet:
            for token in re.findall(r"`([^`]+)`", bullet.group(1)):
                flags.append(token.split()[0])
    if flags:
        groups.append((label, tuple(flags)))
    return groups


def _section(text, start, end):
    """From `start` to the next `end`, or to the end of the page."""
    begin = text.index(start)
    stop = text.find(end, begin)
    return text[begin:] if stop < 0 else text[begin:stop]


def test_dolby_options_list_matches_parser():
    expected = _parser_groups(dolby_to_easyeffects.build_parser(),
                              DOLBY_README_OMITS)
    actual = _readme_groups(
        _section(EE_DOC, "## Command-line options", "\n## "))
    assert actual == expected


def test_ee_options_list_matches_parser():
    expected = _parser_groups(ee_to_pipewire.build_parser([]), EE_README_OMITS)
    actual = _readme_groups(
        _section(PW_DOC, "## `ee_to_pipewire.py` command-line options",
                 "\n## "))
    assert actual == expected


def test_wrapper_options_list_matches_parser():
    expected = _parser_groups(dolby_to_pipewire.build_parser([]),
                              WRAPPER_README_OMITS)
    actual = _readme_groups(
        _section(PW_DOC, "## `dolby_to_pipewire.py` command-line options",
                 "\n## "))
    assert actual == expected
