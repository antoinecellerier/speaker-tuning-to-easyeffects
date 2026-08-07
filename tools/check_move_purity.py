#!/usr/bin/env python3
"""Prove a commit is *pure code motion* — that every line it adds under
``lib/`` was already there, byte-for-byte, in a line it removed from a root
script.

The split of ``dolby_to_easyeffects.py`` and ``ee_to_pipewire.py`` into
``lib/`` runs across ~10 commits, under one rule (``docs/design-notes.md``,
"Splitting the single-file scripts"): **a move commit moves only.** No
reformatting, no renamed functions, no behaviour change riding along. That is
not tidiness — the root scripts stay put, so code leaving them is an
*extraction*, not a rename, and the only thing that can still trace it home is
``git blame -C -C`` / ``git log -C -C``. Copy detection needs the moved lines
*unchanged*: a re-wrapped argument list or a renamed local silently costs the
history of every line it touches. Eyeballing a diff does not scale over ten of
these, so this checks it.

The check is a subset test: every added target line must appear verbatim among
the removed source lines. Three kinds of added line are exempt, because a
freshly extracted module cannot avoid them:

* its **module docstring** — the ``ast`` node, not a guess at where prose ends;
* its **top-level import block** — ``ast.Import``/``ast.ImportFrom`` children
  of the module, so a parenthesised multi-line import counts whole. An import
  *inside* a function is not exempt: a deferred import is new behaviour (a
  dodged cycle) and worth seeing;
* **blank lines**, which carry no provenance.

Everything else — a new comment, a new ``__all__``, a re-indented body — is
reported with its file, line, and text, so the operator can see at a glance
whether it is a real change or something the exemptions should have caught.
Whitespace counts, and a violation that differs from a removed line by
whitespace alone says so: git's copy detection cannot follow a re-indented
block either.

The reverse direction is printed but never fails the run: source lines that
did *not* reappear under the target are code deleted rather than moved, which
is sometimes exactly what was intended and always worth reading.

    python3 tools/check_move_purity.py                 # HEAD
    python3 tools/check_move_purity.py 16c4723         # any commit
    python3 tools/check_move_purity.py --staged        # before committing
    python3 tools/check_move_purity.py --worktree      # staged + unstaged

Exit status: 0 pure, 1 impure, 2 could not check.
"""

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Where extracted code lands, and where it comes from. ``:(glob)`` stops ``*``
# at a directory boundary, so this is the root scripts and nothing deeper.
DEFAULT_TARGET = "lib/"
DEFAULT_SOURCE = ":(glob)*.py"

# git's own name for the empty tree, so a root commit diffs against something.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(*args: str) -> str:
    """Run git in this repo and return stdout. Raises on a non-zero exit."""
    done = subprocess.run(["git", "-C", str(REPO), *args],
                          check=True, capture_output=True, text=True)
    return done.stdout


# --- what is being checked --------------------------------------------------

@dataclass(frozen=True)
class Change:
    """One set of changes to check, named the two ways this tool needs it.

    ``diff_args`` positions ``git diff`` at it; ``blob_prefix`` is what to
    stick in front of a path to read that path's *post-image* — the file as it
    looks after the change — which is what the exemption parse runs on.
    ``None`` means the post-image is the working tree.
    """
    diff_args: tuple[str, ...]
    blob_prefix: str | None
    label: str


def resolve(commit: str | None, staged: bool, worktree: bool) -> Change:
    """The Change named by the CLI arguments."""
    if staged:
        return Change(("--cached",), ":", "the staged changes")
    if worktree:
        return Change(("HEAD",), None, "the working tree (staged + unstaged)")

    rev = commit or "HEAD"
    parents = git("rev-list", "--parents", "-n", "1", rev).split()
    sha, ancestors = parents[0], parents[1:]
    if len(ancestors) > 1:
        raise ValueError(f"{rev} is a merge commit — a move commit never is")
    base = ancestors[0] if ancestors else EMPTY_TREE
    subject = git("show", "-s", "--format=%s", sha).strip()
    return Change((base, sha), f"{sha}:", f"{sha[:7]} {subject}")


# --- the diff, parsed by git ------------------------------------------------

@dataclass(frozen=True)
class Line:
    """One added or removed line, at its line number on its own side."""
    path: str
    lineno: int
    text: str

    def cite(self) -> str:
        return f"{self.path}:{self.lineno}"


# ``--unified=0`` means no context lines, so every body line is a change and
# the hunk header alone carries the numbering.
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def changed_paths(change: Change, pathspec: str) -> list[str]:
    """Paths matching *pathspec* in *change*, as git resolves the pathspec.

    ``--numstat -z`` because it emits paths raw; the human-readable forms quote
    anything unusual and would need un-quoting here. Rename detection is off
    throughout: a detected rename collapses a whole-file move to the handful of
    lines that differ, and the point of this tool is to check the whole file.
    """
    out = git("diff", "--no-renames", "--numstat", "-z", *change.diff_args,
              "--", pathspec)
    return [record.split("\t")[2] for record in out.split("\0")
            if record.count("\t") == 2]


