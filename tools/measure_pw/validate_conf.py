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
  python3 ee_to_pipewire.py preset.json --dry-run \\
    | python3 tools/measure_pw/validate_conf.py -

Exit 0 = clean. Exit 1 = at least one error. Exit 2 = setup error.

Dependencies:
  - `lv2info` (Debian/Ubuntu: `lilv-utils`; Fedora: `lilv`)
  - `spa-json-dump` (ships with PipeWire ≥ 0.3.x)

Both are tiny, sub-millisecond CLIs. No PipeWire daemon required.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# lv2info → port schema
# ---------------------------------------------------------------------------

@dataclass
class Port:
    symbol: str
    name: str
    type: str
    minimum: float | None
    maximum: float | None
    default: float | None
    toggled: bool


_TOGGLE_PROP = "http://lv2plug.in/ns/lv2core#toggled"


def _parse_lv2info(text: str) -> dict[str, Port]:
    """Parse `lv2info` output to a {symbol: Port} dict.

    `lv2info` emits a tab-indented block per port:

        Port N:
            Type:       http://lv2plug.in/ns/lv2core#ControlPort
            Symbol:     ftl_0
            Name:       Filter type Left 0
            Minimum:    0.000000
            Maximum:    11.000000
            Default:    0.000000
            Properties: http://lv2plug.in/ns/lv2core#integer

    Properties may span multiple lines.
    """
    ports: dict[str, Port] = {}
    blocks = re.split(r"\n(?=\tPort \d+:)", text)
    for block in blocks:
        if not re.search(r"\tPort \d+:", block):
            continue

        def grab(field: str) -> str | None:
            m = re.search(rf"^\t\t{field}:\s*(.+?)$", block, re.MULTILINE)
            return m.group(1).strip() if m else None

        symbol = grab("Symbol")
        if not symbol:
            continue
        # The Type field in lv2info is multi-line — a port that is
        # both ControlPort and InputPort lists each on its own line,
        # often with the InputPort first. We don't care about the
        # order; we just need it to be a writable control port.
        if "ControlPort" not in block:
            continue
        type_ = "ControlPort"
        name = grab("Name") or ""

        def grab_float(field: str) -> float | None:
            v = grab(field)
            return float(v) if v is not None else None

        # Properties may continue on subsequent lines indented further
        # than the field tag — collect the whole "Properties:" multiline.
        props_match = re.search(
            r"^\t\tProperties:\s*(.+?)(?=^\n|^\t\tDesigna|\Z)",
            block, re.MULTILINE | re.DOTALL,
        )
        toggled = bool(props_match and _TOGGLE_PROP in props_match.group(1))

        ports[symbol] = Port(
            symbol=symbol,
            name=name,
            type=type_,
            minimum=grab_float("Minimum"),
            maximum=grab_float("Maximum"),
            default=grab_float("Default"),
            toggled=toggled,
        )
    return ports


def lv2info_schema(uri: str) -> dict[str, Port]:
    rc = subprocess.run(
        ["lv2info", uri], capture_output=True, text=True, timeout=10,
    )
    if rc.returncode != 0:
        raise RuntimeError(
            f"lv2info {uri!r} failed: {rc.stderr.strip() or rc.stdout.strip()}"
        )
    return _parse_lv2info(rc.stdout)


# ---------------------------------------------------------------------------
# Conf parser (shells out to spa-json-dump for a real JSON parse)
# ---------------------------------------------------------------------------

def parse_conf(text: str) -> list[dict]:
    """Return a list of {name, plugin, type, control} for each filter
    node found in the conf.

    Uses `spa-json-dump` to translate the SPA-JSON syntax into plain
    JSON, then walks the structure.
    """
    import json
    import tempfile
    # spa-json-dump mmaps the input file, so /dev/stdin doesn't work.
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=True) as tf:
        tf.write(text)
        tf.flush()
        rc = subprocess.run(
            ["spa-json-dump", tf.name],
            capture_output=True, text=True, timeout=10,
        )
    if rc.returncode != 0:
        raise RuntimeError(
            f"spa-json-dump failed: {rc.stderr.strip()}"
        )
    try:
        data = json.loads(rc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"spa-json-dump output not parseable as JSON: {e}")

    nodes: list[dict] = []
    modules = data.get("context.modules", [])
    if not isinstance(modules, list):
        return nodes
    for mod in modules:
        if not isinstance(mod, dict):
            continue
        if mod.get("name") != "libpipewire-module-filter-chain":
            continue
        graph = mod.get("args", {}).get("filter.graph", {})
        for n in graph.get("nodes", []):
            if not isinstance(n, dict):
                continue
            nodes.append({
                "type": n.get("type"),
                "name": n.get("name"),
                "plugin": n.get("plugin"),
                "control": dict(n.get("control") or {}),
            })
    return nodes


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

