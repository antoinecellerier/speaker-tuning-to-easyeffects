#!/usr/bin/env python3
"""Prove a change to .py files touched only comments and docstrings.

  tools/docs/check_comment_only.py                     HEAD~1 -> HEAD
  tools/docs/check_comment_only.py REV_A [REV_B]       REV_A -> REV_B (default HEAD)
  tools/docs/check_comment_only.py --worktree [REV_A]  REV_A (default HEAD) -> the working tree
  ... -- PATH...                                       only these paths

Each changed file is parsed at both ends with `ast`.
Docstrings are dropped, and the two trees must then dump the same.
An added, deleted or renamed .py file counts as a code change.
Files other than .py are skipped.
A file that reads `__doc__` gets a note: its docstrings reach the user.
Exit status: 0 comments and docstrings only, 1 a code change, 2 a git error.
"""
import argparse
import ast
import subprocess
import sys

DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def git(*args):
    done = subprocess.run(['git', *args], capture_output=True, text=True)
    if done.returncode:
        print(done.stderr.strip(), file=sys.stderr)
        sys.exit(2)
    return done.stdout


def strip_docstrings(tree):
    for node in ast.walk(tree):
        if not isinstance(node, DOCSTRING_OWNERS) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            # An emptied body compares equal to a bare `pass`; a module may be empty.
            node.body = node.body[1:] or ([] if isinstance(node, ast.Module) else [ast.Pass()])
    return tree


def first_difference(a, b, path=''):
    """Name the first statement where two bodies differ, descending into classes."""
    for x, y in zip(a, b):
        if ast.dump(x, include_attributes=False) == ast.dump(y, include_attributes=False):
            continue
        name = getattr(x, 'name', None)
        where = f'{path}{name}' if name else f'{path}line {getattr(x, "lineno", "?")}'
        if isinstance(x, ast.ClassDef) and isinstance(y, ast.ClassDef) and x.name == y.name:
            return first_difference(x.body, y.body, where + '.') or where
        return where
    return f'{path}statement count {len(a)} -> {len(b)}' if len(a) != len(b) else None


def compare(old, new):
    """None when OLD and NEW differ only in comments and docstrings, else where they differ."""
    a, b = strip_docstrings(ast.parse(old)), strip_docstrings(ast.parse(new))
    if ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False):
        return None
    return first_difference(a.body, b.body) or 'module'


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('revs', nargs='*', metavar='REV')
    ap.add_argument('--worktree', action='store_true',
                    help='compare REV_A (default HEAD) with the working tree')
    argv = list(argv)
    cut = argv.index('--') if '--' in argv else len(argv)
    a, paths = ap.parse_args(argv[:cut]), argv[cut + 1:]
    if len(a.revs) > (1 if a.worktree else 2):
        ap.error('too many revisions')
    if a.worktree:
        old_rev, new_rev = (a.revs or ['HEAD'])[0], None
    else:
        old_rev = a.revs[0] if a.revs else 'HEAD~1'
        new_rev = a.revs[1] if len(a.revs) > 1 else 'HEAD'
    top = git('rev-parse', '--show-toplevel').strip()
    # Pathspecs are relative to the current directory; the names printed are not.
    diff = git('diff', '--no-renames', '--name-status', old_rev,
               *([new_rev] if new_rev else []), '--', *(paths or [top]))
    bad = 0
    for row in diff.splitlines():
        status, name = row.split('\t', 1)
        if not name.endswith('.py'):
            continue
        if status != 'M':
            print(f'{name}: {dict(A="added", D="deleted").get(status, "status " + status)}')
            bad += 1
            continue
        old = git('-C', top, 'show', f'{old_rev}:{name}')
        if new_rev:
            new = git('-C', top, 'show', f'{new_rev}:{name}')
        else:
            with open(f'{top}/{name}', encoding='utf-8') as f:
                new = f.read()
        try:
            where = compare(old, new)
        except SyntaxError as e:
            where = f'does not parse ({e.msg}, line {e.lineno})'
        if where:
            print(f'{name}: code differs at {where}')
            bad += 1
        elif '__doc__' in new:
            print(f'{name}: comments and docstrings only; note it reads __doc__')
        else:
            print(f'{name}: comments and docstrings only')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