def diff_lines(change: Change, path: str) -> tuple[list[Line], list[Line]]:
    """``(added, removed)`` for one path. The path is passed to git literally,
    so a name containing glob characters is not re-interpreted."""
    text = git("diff", "--no-renames", "--no-color", "--no-ext-diff",
               "--unified=0", *change.diff_args, "--", f":(literal){path}")
    added: list[Line] = []
    removed: list[Line] = []
    old = new = 0
    in_hunk = False
    for line in text.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False          # header block; numbering not valid yet
        hunk = _HUNK.match(line)
        if hunk:
            old, new = int(hunk.group(1)), int(hunk.group(2))
            in_hunk = True
        elif not in_hunk:
            continue                 # ---/+++ headers, "Binary files ... differ"
        elif line.startswith("+"):
            added.append(Line(path, new, line[1:]))
            new += 1
        elif line.startswith("-"):
            removed.append(Line(path, old, line[1:]))
            old += 1
        # "\ No newline at end of file" is neither, and is deliberately ignored.
    return added, removed


def post_image(change: Change, path: str) -> str | None:
    """The file's content after *change*, or None if it isn't readable."""
    if change.blob_prefix is None:
        on_disk = REPO / path
        return on_disk.read_text() if on_disk.is_file() else None
    try:
        return git("show", f"{change.blob_prefix}{path}")
    except subprocess.CalledProcessError:
        return None


# --- what an extracted module may legitimately add --------------------------

def exemptions(source: str, path: str) -> tuple[dict[int, str], str | None]:
    """``{line number: reason}`` for lines a new module may add un-moved.

    Returns a warning alongside it when the file could not be parsed, so a
    silently reduced exemption set never passes for a thorough one. Comments
    are *not* exempt, including one sitting between two imports: new prose is
    new content, and this tool's job is to show it rather than decide about it.
    """
    exempt = {n: "blank" for n, line in enumerate(source.splitlines(), 1)
              if not line.strip()}
    if not path.endswith(".py"):
        return exempt, None
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return exempt, (f"{path} does not parse ({exc.msg}, line {exc.lineno}) "
                        f"— only blank lines were exempted")

    def mark(node: ast.AST, reason: str) -> None:
        for n in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            exempt[n] = reason

    head = tree.body[0] if tree.body else None
    if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)):
        mark(head, "docstring")
    for node in tree.body:                  # top level only, by design
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mark(node, "import")
    return exempt, None


# --- the verdict ------------------------------------------------------------

