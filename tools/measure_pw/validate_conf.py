#!/usr/bin/env python3
"""Deterministic schema validation for ee_to_pipewire.py output.

Shells out to `lv2info` for every LV2 URI referenced in a generated
PipeWire `filter-chain` `.conf`, parses the per-port (Symbol, Minimum,
Maximum, Default, Properties) metadata, and checks the conf's `control
= { ... }` block against it. Catches:

  - unknown ports (typo in symbol name, schema drift)
  - out-of-range values
  - inverted-bool traps on toggled ports (e.g. xm = MUTE not enable —
    if a non-Off filter type pairs with xm=1, the band is silently
    muted; flagged as an error)

Audio testing is still the final gate. This is the cheap up-front
check that catches schema-level mistakes before you spend ten minutes
on a capture battery.

Usage:

  python3 tools/measure_pw/validate_conf.py path/to/file.conf
  ... | python3 tools/measure_pw/validate_conf.py -    # conf on stdin

ee_to_pipewire.py makes the same check on every conf it generates
(unless --no-validate), but in process via lib/pipewire/validate.py
rather than by shelling out here — so reach for this by hand to
re-check a conf already on disk. Two differences worth knowing if you
are comparing the two: this has no overall time budget, where the
library caps the whole check at 30 s and reports the URIs it cut off;
and a check that could not run at all exits 2 here, where the library
hands its caller an UNCHECKED report.

Exit 0 = clean. Exit 1 = at least one error. Exit 2 = setup error.

Dependencies:
  - `lv2info` (Debian/Ubuntu: `lilv-utils`; Fedora: `lilv`)
  - `spa-json-dump` (ships with PipeWire ≥ 0.3.x)

Both are tiny, sub-millisecond CLIs. No PipeWire daemon required.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lib.pipewire import validate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("conf", type=Path,
                    help="filter-chain .conf file (or - / /dev/stdin)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print errors")
    args = ap.parse_args()

    if str(args.conf) in ("-", "/dev/stdin"):
        text = sys.stdin.read()
    else:
        text = args.conf.read_text()

    # Both CLIs, not just `lv2info`: the conf parse below shells out to
    # `spa-json-dump` before any schema is read, so checking one of the two
    # leaves the other to surface as a traceback.
    missing = [cli for cli in ("lv2info", "spa-json-dump")
               if not shutil.which(cli)]
    if missing:
        print(f"error: {' and '.join(missing)} not in PATH "
              f"(install lilv-utils and pipewire)", file=sys.stderr)
        return 2

    try:
        nodes = validate.parse_conf(text)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        # Exit 2, not the 1 an escaping exception would give: "the check could
        # not run" is a different statement from "this conf has errors", and a
        # caller gating on `$?` acts on the difference. Same exception triple
        # `validate.run` maps to `UNCHECKED` for the same reason.
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not nodes:
        print("error: no filter nodes found in conf", file=sys.stderr)
        return 2

    # Build schema cache
    uris = {n["plugin"] for n in nodes
            if n["type"] == "lv2" and n.get("plugin")}
    schemas: dict[str, dict[str, validate.Port]] = {}
    for uri in uris:
        try:
            schemas[uri] = validate.lv2info_schema(uri)
        except RuntimeError as e:
            print(f"warning: {e}", file=sys.stderr)

    if not args.quiet:
        print(f"parsed {len(nodes)} nodes:", file=sys.stderr)
        for n in nodes:
            ident = (n["plugin"].rsplit("/", 1)[-1] if n["type"] == "lv2"
                     else "builtin")
            print(f"  - {n['name']} [{n['type']}/{ident}] "
                  f"({len(n['control'])} controls)", file=sys.stderr)

    errors, warnings = validate.validate(nodes, schemas)
    for w in warnings:
        print(f"WARN: {w}", file=sys.stderr)
    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)

    if errors:
        print(f"{len(errors)} error(s)", file=sys.stderr)
        return 1
    if not args.quiet:
        suffix = " (with warnings)" if warnings else ""
        print(f"PASS{suffix}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
