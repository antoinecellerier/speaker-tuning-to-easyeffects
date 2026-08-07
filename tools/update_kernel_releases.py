#!/usr/bin/env python3
"""Append newly-released kernel series to ``_KERNEL_SERIES_RELEASES``.

That table (in ``lib/data/kernel_releases.py``) drives the issue #33 old-kernel
hint. A series above its max is deliberately treated as recent, so a table
that stops being updated doesn't fail — it goes *silently* dead: every kernel
released after the last entry becomes permanently exempt from the 18-month
warning. Hence this updater and the weekly workflow that runs it
(``.github/workflows/kernel-release-table.yml``), which opens a PR per new
series.

The release month comes from the ``vX.Y`` tag's *tagger date* on Linus' tree.
That is the only source checked to reproduce the hand-entered table exactly;
see ``docs/design-notes.md`` (issue #33) for the one that doesn't.

Append-only by design: entries at or below the table's current max are never
rewritten, so a correction made by hand stays made.

    python3 tools/update_kernel_releases.py            # report, change nothing
    python3 tools/update_kernel_releases.py --write    # apply
"""

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

DEFAULT_SCRIPT = (Path(__file__).resolve().parent.parent
                  / "lib" / "data" / "kernel_releases.py")

# git.kernel.org ignores --filter, so a tag sweep there means a full ~5 GB
# clone; the googlesource mirror honors partial clones (~5 MB for all ~940
# tags). Same trade-off, same mirror, as .github/workflows/kernel-sound-watch.yml.
# Mirror lag on a just-pushed tag is benign: the run finds nothing and the
# next one picks it up.
MIRROR = "https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux"

# A run proposing more than this many series is a parse bug, not a backlog:
# mainline ships one series per ~9 weeks, so 4 is ~9 months of missed runs.
# Fail loudly rather than write a plausible-looking wrong table.
MAX_NEW_SERIES = 4

Series = tuple[int, int]


# --- the table in lib/data/kernel_releases.py -------------------------------

_TABLE_RE = re.compile(r"^_KERNEL_SERIES_RELEASES = \{\n(.*?)\n\}$", re.M | re.S)
_ENTRY_RE = re.compile(r'\((\d+),\s*(\d+)\):\s*"(\d{4}-\d{2})"')

# The literal is hand-wrapped three entries to a line. render_table reproduces
# that shape byte-for-byte (tests/test_kernel_releases.py locks it against the
# live file), so an appended entry shows up as a one-line diff rather than a
# whole-block reformat.
_PER_LINE = 3


def parse_table(src: str) -> tuple[tuple[int, int], dict[Series, str]]:
    """``(span, {(major, minor): "YYYY-MM"})`` for the table literal in *src*.

    ``span`` is the ``(start, end)`` offset of the dict *body* — the text
    between the braces, excluding their newlines — so a caller can splice a
    re-rendered body back in without touching anything else in the file.
    """
    m = _TABLE_RE.search(src)
    if m is None:
        raise ValueError("no _KERNEL_SERIES_RELEASES literal found")
    body = m.group(1)
    entries = {(int(a), int(b)): d for a, b, d in _ENTRY_RE.findall(body)}
    # Every "):" in the body is one entry; a mismatch means the regex silently
    # skipped a pair (reformatted literal, odd spacing) and the "current" table
    # we'd diff against would be wrong.
    if len(entries) != body.count("):"):
        raise ValueError(f"parsed {len(entries)} entries but the literal has "
                         f"{body.count('):')} — refusing to edit it")
    return (m.start(1), m.end(1)), entries


def render_table(entries: dict[Series, str]) -> str:
    """The dict body for *entries*, in the literal's hand-wrapped shape."""
    items = [f'({major}, {minor}): "{released}",'
             for (major, minor), released in sorted(entries.items())]
    return "\n".join("    " + " ".join(items[i:i + _PER_LINE])
                     for i in range(0, len(items), _PER_LINE))


# --- upstream tags ----------------------------------------------------------

# Mainline series tags only: no -rc, no stable point releases (v7.1.5, which
# live in linux-stable.git anyway), no v2.6.x three-component ancients.
_TAG_RE = re.compile(r"^v(\d+)\.(\d+)$")