# The columns of the report, in order. They partition every added line:
# exempt first (a moved blank line is still counted blank), then moved, then
# whatever is left over — so the row always sums back to "added".
COLUMNS = ("docstring", "import", "blank", "moved", "unmoved")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "commit", nargs="?",
        help="commit to check (default: HEAD). Its diff against its parent is "
             "what gets checked, so it must not be a merge.")
    when = parser.add_mutually_exclusive_group()
    when.add_argument(
        "--staged", action="store_true",
        help="check what is staged instead of a commit — the gate to run "
             "before `git commit`")
    when.add_argument(
        "--worktree", action="store_true",
        help="check the working tree against HEAD (staged and unstaged both)")
    parser.add_argument(
        "--target", default=DEFAULT_TARGET, metavar="PATHSPEC",
        help=f"where extracted code lands; every line added here must have "
             f"been moved (default: {DEFAULT_TARGET})")
    parser.add_argument(
        "--source", action="append", metavar="PATHSPEC",
        help=f"where it comes from; repeatable. Add `lib/` when the move is "
             f"from one lib module to another (default: {DEFAULT_SOURCE})")
    parser.add_argument(
        "--limit", type=int, default=40, metavar="N",
        help="stop listing after N lines per section, 0 for all (default: 40)")
    parser.add_argument(
        "--show-exempt", action="store_true",
        help="also list the added lines that were exempted, and why — to "
             "audit the exemptions rather than trust them")
    args = parser.parse_args(argv)

    if args.commit and (args.staged or args.worktree):
        parser.error("name a commit or pass --staged/--worktree, not both")
    sources = args.source or [DEFAULT_SOURCE]

    try:
        change = resolve(args.commit, args.staged, args.worktree)
        target_paths = changed_paths(change, args.target)
        source_paths = [p for spec in sources for p in changed_paths(change, spec)]
    except (ValueError, subprocess.CalledProcessError) as exc:
        detail = exc
        if isinstance(exc, subprocess.CalledProcessError):
            # git's first line says what went wrong; the rest is a tutorial.
            detail = (exc.stderr.strip().splitlines() or ["git failed"])[0]
        print(f"error: {detail}", file=sys.stderr)
        return 2

    print(f"check_move_purity: {change.label}")
    print(f"  added under {args.target}  must be verbatim-removed from "
          f"{' '.join(sources)}")

    # A module written but not yet `git add`-ed is in no diff at all, so the
    # check would pass it by never seeing it — the one way this tool can
    # certify a move it did not read. Refuse instead of reporting clean.
    untracked = []
    if args.staged or args.worktree:
        untracked = [p for p in git("ls-files", "--others", "--exclude-standard",
                                    "--", args.target).splitlines() if p]
        if untracked:
            print(f"\n  {len(untracked)} untracked file(s) under {args.target} "
                  f"are in no diff, so nothing here can vouch for them:")
            for path in untracked:
                print(f"    {path}   ->  git add -N {path}")
    unchecked = (f"UNCHECKED: nothing read under {args.target} contradicts the "
                 f"removed lines, but the untracked file(s) above were never "
                 f"read at all. Track them and re-run before trusting this.")

    # Removed lines are the haystack; the source side is read whole, once.
    removed: list[Line] = []
    for path in sorted(set(source_paths)):
        removed.extend(diff_lines(change, path)[1])
    removed_text = {line.text for line in removed}
    # Same lines keyed by their stripped form, to tell "you changed this line"
    # apart from "you re-indented it" in the violation report.
    stripped_source: dict[str, Line] = {}
    for line in removed:
        stripped_source.setdefault(line.text.strip(), line)

    rows: list[tuple[str, dict[str, int]]] = []
    violations: list[Line] = []
    exempted: list[tuple[Line, str]] = []
    added_text: set[str] = set()
    notes: list[str] = []

    for path in sorted(set(target_paths)):
        added, _ = diff_lines(change, path)
        added_text.update(line.text for line in added)
        image = post_image(change, path)
        if image is None:
            exempt: dict[int, str] = {}
            notes.append(f"{path} has no readable post-image (deleted?) — "
                         f"nothing was exempted for it")
        else:
            exempt, warning = exemptions(image, path)
            if warning:
                notes.append(warning)

        counts = dict.fromkeys(COLUMNS, 0)
        for line in added:
            reason = exempt.get(line.lineno)
            if reason is not None:
                counts[reason] += 1
                exempted.append((line, reason))
            elif line.text in removed_text:
                counts["moved"] += 1
            else:
                counts["unmoved"] += 1
                violations.append(line)
        rows.append((path, counts))

    if not rows:
        # Not "pure": there is no move here to be pure. Saying so beats a
        # green verdict earned by the commit not being the kind checked — and
        # an untracked module is the likeliest reason a move looks like none.
        if untracked:
            print(f"\n{unchecked}")
            return 2
        print(f"\nNOTHING TO CHECK: this change adds no lines under "
              f"{args.target}.")
        return 0

    width = max([len(path) for path, _ in rows] + [16])
    heads = "".join(f"{name.upper() if name == 'unmoved' else name:>10}"
                    for name in COLUMNS)
    print(f"\n  {'file':<{width}}{'added':>10}{heads}")
    for path, counts in rows:
        print(f"  {path:<{width}}{sum(counts.values()):>10}"
              + "".join(f"{counts[name]:>10}" for name in COLUMNS))

    for note in notes:
        print(f"\n  note: {note}")

    if args.show_exempt and exempted:
        print(f"\nExempted as new-module boilerplate ({len(exempted)} lines):")
        emit([f"{line.cite()}  [{reason}] {line.text}"
              for line, reason in exempted], args.limit)

    # The reverse direction: informational, never a failure.
    orphans = [line for line in removed
               if line.text.strip() and line.text not in added_text]
    if orphans:
        print(f"\nRemoved but not re-added under {args.target} — deleted rather "
              f"than moved ({len(orphans)} lines, informational):")
        emit([f"{line.cite()}  {line.text}" for line in orphans], args.limit)

    if violations:
        print(f"\nIMPURE: {len(violations)} added line(s) under {args.target} "
              f"have no verbatim origin among the removed lines.")
        listing = []
        for line in violations:
            listing.append(f"{line.cite()}  {line.text}")
            near = stripped_source.get(line.text.strip())
            if near is not None:
                listing.append(f"    ^ {near.cite()} differs only in whitespace"
                               f" — invisible in a diff, still fatal to -C")
        emit(listing, args.limit)
        print("\nEither those lines changed on the way over — restore them and "
              "tidy in the commit after — or the module is new code rather "
              "than moved code, which is fine and is what this looks like.")
        return 1

    if untracked:
        print(f"\n{unchecked}")
        return 2

    print(f"\nPURE: every line added under {args.target} was removed verbatim "
          f"from {' '.join(sources)}.")
    print("Provenance still needs checking on the result — this proves the "
          "text is identical, not that git found it: "
          "`git blame -C -C <moved file>`.")
    return 0


def emit(rows: list[str], limit: int) -> None:
    """Print *rows* indented, truncated to *limit* (0 = all)."""
    for row in rows[:limit] if limit else rows:
        print(f"  {row}")
    if limit and len(rows) > limit:
        print(f"  ... and {len(rows) - limit} more (--limit 0 for all)")


if __name__ == "__main__":
    raise SystemExit(main())
