"""The changelog section extractor feeds GitHub Release notes.

It must return the right section and, crucially, exit non-zero when a
section is missing so the release workflow fails instead of publishing
empty notes.
"""

import subprocess
import sys
from pathlib import Path

from tools.changelog_section import extract_section, reflow

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "changelog_section.py"

SAMPLE = """# Changelog

## Unreleased

## v2026.05 — 2026-05-28
### Changed
- **[AUDIBLE]** thing one (#13)

### Added
- thing two

## v2026.01 — 2026-01-10
### Fixed
- older thing
"""


def test_extracts_named_section():
    body = extract_section(SAMPLE, "v2026.05")
    assert "thing one" in body
    assert "thing two" in body
    assert "older thing" not in body  # stops at the next heading
    assert "Unreleased" not in body


def test_extracts_unreleased_as_empty():
    assert extract_section(SAMPLE, "Unreleased") == ""


def test_missing_section_returns_none():
    assert extract_section(SAMPLE, "v1999.01") is None


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


def test_cli_exit_zero_for_present_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v2026.05", "--file", str(changelog)],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "[AUDIBLE]" in result.stdout


def test_cli_exit_nonzero_for_missing_section(tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(SAMPLE)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "v1999.01", "--file", str(changelog)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
