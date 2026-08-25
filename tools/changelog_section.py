#!/usr/bin/env python3
"""Print the CHANGELOG.md section, or the release title, for a version tag.

Used by .github/workflows/release.yml to turn a pushed ``vYYYY.MM`` tag
into a GitHub Release. The heading may carry a tagline —
``## v2026.05 — Treble Restored and a PipeWire Path`` — which ``--title``
prints as the release title; a summary paragraph directly under the
heading then opens the notes. Exits non-zero when the section is missing,
or when the heading is neither shape, so the release job fails loudly
instead of publishing empty or misnamed notes.

    python3 tools/changelog_section.py v2026.05
    python3 tools/changelog_section.py v2026.05 --title
    python3 tools/changelog_section.py v2026.05 --file path/to/CHANGELOG.md
"""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# "## v2026.08" or "## v2026.08 — Tagline": one ASCII space either side of an
# em dash, so a hyphen or a doubled space fails instead of passing through.
HEADING = re.compile(r"^## (?P<version>\S+)(?: — (?P<tagline>.+))?$")
# The pre-tagline heading shape; the date belongs on the tag now.
DATE_TAGLINE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _heading_index(lines: list[str], version: str) -> int | None:
    """Index of the ``## <version> …`` line, matched on its first token."""
    for i, line in enumerate(lines):
        if line.startswith("## "):
            tokens = line[3:].split()
            if tokens and tokens[0] == version:
                return i
    return None


def parse_heading(line: str) -> tuple[str, str | None] | None:
    """Split ``## <version>[ — <tagline>]``; None if the line isn't one."""
    match = HEADING.match(line)
    return (match["version"], match["tagline"]) if match else None


def extract_section(text: str, version: str) -> str | None:
    """Return the body of the ``## <version> …`` section, or None.

    A section header is a line beginning with ``## `` whose first
    whitespace-delimited token equals ``version``. The body runs until the
    next ``## `` header (or end of file), with surrounding blank lines
    stripped.
    """
    lines = text.splitlines()
    index = _heading_index(lines, version)
    if index is None:
        return None
    start = index + 1
    end = len(lines)
    for j in range(start, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end]).strip()


def release_title(text: str, version: str) -> str | None:
    """Return the GitHub Release title for ``version``, or None if absent.

    ``v2026.05 — Treble Restored and a PipeWire Path`` where the heading
    carries a tagline, the bare version where it doesn't. Raises ValueError
    on a heading that is neither shape — notably one still carrying the
    release date, which belongs on the tag — so a forgotten retrofit or a
    stale cut procedure fails the release job instead of titling a release
    with a date.
    """
    lines = text.splitlines()
    index = _heading_index(lines, version)
    if index is None:
        return None
    parsed = parse_heading(lines[index])
    if parsed is None:
        raise ValueError(f"{lines[index]!r} is neither '## {version}' nor "
                         f"'## {version} — <tagline>'")
    found, tagline = parsed
    if tagline and DATE_TAGLINE.match(tagline):
        raise ValueError(f"{lines[index]!r} carries a date, not a tagline — "
                         "the release date lives on the tag")
    return f"{found} — {tagline}" if tagline else found


# A flush-left line that opens a Markdown block of its own — ATX heading,
# list item, blockquote — so it never folds into the line above.
BLOCK_START = re.compile(r"#{1,6}(\s|$)|[-*+][ \t]|\d+[.)][ \t]|>")


def reflow(text: str) -> str:
    """Merge soft-wrapped continuation lines into their parent line.

    GitHub renders release notes (and issues/comments) with GFM hard line
    breaks — every ``\\n`` becomes a ``<br>`` — so anything CHANGELOG.md
    wraps at 76 columns would render broken mid-sentence. A line folds into
    the one above it when it is indented (a bullet's continuation) or when
    it is flush-left prose that opens no block of its own (the summary
    paragraph's later lines); blank lines, headings and list items start a
    new line, and nothing folds into a heading. Fenced code and tables are
    not recognised — the changelog carries neither.
    """
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        previous = out[-1] if out else ""
        folds = bool(stripped) and (line[:1].isspace() or not BLOCK_START.match(line))
        if folds and previous.strip() and not previous.startswith("#"):
            out[-1] = previous.rstrip() + " " + stripped
        else:
            out.append(line)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", help="version heading to extract, e.g. v2026.05")
    parser.add_argument("--title", action="store_true",
                        help="print the release title ('v2026.05 — tagline', or the "
                             "bare version) instead of the section body")
    parser.add_argument("--file", type=Path, default=DEFAULT_CHANGELOG,
                        help="path to the changelog (default: repo CHANGELOG.md)")
    args = parser.parse_args(argv)

    try:
        text = args.file.read_text()
    except OSError as exc:
        print(f"error: cannot read {args.file}: {exc}", file=sys.stderr)
        return 2

    try:
        result = (release_title(text, args.version) if args.title
                  else extract_section(text, args.version))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result is None:
        print(f"error: no '## {args.version}' section in {args.file}", file=sys.stderr)
        return 1

    print(result if args.title else reflow(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
