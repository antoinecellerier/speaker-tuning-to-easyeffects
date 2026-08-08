#!/usr/bin/env python3
"""Inventory every user-visible string, tagged with whether a git range changed it.

The input to a factual-verification sweep of the terminal copy (the
**/copy-audit** skill). Deriving the list once, here, is what keeps the
reviewers off the raw diff and off the source tree: they read a slice of one
file instead, and cite a stable id back.

    python3 tools/extract_claims.py --since v2026.07
    python3 tools/extract_claims.py --since HEAD~20 --out-dir /tmp/audit

Writes ``claims.md`` (everything, one line per string) plus the per-reviewer
slices, partitioned by the evidence each needs rather than by file — an agent
checking corpus figures should not have to load the wrapper's source.
"""

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "localresearch" / "msg-verify"

SCRIPTS = {
    "E": "dolby_to_easyeffects.py",
    "W": "dolby_to_pipewire.py",
    "C": "ee_to_pipewire.py",
}
PROSE = {"R": "README.md", "I": ".github/ISSUE_TEMPLATE/device-report.yml"}


def lib_modules() -> dict[str, str]:
    """`lib/` modules, keyed by import path — ``lib.report.messages``.

    The entry points get one letter each because there are exactly three of
    them and the ids get quoted back by hand. `lib/` is open-ended: it gains a
    module per extraction, so a letter table would run out, and a sequence
    numbered by glob order would renumber every later module the day one lands
    alphabetically ahead of it. Deriving the prefix from the module's own path
    makes it depend on nothing else — discovering a module cannot move
    another's id — and keeps the two `plugins.py` the target layout wants
    (`lib.preset.plugins`, `lib.pipewire.plugins`) apart, which a bare stem
    would not.

    It names the *module*, not the script that imports it. A helper used by
    the generator and by the converter emits one string, and the reviewer
    reading it needs to see one id: two ids would read as two sentences to fix
    independently, which is how a shared line gets fixed on one side only.
    """
    found: dict[str, str] = {}
    for path in sorted((REPO / "lib").rglob("*.py")):
        rel = path.relative_to(REPO)
        parts = rel.with_suffix("").parts
        if parts[-1] == "__init__":  # lib/report/__init__.py -> lib.report
            parts = parts[:-1]
        found[".".join(parts)] = rel.as_posix()
    return found


# Calls whose string arguments reach the terminal. `print` is included because
# a handful of sites bypass the console helpers.
EMITTERS = {"cprint", "warn", "print", "_cprint_wrapped", "_print_flag_hint",
            "_print_ask", "_print_finding_detail", "error", "die"}
# Keyword arguments that carry copy wherever they appear (argparse, Finding).
COPY_KWARGS = {"help", "description", "epilog", "detail", "ask", "metavar",
               "title", "usage"}

# Copy also lives in table entries (`_UNMODELED_FEATURES`) and inside lambdas,
# where no emitter name is in reach — so every prose-shaped literal is taken
# and the emitter column falls back to the enclosing def. Over-collecting is
# the safe direction: a non-copy row costs a reviewer one glance, a missed one
# costs the whole point of the sweep.
_CODEISH = re.compile(r"^[\w./:@%{}$*=+-]+$")
_HAS_WORDS = re.compile(r"[a-z]{3,}\s+\S")


def is_copy(text: str) -> bool:
    """Does this read as a sentence a user sees, rather than as code?"""
    return (len(text) >= 12 and _HAS_WORDS.search(text) is not None
            and _CODEISH.match(text) is None)


# `f"Note: {QUIT_EE_HINT}."` renders as its source form, and the source form is
# what the filter above then judges: a stub with no sentence in it, so a whole
# footnote never reached a reviewer. Substituting the constant's value gives
# the filter the sentence to judge — for the accept/reject decision only. The
# row keeps `{expr}`, because the reviewer is being asked about the line as it
# is written, and a row whose text silently differed from the source would be
# uncitable.
_CONST_REF = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


def module_constants(rels: list[str]) -> dict[str, str]:
    """``NAME -> "literal"``, for every module-level UPPER string constant.

    Repo-wide rather than per-file, because the definition and the sentence
    that interpolates it routinely sit in different modules —
    ``PIPEWIRE_RESTART_CMD`` is defined in `lib/pipewire/conf.py` and spelled
    into a line of copy in three others. Only a plain `str` constant bound at
    module scope counts: an f-string would need the same resolution one level
    down, and a name bound inside a function is not what `{NAME}` in a sibling
    module refers to. A name defined twice with different text is ambiguous
    here, so the probe keeps neither.
    """
    found: dict[str, str] = {}
    clashed: set[str] = set()
    for rel in rels:
        tree = ast.parse((REPO / rel).read_text())
        for node in tree.body:  # module scope only
            if not isinstance(node, ast.Assign):
                continue
            if not (isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)):
                continue
            for target in node.targets:
                if not (isinstance(target, ast.Name) and target.id.isupper()):
                    continue
                if found.get(target.id, node.value.value) != node.value.value:
                    clashed.add(target.id)
                found[target.id] = node.value.value
    return {k: v for k, v in found.items() if k not in clashed}


