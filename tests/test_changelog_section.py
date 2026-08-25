"""The changelog section extractor feeds GitHub Release notes and titles.

It must return the right section and title and, crucially, exit non-zero
when a section is missing or a heading is malformed, so the release
workflow fails instead of publishing empty or misnamed notes.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools.changelog_section import (DATE_TAGLINE, extract_section, parse_heading,
                                     reflow, release_title)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "changelog_section.py"
REAL_CHANGELOG = ROOT / "CHANGELOG.md"

SAMPLE = """# Changelog

## Unreleased

## v2026.05 — Treble Restored and a PipeWire Path

Reads the speaker voicing as the percentage it is, so presets stop
sounding dull, and adds a second way to run one.

### Changed
- **[AUDIBLE]** thing one (#13)

### Added
- thing two

## v2026.01
### Fixed
- older thing
"""

# The shape retired in favour of a tagline: the date lives on the tag now.
DATED = "# Changelog\n\n## v2026.01 — 2026-01-10\n### Fixed\n- older thing\n"


def test_extracts_named_section():
    body = extract_section(SAMPLE, "v2026.05")
    assert body.startswith("Reads the speaker voicing")
    assert "thing one" in body
    assert "thing two" in body
    assert "older thing" not in body  # stops at the next heading
    assert "Unreleased" not in body


def test_extracts_unreleased_as_empty():
    assert extract_section(SAMPLE, "Unreleased") == ""


def test_missing_section_returns_none():
    assert extract_section(SAMPLE, "v1999.01") is None


def test_parse_heading_splits_version_and_tagline():
    assert parse_heading("## v2026.05 — Treble Restored") == ("v2026.05", "Treble Restored")
    assert parse_heading("## v2026.01") == ("v2026.01", None)
    assert parse_heading("- bullet") is None
    # Only the exact " — " separator counts: a hyphen or a doubled space is
    # a typo the release job should refuse, not a tagline.
    assert parse_heading("## v2026.05 - Treble Restored") is None
    assert parse_heading("## v2026.05  —  Treble Restored") is None


def test_release_title_carries_the_tagline():
    assert release_title(SAMPLE, "v2026.05") == "v2026.05 — Treble Restored and a PipeWire Path"


def test_release_title_falls_back_to_the_bare_version():
    assert release_title(SAMPLE, "v2026.01") == "v2026.01"


def test_release_title_returns_none_for_a_missing_section():
    assert release_title(SAMPLE, "v1999.01") is None


def test_release_title_rejects_a_date_shaped_tagline():
    with pytest.raises(ValueError, match="date"):
        release_title(DATED, "v2026.01")


def test_the_summary_paragraph_opens_the_extracted_body():
    # Two flush-left lines under the heading are one paragraph: they must
    # reach GitHub as one line, ahead of the first ### section.
    first_line = reflow(extract_section(SAMPLE, "v2026.05")).splitlines()[0]
    assert first_line == ("Reads the speaker voicing as the percentage it is, so presets stop "
                          "sounding dull, and adds a second way to run one.")


def test_reflow_joins_wrapped_continuations():
    # GitHub release notes hard-break every newline; a bullet wrapped onto
    # indented continuation lines must collapse to one line so it renders
    # as a single sentence rather than breaking mid-clause.
    wrapped = (
        "### Changed\n"
        "\n"
        "- **[AUDIBLE]** Brought back the treble. The voicing was applied\n"
        "  about 10x too strongly, rolling the highs off far harder than\n"
        "  Dolby intends.\n"
        "- A second, separate bullet.\n"
    )
    result = reflow(wrapped)
    assert "- **[AUDIBLE]** Brought back the treble. The voicing was applied about 10x too strongly, rolling the highs off far harder than Dolby intends." in result
    # Heading, blank line, and the distinct second bullet stay on their own lines.
    assert "### Changed" in result.splitlines()
    assert "- A second, separate bullet." in result.splitlines()
    # No indented continuation lines survive.
    assert not any(line[:1].isspace() and line.strip() for line in result.splitlines())


def test_reflow_joins_a_wrapped_paragraph_but_not_the_blocks_around_it():
    text = (
        "### Added\n"
        "Prose right under a heading starts its own line,\n"
        "and its wrapped continuation folds into it.\n"
        "- A bullet after the paragraph stays a bullet.\n"
        "\n"
        "A new paragraph after a blank line stays separate.\n"
    )
    assert reflow(text).splitlines() == [
        "### Added",
        "Prose right under a heading starts its own line, and its wrapped continuation folds into it.",
        "- A bullet after the paragraph stays a bullet.",
        "",
        "A new paragraph after a blank line stays separate.",
    ]


def run_cli(changelog: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--file", str(changelog)],
        capture_output=True, text=True,
    )


def test_cli_exit_zero_for_present_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    result = run_cli(changelog, "v2026.05")
    assert result.returncode == 0
    assert "[AUDIBLE]" in result.stdout


def test_cli_exit_nonzero_for_missing_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    assert run_cli(changelog, "v1999.01").returncode == 1


def test_cli_title_prints_the_release_title(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    result = run_cli(changelog, "v2026.05", "--title")
    assert result.returncode == 0
    assert result.stdout == "v2026.05 — Treble Restored and a PipeWire Path\n"
    assert run_cli(changelog, "v2026.01", "--title").stdout == "v2026.01\n"


def test_cli_title_exits_nonzero_for_missing_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    assert run_cli(changelog, "v1999.01", "--title").returncode == 1


def test_cli_title_exits_nonzero_for_a_date_shaped_heading(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(DATED)
    result = run_cli(changelog, "v2026.01", "--title")
    assert result.returncode == 1
    assert "date" in result.stderr


RELEASE_HEADING = re.compile(r"^## v\S+( — .+)?$")


def test_every_release_heading_in_the_real_changelog_titles_a_release():
    """The file the workflow reads, not a sample: a heading the title parser
    rejects fails the release job on a pushed tag, where CI runs no tests —
    this is the only check between a cut and the release job."""
    text = REAL_CHANGELOG.read_text()
    lines = text.splitlines()
    headings = [(i, line) for i, line in enumerate(lines) if line.startswith("## v")]
    assert headings, "CHANGELOG.md has no release sections"
    for i, line in headings:
        assert RELEASE_HEADING.match(line), f"malformed release heading: {line!r}"
        version, tagline = parse_heading(line)
        assert not (tagline and DATE_TAGLINE.match(tagline)), (
            f"{line!r}: the date belongs on the tag; the heading takes a tagline")
        assert release_title(text, version) == line.removeprefix("## ")
        if tagline:
            assert len(tagline.split()) <= 8, f"{line!r}: tagline over 8 words"
            # A tagline never comes alone: the rule pairs it with a summary
            # paragraph directly under the heading, ahead of the first ###.
            following = next(l for l in lines[i + 1:] if l.strip())
            assert not following.startswith("#"), (
                f"{line!r} has a tagline but no summary paragraph under it")
