"""Deterministic schema validation for a generated PipeWire filter-chain conf.

Shells out to `lv2info` for every LV2 URI referenced in a conf, parses the
per-port (Symbol, Minimum, Maximum, Default, Properties) metadata, and checks
the conf's `control = { ... }` block against it. Catches:

  - unknown ports (typo in symbol name, schema drift)
  - out-of-range values
  - inverted-bool traps on toggled ports (e.g. xm = MUTE not enable — if a
    non-Off filter type pairs with xm=1, the band is silently muted; flagged
    as an error)

Audio testing is still the final gate. This is the cheap up-front check that
catches schema-level mistakes before anyone spends ten minutes on a capture
battery, and `ee_to_pipewire.py` runs it on every conf it generates.

This module is the runtime core only: parsing and validation, no argument
parsing, no exit codes, no stdout. `run` hands back a `Report` its caller
renders; nothing here prints, because in process anything it printed would
land raw in the middle of a run's own output. It raises `RuntimeError` when a
tool it needs fails, and never calls `sys.exit`. The command-line front end
lives in `tools/measure_pw/validate_conf.py`, which owns the CLI prose and the
0/1/2 exit-code contract.

Needs `lv2info` (package names per distribution in `lib/packages.py`) and
`spa-json-dump` (ships with PipeWire) on PATH. Both are tiny, sub-millisecond
CLIs, and no PipeWire daemon is required.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# lv2info → port schema
# ---------------------------------------------------------------------------

# Frozen because a caller may memoize these across confs (`run`'s `schemas`
# argument): one `Port` can end up shared by every validation in a session, so
# a mutation anywhere would rewrite the schema everything else is checked
# against. Value-only already — nothing has ever written to one.
@dataclass(frozen=True)
class Port:
    symbol: str
    name: str
    type: str
    minimum: float | None
    maximum: float | None
    default: float | None
    toggled: bool
    # Which of Minimum/Maximum `lv2info` printed as something other than a
    # float, so the bound is `None` for a reason worth reporting. A bound
    # `lv2info` simply omits is *not* recorded — nothing was skipped there, the
    # port just has no such limit. Nor is `Default`, which no check reads.
    unparsed: tuple[str, ...] = ()


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

        # A bound that is present but not a bare float — a decimal comma from
        # a non-C locale, a unit suffix, a spelling of infinity we don't
        # expect, a future format change — used to raise `ValueError` out of
        # here and abort the whole run, taking the user's conf with it. One
        # unreadable number is not a reason to write no conf, so it degrades
        # to "this bound is unknown" and is recorded rather than swallowed.
        unparsed: list[str] = []

        def grab_float(field: str, *, checked: bool) -> float | None:
            """`lv2info`'s value for `field`, or None if it isn't a usable
            float.

            `checked` says whether `validate` range-checks a control value
            against this field. Only those are recorded in `unparsed`, because
            that list answers one question — which check did we forgo? Nothing
            reads `Port.default`, so noting an unreadable Default would report
            a range as unchecked when it had in fact been checked in full.
            """
            v = grab(field)
            if v is None:
                return None
            try:
                parsed = float(v)
            except ValueError:
                if checked:
                    unparsed.append(field)
                return None
            # NaN parses fine and then compares False against everything, so a
            # NaN bound would let the range check below pass every value while
            # reporting nothing — the same false clearance an unreadable bound
            # used to cause, by a quieter door. `inf` is deliberately left
            # alone: it is a legitimate LV2 bound and it compares correctly.
            if parsed != parsed:
                if checked:
                    unparsed.append(field)
                return None
            return parsed

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
            minimum=grab_float("Minimum", checked=True),
            maximum=grab_float("Maximum", checked=True),
            default=grab_float("Default", checked=False),
            toggled=toggled,
            # Last, so all three grab_float calls above have run.
            unparsed=tuple(unparsed),
        )
    return ports


class Lv2infoUnavailable(RuntimeError):
    """`lv2info` never got to answer: a timeout, a binary that went away
    between the PATH check and the fork, a fork that couldn't allocate.

    Split from the plain `RuntimeError` a non-zero exit raises because the two
    differ in the two ways that matter. Whether the answer can be cached: a
    non-zero exit *is* an answer about the plugin (not installed, TTL won't
    parse) and cannot change during the run, while these describe the run
    itself and say nothing about the plugin, so a caller holding a long-lived
    memo has to ask again next time. And what a caller does with it: a
    non-zero exit means the daemon will not load that plugin either, so it
    refuses the conf; these leave the plugin's ports unchecked and warn.
    """


def lv2info_schema(uri: str) -> dict[str, Port]:
    """The port metadata `lv2info` reports for one plugin URI.

    Every way the exec itself can fail arrives as one `RuntimeError` naming the
    URI. The ones that never reached a verdict arrive as the
    `Lv2infoUnavailable` subclass — the distinction the caller acts on, since
    a plain non-zero exit is lilv saying it cannot resolve this plugin and the
    daemon uses the same lilv. Only the exec is wrapped. A failure to parse
    what `lv2info` did print is our bug, and still propagates.
    """
    try:
        rc = subprocess.run(
            ["lv2info", uri], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # subprocess.TimeoutExpired is a SubprocessError, so it lands here.
        raise Lv2infoUnavailable(f"lv2info {uri!r} failed: {e}") from e
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


def _check_peq_mute(node: dict) -> list[str]:
    """Cross-check for the xm-MUTE inversion trap on an LSP para_equalizer:
    a band with an active (non-Off) filter type but `xm=1` is silently muted.
    `xm` is MUTE (0=active, 1=muted), not enable — inverting it passes the
    whole PEQ through. Returns one error string per offending band.
    """
    errors: list[str] = []
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
    return errors


def validate(nodes: list[dict], schemas: dict[str, dict[str, Port]],
             unloadable: frozenset[str] = frozenset(),
             ) -> tuple[list[str], list[str]]:
    """Check every control value against its port.

    `unloadable` names URIs `lv2info` answered "no" for. They have no schema
    either, but the caller is already refusing the conf over them, so the
    "ports went unchecked" warning below would only restate the refusal in the
    milder of the two words.
    """
    errors: list[str] = []
    warnings: list[str] = []

    for node in nodes:
        if node["type"] != "lv2":
            continue
        uri = node["plugin"]
        schema = schemas.get(uri)
        if schema is None:
            if uri not in unloadable:
                warnings.append(
                    f"{node['name']}: no lv2info schema available for {uri}; "
                    "skipping"
                )
            continue

        # Bounds `lv2info` printed unreadably, collected across this node's
        # ports and reported as one line: a check that stops checking without
        # stopping reads exactly like a pass. Raised here rather than in the
        # parser for two reasons — only a port the conf actually writes has a
        # check to forgo (a plugin exposes hundreds this conf never touches),
        # and the same misformatting usually hits every port at once, so
        # per-port lines would bury the rest of the run.
        unparsed: list[str] = []

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

            # One entry per bound, not per port: the count below is worded as
            # bounds, and a port whose Minimum *and* Maximum are both unreadable
            # has had two checks forgone, not one.
            unparsed.extend(f"{sym} {field}" for field in port.unparsed)

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

        if unparsed:
            shown = ", ".join(unparsed[:3])
            more = (f", and {len(unparsed) - 3} more"
                    if len(unparsed) > 3 else "")
            warnings.append(
                f"{node['name']}: lv2info reported {len(unparsed)} port "
                f"bound{'' if len(unparsed) == 1 else 's'} for "
                f"{uri.rsplit('/', 1)[-1]} in a form this check can't read "
                f"({shown}{more}); those values were not range-checked"
            )

        # Cross-check: filter-type non-Off must have xm=0 (active). Targets
        # the bug where xm was inverted and silently muted every band.
        if "para_equalizer" in uri:
            errors.extend(_check_peq_mute(node))

    return errors, warnings


# ---------------------------------------------------------------------------
# The whole check, as one call
# ---------------------------------------------------------------------------

# What a run can end as. Four states, named rather than numbered: they were a
# subprocess's exit codes when the check was one, and -1/0/1/2 at a call site
# said nothing about which of them must stop a conf being written.
NO_TOOLING = "no-tooling"   # neither CLI is installed, so nothing was checked
UNCHECKED = "unchecked"     # the check could not run — a skip, not a verdict
CLEAN = "clean"             # every control value matched its port
ERRORS = "errors"           # at least one did not; the conf must not be used


@dataclass
class Report:
    """One run's outcome, for the caller to render — the shape of
    ``lib.doctor.CheckResult``, and for the same reason: a module that cannot
    import ``lib.console`` can still say everything it found.

    ``errors`` and ``warnings`` are separate lists rather than one stream of
    tagged lines, so the caller can style them apart. They arrive already
    worded as sentences about the conf, with no prefix of their own: a
    ``WARN:``/``FAIL:`` tag is a thing one *program* writes for another to
    read, and there is no second program here.

    ``reason`` carries the one-line why for ``NO_TOOLING`` and ``UNCHECKED``,
    where there is nothing to list. It is empty otherwise.
    """
    status: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str = ""
    unloadable: tuple[str, ...] = ()
    # Which CLIs were absent, for a `NO_TOOLING` caller whose remedy names a
    # package. Carried rather than re-probed: a message that asks `PATH` a
    # second time can name a different tool than the one that stopped the
    # check, and the two probes are what a reader would have to reconcile.
    missing_tools: tuple[str, ...] = ()


# The budget for the whole check, not for each `lv2info` inside it. It used to
# come free: the check ran in a subprocess with a 30 s timeout around all of
# it. In process only the per-exec timeouts above are left, and those multiply
# by the number of distinct plugin URIs in the conf — six of them at ten
# seconds each is a minute of a user staring at nothing. The deadline is read
# between execs rather than threaded into them, so the worst case is this
# budget plus one hung `lv2info`, and a URI the budget cuts off is reported
# like any other schema we could not read.
_BUDGET_S = 30


def run(conf_text: str, *,
        schemas: dict[str, tuple[dict[str, Port] | None, str]] | None = None,
        ) -> Report:
    """Schema-check `conf_text` against `lv2info`'s port metadata.

    `schemas` is an optional memo of what `lv2info` answered, keyed by URI and
    **shared with every later call that is handed the same dict** — including
    the `Port` objects inside it, which is why they are frozen. A port schema
    is a property of the installed plugin, not of the conf under test, so a
    caller validating many confs in one process (the corpus tier, thousands of
    them) can pass a session dict and pay for each URI once. It is passed in
    rather than cached in here on purpose: the converter and the CLI validate
    one conf each and would never see a hit, so an `lru_cache` at module scope
    would be process-global mutable state kept for a test's benefit — the same
    dependency injection `validate(nodes, schemas)` already uses one layer up.
    Omit it and the call is exactly as it was: a fresh dict, no shared state.

    Each entry is `(schema_or_None, note)`, not a bare schema: a URI `lv2info`
    answered *about* — a non-zero exit, so the plugin is missing or its TTL
    won't parse — must not be re-exec'd per conf, but the warning that says its
    ports went unchecked has to be re-emitted for every conf it affects.
    Storing the miss without its note would report the failure once and then
    silently stop mentioning it. Only answers are memoized: an exec that never
    reached one leaves no entry, so the next conf asks again.

    **`UNCHECKED` is a skip, not a verdict**, and that is why nothing generic
    maps to it: a caller prints it dim and goes on to write the conf, and the
    corpus tier turns it into `pytest.skip`, so a bug reaching this arm would
    approve every XML in the corpus with the run still green. It is reserved
    for the failures that genuinely mean *could not run* — `spa-json-dump`
    missing, failing, or handing back something that is not JSON. A single URI
    whose `lv2info` times out or never answers is narrower than that: it
    degrades to a warning and that plugin's ports go unchecked. A bound
    `lv2info` prints in a form we can't read is narrower still: that one range
    goes unchecked, and `validate` names it. Anything else propagates.

    A URI `lv2info` *answers* for with a non-zero exit is the exception in the
    other direction: it lands in `errors` and in `unloadable`, because the
    filter-chain resolves plugins through the same lilv and will not load it
    either. See the `unloadable` note in `run`.
    """
    # Named individually, and without package names: this line used to read
    # "(install lilv-utils and pipewire)" whichever of the two was missing,
    # which told a reader whose PipeWire is plainly running to install
    # PipeWire, and disagreed with the caller's own remedy. The caller owns
    # the remedy; this says only what is not here.
    absent = [t for t in ("lv2info", "spa-json-dump") if not shutil.which(t)]
    if absent:
        return Report(NO_TOOLING,
                      reason=f"{' and '.join(absent)} not in PATH",
                      missing_tools=tuple(absent))

    deadline = time.monotonic() + _BUDGET_S
    try:
        nodes = parse_conf(conf_text)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        return Report(UNCHECKED, reason=str(e))
    if not nodes:
        return Report(UNCHECKED, reason="no filter nodes found in conf")

    # Ordered ahead of the schema warnings below, because they explain them: a
    # URI `lv2info` never answered for is why that plugin's ports went
    # unchecked, and the two lines are about the same plugin. A URI it
    # answered *no* for takes the other list — see `unloadable` below.
    tool_warnings: list[str] = []
    tool_errors: list[str] = []
    unloadable: list[str] = []
    memo = {} if schemas is None else schemas
    port_schemas: dict[str, dict[str, Port]] = {}
    for uri in {n["plugin"] for n in nodes
                if n["type"] == "lv2" and n.get("plugin")}:
        if uri not in memo:
            if time.monotonic() >= deadline:
                # Not memoized: a budget that ran out is a property of this
                # run, not of the plugin, and caching it would leave the URI
                # unchecked for the rest of the process.
                tool_warnings.append(f"lv2info {uri!r} skipped: the "
                                     f"{_BUDGET_S}s validation budget ran out")
                continue
            try:
                memo[uri] = (lv2info_schema(uri), "")
            except Lv2infoUnavailable as e:
                # Not memoized, for the same reason as the budget above: an
                # exec that never reached a verdict is a property of this run.
                # Caching it would leave the URI unchecked for every later conf
                # in the process — one `lv2info` timing out under a loaded
                # `-n auto` corpus walk would disable the schema check for the
                # rest of the walk, with every conf still reporting CLEAN.
                tool_warnings.append(str(e))
                continue
            except RuntimeError as e:
                # A non-zero exit, which is `lv2info` answering rather than
                # failing to be asked: the plugin isn't installed, or its TTL
                # won't parse. That can't change mid-run, so it is memoized and
                # the note re-emitted on every hit below. Narrow on purpose:
                # `lv2info_schema` funnels every exec failure into these two
                # types, so anything else escaping it is a bug in the parse and
                # must not be quietly downgraded to "unchecked".
                memo[uri] = (None, str(e))
        schema, note = memo[uri]
        if note:
            # An answer, not a failure to ask — so it decides the run rather
            # than annotating it. `lv2info` and PipeWire's filter-chain load
            # plugins through the same lilv; a URI lilv will not resolve here
            # is one the daemon will not resolve either, whether the plugin is
            # missing or its TTL won't parse. Writing the conf anyway leaves a
            # chain that cannot load, which the reader discovers after
            # restarting their sound server. Re-emitted on every hit, not just
            # the exec that produced it.
            unloadable.append(uri)
            tool_errors.append(note)
        if schema is not None:
            port_schemas[uri] = schema

    errors, warnings = validate(nodes, port_schemas, frozenset(unloadable))
    errors = tool_errors + errors
    return Report(ERRORS if errors else CLEAN,
                  errors=tuple(errors),
                  warnings=tuple(tool_warnings + warnings),
                  unloadable=tuple(sorted(unloadable)))