def changed_lines(path: str, base: str) -> set[int]:
    """New-file line numbers the range added or rewrote, from the hunk heads."""
    diff = subprocess.run(
        ["git", "diff", "-U0", f"{base}..HEAD", "--", path],
        cwd=REPO, capture_output=True, text=True).stdout
    out: set[int] = set()
    for head in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start = int(head.group(1))
        count = int(head.group(2) or 1)
        out.update(range(start, start + count))
    return out


def literal(node: ast.AST) -> str | None:
    """Flatten a string constant, an f-string, or an implicit concatenation.

    f-string placeholders keep their source (``{expr}``) so a reviewer can see
    that a value is interpolated without having to open the file.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif isinstance(v, ast.FormattedValue):
                parts.append("{" + ast.unparse(v.value) + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = literal(node.left), literal(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def enclosing(tree: ast.AST) -> dict[int, str]:
    """Line -> nearest enclosing def/class, so a claim carries its context."""
    span: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            for ln in range(node.lineno, end + 1):
                span[ln] = node.name
    return span


def harvest_script(prefix: str, rel: str, rows: list, base: str,
                   consts: dict[str, str]) -> None:
    path = REPO / rel
    tree = ast.parse(path.read_text())
    where = enclosing(tree)
    touched = changed_lines(rel, base)
    seen: set[tuple[int, str]] = set()

    # Docstrings are internal notes, except a module's, which several parsers
    # hand straight to --help.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))

    # An f-string's literal chunks are already carried by the row the whole
    # f-string produces, and `literal()` renders that row from them. Taking
    # them again as constants in their own right gave a third of the inventory
    # to truncated halves of sentences a reviewer can read whole one row up —
    # each with its own citable id, and each looking like a claim to check.
    fragments = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.Constant):
                    fragments.add(id(value))

    def add(node: ast.AST, kind: str) -> None:
        text = literal(node)
        if text is None:
            return
        text = " ".join(text.split())
        probe = " ".join(_CONST_REF.sub(
            lambda m: consts.get(m.group(1), m.group(0)), text).split())
        if not (is_copy(text) or is_copy(probe)):
            return
        line = node.lineno
        if (line, text) in seen:
            return
        seen.add((line, text))
        end = getattr(node, "end_lineno", line) or line
        rows.append({
            "prefix": prefix, "file": rel, "line": line,
            "ctx": where.get(line, "<module>"), "kind": kind,
            "changed": any(ln in touched for ln in range(line, end + 1)),
            "text": text,
        })

    # First pass names the emitter where one is in reach, so the column is
    # useful; the sweep below then picks up everything it didn't reach.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", ""))
            if name in EMITTERS:
                for arg in node.args:
                    add(arg, name)
            for kw in node.keywords:
                if kw.arg in COPY_KWARGS:
                    add(kw.value, f"{name}({kw.arg}=)" if name else kw.arg)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    add(node.value, f"const {target.id}")

    for node in ast.walk(tree):
        if isinstance(node, (ast.Constant, ast.JoinedStr)):
            if id(node) not in docstrings and id(node) not in fragments:
                add(node, "literal")


def harvest_prose(prefix: str, rel: str, rows: list, base: str,
                  only_section: str | None = None) -> None:
    touched = changed_lines(rel, base)
    lines = (REPO / rel).read_text().splitlines()
    inside = only_section is None
    for i, raw in enumerate(lines, start=1):
        text = raw.strip()
        if only_section is not None:
            if text.startswith("## "):
                inside = text.startswith(only_section)
            if not inside:
                continue
        if len(text) < 12 or " " not in text or text.startswith("```"):
            continue
        rows.append({
            "prefix": prefix, "file": rel, "line": i, "ctx": "-",
            "kind": "prose", "changed": i in touched, "text": text,
        })


# Reviewers are partitioned by the evidence they need, not by file: each one
# then loads a single corpus of facts instead of all four.
_NUMERIC = re.compile(
    r"\d|\b(every|always|never|rare|typical|usually|only|most|all devices"
    r"|any device|no device)\b", re.I)


# A `lib/` string goes to *both* source reviewers: the module is imported by
# whichever entry points want it, and nothing in the string says which. Two
# reviewers reading one shared line is the cheap failure; a line no reviewer
# owns is the one this file exists to prevent.
def _shared(row: dict) -> bool:
    return row["file"].startswith("lib/")


SLICES = {
    "slice_numbers.md": (
        "changed strings carrying a number or a universal quantifier "
        "— check against a fresh corpus sweep",
        lambda r: _NUMERIC.search(r["text"]) is not None),
    "slice_generator.md": (
        "every changed string in the generator — check against its code, "
        "and against the validated-mappings list",
        lambda r: r["prefix"] == "E" or _shared(r)),
    "slice_wrapper_docs.md": (
        "wrapper, converter, README and issue template — check against "
        "their code and against a real system",
        lambda r: r["prefix"] in "WCRI" or _shared(r)),
    "slice_changelog.md": (
        "changed CHANGELOG (Unreleased) lines — check against what ships",
        lambda r: r["prefix"] == "L"),
    "slice_all_changed.md": (
        "everything changed, for cross-surface consistency",
        lambda r: True),
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True, metavar="REV",
                    help="git revision to diff HEAD against; strings whose "
                         "lines the range touched are tagged CHANGED")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"where to write claims.md and the slices "
                         f"(default: {DEFAULT_OUT_DIR})")
    args = ap.parse_args()

    if subprocess.run(["git", "rev-parse", "--verify", args.since],
                      cwd=REPO, capture_output=True).returncode != 0:
        print(f"not a revision: {args.since}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    sources = SCRIPTS | lib_modules()
    consts = module_constants(sorted(set(sources.values())))
    for prefix, rel in sources.items():
        harvest_script(prefix, rel, rows, args.since, consts)
    for prefix, rel in PROSE.items():
        harvest_prose(prefix, rel, rows, args.since)
    harvest_prose("L", "CHANGELOG.md", rows, args.since,
                  only_section="## Unreleased")

    counters: dict[str, int] = {}
    for row in sorted(rows, key=lambda r: (r["prefix"], r["file"], r["line"])):
        n = counters[row["prefix"]] = counters.get(row["prefix"], 0) + 1
        row["id"] = f"{row['prefix']}-{n:03d}"
        row["rendered"] = (
            f"{row['id']} | {'CHANGED' if row['changed'] else '-'} | "
            f"{row['file']}:{row['line']} | {row['ctx']} | {row['kind']} | "
            f"{row['text']}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    changed = [r for r in rows if r["changed"]]
    # Rows are per *site*, so one sentence written at two places is two rows
    # and each stays citable by id. That makes the row count fall on a commit
    # that collapses a duplicate into one definition, with the rendered output
    # byte-identical: `8dcab03` dropped four rows and no claim. Counting texts
    # too gives the comparison a number that survives such a commit. Not an
    # invariant — hoisting a literal into a constant rewrites the text to
    # `{expr}`, which moves both counts and can push a row under the filters
    # above — but the two together say which of the three happened.
    distinct = len({r["text"] for r in rows})
    header = [
        "# Claim inventory — user-visible strings",
        "",
        f"Range `{args.since}..HEAD`. {len(rows)} rows over {distinct} "
        f"distinct strings, {len(changed)} tagged CHANGED.",
        "",
        "A string emitted from two sites is two rows, one per site, so a "
        "commit that collapses the duplicate into one definition shrinks "
        "the row count while printing exactly what it printed before. "
        "Compare the distinct count across a refactor: it counts sentences "
        "rather than sites. Both move when a literal is hoisted into a "
        "constant, since the text then reads `{expr}`.",
        "",
        "Columns: `ID | CHANGED | file:line | enclosing def | emitter | text`.",
        "`{expr}` marks an interpolated value. Only CHANGED rows are "
        "verification targets; the rest are here so a claim can be read "
        "against the run it prints in.",
        "",
        "Prefixes: E generator · W wrapper · C converter · R README · "
        "I issue template · L CHANGELOG (Unreleased only) · `lib.*` a "
        "shared module, named by its import path and owned by no one "
        "script.",
        "",
    ]
    (args.out_dir / "claims.md").write_text(
        "\n".join(header + [r["rendered"] for r in rows]) + "\n")
    print(f"{len(rows)} rows / {distinct} distinct strings "
          f"({len(changed)} CHANGED) -> {args.out_dir / 'claims.md'}")

    for name, (note, keep) in SLICES.items():
        picked = [r["rendered"] for r in changed if keep(r)]
        (args.out_dir / name).write_text(
            f"# {name} — {note}\n\n" + "\n".join(picked) + "\n")
        print(f"  {name}: {len(picked)} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
