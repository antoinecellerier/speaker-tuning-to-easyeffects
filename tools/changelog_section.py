#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a given version tag.

Used by .github/workflows/release.yml to turn a pushed ``vYYYY.MM`` tag
into GitHub Release notes. Exits non-zero when the section is missing, so
the release job fails loudly instead of publishing empty notes.

    python3 tools/changelog_section.py v2026.05
    python3 tools/changelog_section.py v2026.05 --file path/to/CHANGELOG.md
"""

import argparse
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def extract_section(text: str, version: str) -> str | None:
    """Return the body of the ``## <version> …`` section, or None.

    A section header is a line beginning with ``## `` whose first
    whitespace-delimited token equals ``version``. The body runs until the
    next ``## `` header (or end of file), with surrounding blank lines
    stripped.
    """
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            token = line[3:].split()[0] if line[3:].split() else ""
            if token == version:
                start = i + 1
                break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def reflow(text: str) -> str:
    """Merge soft-wrapped continuation lines into their parent line.

    GitHub renders release notes (and issues/comments) with GFM hard line
    breaks — every ``\\n`` becomes a ``<br>`` — so a bullet wrapped across
    several physical lines in CHANGELOG.md would render broken mid-sentence.
    Each line indented with leading whitespace is a soft-wrap continuation
    of the line above it, so fold it back in; headings, blank lines, and
    new ``- `` list items (all unindented) are left untouched.
    """
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1].isspace() and out and out[-1].strip():
            out[-1] = out[-1].rstrip() + " " + line.strip()
        else:
            out.append(line)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="version heading to extract, e.g. v2026.05")
    parser.add_argument("--file", type=Path, default=DEFAULT_CHANGELOG,
                        help="path to the changelog (default: repo CHANGELOG.md)")
    args = parser.parse_args(argv)

    try:
        text = args.file.read_text()
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    section = extract_section(text, args.version)
    if section is None:
        print(f"error: no '## {args.version}' section in {args.file}", file=sys.stderr)
        return 1

    print(reflow(section))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
