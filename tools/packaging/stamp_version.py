"""Stamp the git version into a built package's copy of lib/version.py.

setuptools runs these two commands at build time, through
``[tool.setuptools.cmdclass]`` in pyproject.toml. Without them, a wheel or
sdist built from a clone carries lib/version.py's archive placeholders
unexpanded, and an installed copy is no checkout ``git describe`` could read,
so ``--version`` would know only the package version. They write into the
build's copy the two values ``git archive`` writes into a source archive
(.gitattributes, export-subst), so lib/version.py reads an installed copy the
way it reads an archive, with no code of its own for it. A copy whose
placeholders are already expanded, built from a GitHub source archive or from
an sdist this stamped, is left alone.

The values match what lib/version.py asks git for in a checkout, so a build
and a run from the same tree report the same version, ``-dirty`` included.
Only a tree that is the top of its own checkout is described: an unpacked
source tree inside some other repository would otherwise be stamped with that
repository's version.

The build copy is always rewritten from the source file, stamped or not.
setuptools copies a file into ``build/`` only when the source is newer, and
a stamp makes the copy newer, so a copy left from an earlier build would
otherwise keep that build's version after new commits. The source tree is
never written: setuptools may hard-link a build copy to its source file, so
the copy is replaced, never edited in place. An editable install runs from
the source itself, where lib/version.py asks git at run time, so it is not
stamped.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

_VERSION_FILE = Path("lib", "version.py")
# Only a placeholder still unexpanded; a stamped value never matches again.
_PLACEHOLDER = re.compile(
    r'^(_ARCHIVE_(?:DESCRIBE|COMMIT)) = "\$Format:[^"\n]*\$"$', re.MULTILINE)


def _git(root: Path, *args: str) -> str | None:
    """git's trimmed output in *root*, or None if git can't answer."""
    try:
        result = subprocess.run(["git", "-C", str(root), *args],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def describe(root: Path) -> tuple[str, str] | None:
    """(``git describe`` string, short commit) for *root*, or None unless
    *root* is the top of a git checkout git can read."""
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != root.resolve():
        return None
    described = _git(root, "describe", "--tags", "--always", "--dirty")
    commit = _git(root, "rev-parse", "--short", "HEAD")
    return (described, commit) if described and commit else None


def stamp_text(text: str, described: str, commit: str) -> str:
    """*text* with both archive placeholders replaced by these values."""
    values = {"_ARCHIVE_DESCRIBE": described, "_ARCHIVE_COMMIT": commit}
    return _PLACEHOLDER.sub(
        lambda m: f'{m.group(1)} = "{values[m.group(1)]}"', text)


def stamp(copy: Path, root: Path) -> bool:
    """Rewrite the build *copy* of lib/version.py from *root*'s source,
    stamped from *root*'s checkout. False when it went unstamped: the source
    holds no placeholder (git archive already stamped it), or *root* can't
    be described, which also clears a stamp left by an earlier build."""
    if not copy.is_file():
        return False
    text = (root / _VERSION_FILE).read_text(encoding="utf-8")
    found = describe(root) if _PLACEHOLDER.search(text) else None
    if found is not None:
        text = stamp_text(text, *found)
    copy.unlink()  # a hard link to the source would carry the edit back
    copy.write_text(text, encoding="utf-8")
    return found is not None


class BuildPy(build_py):
    """``build_py``, then the wheel's copy of lib/version.py stamped."""

    def run(self) -> None:
        super().run()
        if not getattr(self, "editable_mode", False):
            stamp(Path(self.build_lib) / _VERSION_FILE, Path.cwd())


class Sdist(sdist):
    """``sdist``, then the archive's copy of lib/version.py stamped, so a
    wheel later built from it, with no git to ask, keeps the version."""

    def make_release_tree(self, base_dir, files) -> None:
        super().make_release_tree(base_dir, files)
        stamp(Path(base_dir) / _VERSION_FILE, Path.cwd())