# Filter-type integer values from the LSP filter_types[] enum
# (para_equalizer.cpp:70). 0=Off, anything ≥1 means an active filter.
_FILTER_TYPE_OFF = 0


def validate(nodes: list[dict], schemas: dict[str, dict[str, Port]]
             ) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    for node in nodes:
        if node["type"] != "lv2":
            continue
        uri = node["plugin"]
        schema = schemas.get(uri)
        if schema is None:
            warnings.append(
                f"{node['name']}: no lv2info schema available for {uri}; "
                "skipping"
            )
            continue

        for sym, value in node["control"].items():
            port = schema.get(sym)
            if port is None:
                errors.append(
                    f"{node['name']}: unknown port symbol {sym!r} for "
                    f"{uri.rsplit('/', 1)[-1]}"
                )
                continue

            if not isinstance(value, (int, float, bool)):
                errors.append(
                    f"{node['name']}: {sym}={value!r} is not numeric"
                )
                continue
            v = float(value)

            if port.minimum is not None and v < port.minimum - 1e-6:
                errors.append(
                    f"{node['name']}: {sym}={v} is below the port minimum "
                    f"{port.minimum} ({port.name!r})"
                )
            if port.maximum is not None and v > port.maximum + 1e-6:
                errors.append(
                    f"{node['name']}: {sym}={v} exceeds the port maximum "
                    f"{port.maximum} ({port.name!r})"
                )
            if port.toggled and v not in (0.0, 1.0):
                errors.append(
                    f"{node['name']}: {sym}={v} on a toggled port "
                    f"(must be 0 or 1)"
                )

        # Cross-check: filter-type non-Off must have xm=0 (active).
        # Targets the bug where xm was inverted and silently muted
        # every band.
        if "para_equalizer" in uri:
            ctrl = node["control"]
            for i in range(32):
                for side in ("l", "r"):
                    ft = ctrl.get(f"ft{side}_{i}")
                    xm = ctrl.get(f"xm{side}_{i}")
                    if ft is None or xm is None:
                        continue
                    if ft != _FILTER_TYPE_OFF and xm == 1:
                        errors.append(
                            f"{node['name']}: band {i} ({side}) has filter "
                            f"type {ft} but xm{side}_{i}=1. xm is MUTE "
                            "(0=active, 1=muted), so this band is "
                            "silently bypassed despite being declared as "
                            "an active filter type."
                        )

    return errors, warnings


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

    if not subprocess.run(["which", "lv2info"], capture_output=True).returncode == 0:
        print("error: lv2info not in PATH (install lilv-utils / lv2info)",
              file=sys.stderr)
        return 2

    nodes = parse_conf(text)
    if not nodes:
        print("error: no filter nodes found in conf", file=sys.stderr)
        return 2

    # Build schema cache
    uris = {n["plugin"] for n in nodes
            if n["type"] == "lv2" and n.get("plugin")}
    schemas: dict[str, dict[str, Port]] = {}
    for uri in uris:
        try:
            schemas[uri] = lv2info_schema(uri)
        except RuntimeError as e:
            print(f"warning: {e}", file=sys.stderr)

    if not args.quiet:
        print(f"parsed {len(nodes)} nodes:", file=sys.stderr)
        for n in nodes:
            ident = (n["plugin"].rsplit("/", 1)[-1] if n["type"] == "lv2"
                     else "builtin")
            print(f"  - {n['name']} [{n['type']}/{ident}] "
                  f"({len(n['control'])} controls)", file=sys.stderr)

    errors, warnings = validate(nodes, schemas)
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