def upstream_series(tag_lines: str) -> dict[Series, str]:
    """``{(major, minor): "YYYY-MM"}`` from ``<tag> <YYYY-MM-DD>`` lines."""
    found: dict[Series, str] = {}
    for line in tag_lines.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue  # lightweight tag (no tagger date) or blank line
        m = _TAG_RE.match(parts[0])
        if m and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
            found[(int(m.group(1)), int(m.group(2)))] = parts[1][:7]
    return found


def fetch_tag_lines(cache_dir: Path) -> str:
    """``<tag> <YYYY-MM-DD>`` for every tag on Linus' tree.

    One partial-clone fetch of all refs (~2 s, ~5 MB) beats per-tag lookups
    and needs no API token or rate-limit budget.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    if not (cache_dir / ".git").exists():
        subprocess.run(["git", "init", "-q", str(cache_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(cache_dir), "fetch", "-q", "--depth=1",
         "--filter=tree:0", MIRROR, "+refs/tags/*:refs/tags/*"],
        check=True)
    out = subprocess.run(
        ["git", "-C", str(cache_dir), "tag", "-l",
         "--format=%(refname:strip=2) %(taggerdate:short)"],
        check=True, capture_output=True, text=True)
    return out.stdout


# --- the update itself ------------------------------------------------------

def new_series(table: dict[Series, str], upstream: dict[Series, str],
               today: date) -> dict[Series, str]:
    """Series in *upstream* that belong after the table's current max.

    Append-only: a gap *below* the max is left alone (a deliberate hand
    correction must survive). Raises on anything that smells like a parse
    bug rather than a genuine release.
    """
    if not table:
        raise ValueError("empty table — refusing to rebuild it from scratch")
    top = max(table)
    fresh = {s: d for s, d in upstream.items() if s > top}
    if len(fresh) > MAX_NEW_SERIES:
        raise ValueError(f"{len(fresh)} new series at once "
                         f"(max {MAX_NEW_SERIES}) — suspect a parse bug")
    this_month = today.strftime("%Y-%m")
    previous = table[top]
    for series, released in sorted(fresh.items()):
        if released < previous:
            raise ValueError(f"{series[0]}.{series[1]} released {released}, "
                             f"before {previous} — dates out of order")
        if released > this_month:
            raise ValueError(f"{series[0]}.{series[1]} released {released}, "
                             f"in the future (today {this_month})")
        previous = released
    return fresh


def apply_update(src: str, additions: dict[Series, str]) -> str:
    """*src* with *additions* spliced into the table literal.

    Verifies the result parses as Python and that re-reading the table yields
    exactly the intended entries — a wrong table here silently changes who
    gets the old-kernel warning, so the write is checked, not assumed.
    """
    (start, end), table = parse_table(src)
    merged = {**table, **additions}
    updated = src[:start] + render_table(merged) + src[end:]
    ast.parse(updated)
    if parse_table(updated)[1] != merged:
        raise ValueError("rewritten table does not round-trip")
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true",
                        help="apply the additions (default: report only)")
    parser.add_argument("--script", type=Path, default=DEFAULT_SCRIPT,
                        help="file holding the table (default: the shipped "
                             "lib/data/kernel_releases.py)")
    parser.add_argument("--offline-tags", type=Path, metavar="FILE",
                        help="read '<tag> <date>' lines from FILE instead of "
                             "fetching (for tests and reruns)")
    parser.add_argument("--cache-dir", type=Path,
                        help="git dir for the tag fetch (default: a temp dir)")
    args = parser.parse_args(argv)

    try:
        src = args.script.read_text()
        _, table = parse_table(src)
        if args.offline_tags:
            tag_lines = args.offline_tags.read_text()
        elif args.cache_dir:
            tag_lines = fetch_tag_lines(args.cache_dir)
        else:
            with tempfile.TemporaryDirectory() as tmp:
                tag_lines = fetch_tag_lines(Path(tmp) / "linux-tags")
        additions = new_series(table, upstream_series(tag_lines), date.today())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not additions:
        top = max(table)
        print(f"up to date: newest known series is {top[0]}.{top[1]} "
              f"({table[top]})", file=sys.stderr)
        return 0

    if args.write:
        try:
            args.script.write_text(apply_update(src, additions))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # stdout is the machine-readable part: one "<major>.<minor> <YYYY-MM>" per
    # line, oldest first, for the workflow to name the branch and PR from.
    for (major, minor), released in sorted(additions.items()):
        print(f"{major}.{minor} {released}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
