#!/usr/bin/env python3
"""Show what a run's end-of-run output looks like for each interesting corpus XML.

The closing block is copy, and copy only fails in ways you see. The suite
traps its structure — one sentence per ask, no stray URL, a clean run
collapsing to the ask alone — but it cannot tell you that a sentence reads
badly, that two findings say nearly the same thing, or that a section is
longer than anyone will read.

So: for each pattern a finding can be raised by, find a corpus XML that
actually raises it and print the tail of a real run. Nothing is written —
every run is --dry-run.

    tools/preview_output.py                  # one example per pattern
    tools/preview_output.py --list           # which XML matches what, no runs
    tools/preview_output.py loudness-untamed     # just that pattern
    tools/preview_output.py --all-profiles   # add flags to every run
    tools/preview_output.py --full           # whole run, not just the tail

Patterns are keyed by finding slug, so this file needs a new entry only when
a finding is added — and if one is added without an entry, --list says so.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dolby_to_easyeffects as d                                # noqa: E402
from lib.dax import parse                                       # noqa: E402
from corpus_audit import discover_roots, discover_xmls              # noqa: E402


# How to recognise, from a parsed tuning alone, that a run on this XML would
# raise a given finding. Keyed by slug so the coverage check below is exact.
#
# These deliberately re-derive the condition rather than calling into the
# generator: a predicate that just asked "did the finding fire?" would go on
# agreeing with the code after both drifted, and the point of eyeballing is to
# catch the case where the code is doing something you didn't intend.
def _predicates():
    def has_regulator(t):
        return t.regulator and t.volmax_boost > 0

    def inert(t):
        return (has_regulator(t)
                and all(x >= 0 for x in t.regulator["threshold_high"]))

    def unlimited(t):
        if not has_regulator(t) or inert(t):
            return False
        left = [x / parse.DB_FIXED_POINT_SCALE for x in t.ao_left]
        right = [x / parse.DB_FIXED_POINT_SCALE for x in t.ao_right]
        if not left:
            return False
        peak = max(range(len(left)), key=lambda i: max(left[i], right[i]))
        th = t.regulator["threshold_high"]
        return (max(left[peak], right[peak])
                >= t.geq_max_range / parse.DB_FIXED_POINT_SCALE
                and peak < len(th) and th[peak] >= 0)

    return {
        "loudness-untamed": inert,
        "boost-unlimited": unlimited,
        "profile-mismatch": lambda t: (t.default_profile
                                      and t.default_profile != t.profile_used),
        "leveler-gap": lambda t: bool(t.leveler_substages),
        "unconfirmed-by-ear": lambda t: any(
            f["type"] in (3, 6, 8) for f in t.peq_filters),
        # The watching-only XML fields, which parse_xml already turns into
        # findings — so ask the finding itself rather than restating xpaths.
        **{slug: (lambda t, s=slug: any(f.slug == s for f in t.findings))
           for slug in ("peak-level", "ieq-preset", "regulator-overdrive",
                        "regulator-relaxation", "speaker-optimizer", "virtualizer")},
    }


PREDICATES = _predicates()


def _scan(xmls, wanted, per_pattern):
    """First few XMLs matching each wanted pattern, in one pass over the corpus.

    Parsing is the expensive part (a few ms each over a few thousand files),
    so match every pattern per XML and stop once all are satisfied.
    """
    found = {slug: [] for slug in wanted}
    for path in xmls:
        if all(len(v) >= per_pattern for v in found.values()):
            break
        try:
            # parse_xml reports as it goes; this pass only wants the verdict.
            with contextlib.redirect_stdout(io.StringIO()):
                tuning = parse.parse_xml(Path(path))
        except Exception:
            continue
        for slug in wanted:
            if len(found[slug]) < per_pattern and PREDICATES[slug](tuning):
                found[slug].append(path)
    return found


def _tail(text):
    """Everything after the last per-band table.

    Anchored on where the tables stop rather than on the section headings:
    the findings raised after the profile loop print their detail *above*
    those headings, and anchoring on the headings hid exactly the half this
    tool exists to show. A line-count anchor would drift as blocks grow.
    """
    lines = text.splitlines(keepends=True)
    last_table_row = -1
    for i, line in enumerate(lines):
        if re.match(r"\s*\d+ Hz\b", line):
            last_table_row = i
    return "".join(lines[last_table_row + 1:]) if last_table_row >= 0 else text


def _run(path, extra):
    argv = [str(path), "--dry-run", "--skip-ee-check", *extra]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            d.run_cli(argv)
        except Exception as e:                       # keep going on one bad XML
            print(f"[preview] run failed: {type(e).__name__}: {e}")
    return buf.getvalue()


def _detail_above_tail(text, tail, slug):
    """The finding's inline half, when it printed before the tables.

    A finding with no ask never reaches the closing block, and the ones that
    report from inside parse_xml land in the first few lines of the run — so
    the tail alone can show a section headed `### speaker-optimizer` whose
    text never mentions it. Pull those lines forward rather than previewing a
    pattern by showing output that doesn't contain it.
    """
    if f"[{slug}]" in tail:
        return ""
    lines = text.splitlines()
    out, grabbing = [], False
    for line in lines:
        if f"[{slug}]" in line:
            grabbing = True
        elif grabbing and not line.startswith("    "):
            break
        if grabbing:
            out.append(line)
    return "\n".join(out) + "\n" if out else ""


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("patterns", nargs="*", metavar="SLUG",
                    help=f"finding slugs to preview (default: all). "
                         f"Known: {', '.join(sorted(PREDICATES))}")
    ap.add_argument("--corpus-dir", action="append", default=[],
                    help="where to look for DAX3 XMLs (default: "
                         "$ATMOS_CORPUS_DIR, else the current directory)")
    ap.add_argument("--list", action="store_true",
                    help="report which XML matches each pattern and exit, "
                         "without running any of them")
    ap.add_argument("--full", action="store_true",
                    help="print the whole run rather than the closing blocks")
    ap.add_argument("--examples", type=int, default=1, metavar="N",
                    help="how many XMLs to show per pattern (default: 1)")
    ap.add_argument("--width", type=int, metavar="COLS",
                    help="render at this terminal width instead of yours — "
                         "80 is what most users see")
    args, passthrough = ap.parse_known_args(argv)

    unknown = set(args.patterns) - set(PREDICATES)
    if unknown:
        ap.error(f"unknown pattern(s): {', '.join(sorted(unknown))}. "
                 f"Known: {', '.join(sorted(PREDICATES))}")
    wanted = args.patterns or sorted(PREDICATES)

    if args.width:
        os.environ["COLUMNS"] = str(args.width)

    roots = discover_roots(args.corpus_dir)
    xmls = sorted(discover_xmls(roots))
    if not xmls:
        print(f"No Dolby DAX3 XMLs found under: {', '.join(roots)}\n"
              "Pass --corpus-dir, set ATMOS_CORPUS_DIR, or run from a folder "
              "containing DEV_*/SOUNDWIRE*/SDW* tuning XMLs.", file=sys.stderr)
        return 1

    print(f"Scanning {len(xmls)} XMLs for {len(wanted)} pattern(s)...",
          file=sys.stderr)
    found = _scan(xmls, wanted, args.examples)

    # A slug with no example is worth saying out loud: either the corpus has
    # nothing that trips it, or the predicate here has drifted from the code.
    missing = [s for s in wanted if not found[s]]
    if missing:
        print(f"No corpus XML matched: {', '.join(missing)}", file=sys.stderr)

    if args.list:
        for slug in wanted:
            print(f"\n{slug}")
            for path in found[slug] or ["(no match)"]:
                # Full path, not basename: the basename left the caller
                # re-discovering the file just to pass it to another tool.
                print(f"  {path}")
        return 0

    rule = "─" * min(shutil.get_terminal_size((80, 24)).columns, 100)
    for slug in wanted:
        for path in found[slug]:
            print(f"\n{rule}\n### {slug}  —  {Path(path).name}\n{rule}")
            out = _run(path, passthrough)
            if args.full:
                print(out, end="")
                continue
            tail = _tail(out)
            above = _detail_above_tail(out, tail, slug)
            if above:
                print(f"(printed near the top of the run, {len(out.splitlines())}"
                      " lines above the block below)")
                print(above, end="")
            print(tail, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
