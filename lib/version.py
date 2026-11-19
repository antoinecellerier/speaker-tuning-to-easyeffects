"""Best-effort version string, shared by both converters.

Resolved in this order: the stamp git substituted into this file when it built
a source archive, then ``git describe`` in a checkout, then an installed
package's version marked ``+unstamped``, then ``"unknown"``. The
archive stamp comes first because a substituted value proves we are *not* in a
checkout -- which also keeps an archive unpacked inside some unrelated git repo
from reporting that repo's version. ``git describe`` runs only where
`lib.paths.is_checkout` holds, for the same reason: an installed package's
``site-packages/`` can sit inside some other repository too. A package built
from a clone carries the same stamp an archive does, written by its build
(tools/packaging/stamp_version.py). One built with no git to ask, such as
from a tree with no ``.git``, falls back to pyproject.toml's ``version``,
bumped only at a release, so ``+unstamped`` says the exact commit is not
known: any commit from that release on.

Stdlib-only on purpose: ``ee_to_pipewire.py`` imports this for ``--version``
and must not pull numpy/scipy into its startup path just to read a version.
"""

from __future__ import annotations

import re
import subprocess
from importlib import metadata
from pathlib import Path

from lib import paths, tool_env

__all__ = ["get_version"]

# git-archive rewrites these two when it builds a zip/tarball -- which is what
# GitHub serves for "Source code" and "Download ZIP" -- because .gitattributes
# marks this file export-subst. In a checkout they keep their literal
# placeholder text and _archive_version() rejects them.
#
# ``tags=true`` is load-bearing: every tag but v2026.06 is lightweight, and
# without it those describe to nothing. The commit hash mirrors what
# ``describe --always`` falls back to below, for a commit with no reachable tag
# -- git substitutes an empty string for the describe placeholder there.
#
# Do NOT add another dollar-Format token to this file, not even inside a
# comment explaining the mechanism: export-subst expands the whole file, so the
# explanation would turn into a value. Name .gitattributes instead.
_ARCHIVE_DESCRIBE = "$Format:%(describe:tags=true)$"
_ARCHIVE_COMMIT = "$Format:%h$"

# Refname and hex characters only, so one predicate rejects all three
# non-values: an unexpanded placeholder (it has a "$"), the empty string git
# leaves for an undescribable commit, and any substitution that came out
# looking like something else.
_PLAUSIBLE_STAMP = re.compile(r"\A[A-Za-z0-9._+-]+\Z")

_CACHE: str | None = None


def _archive_version() -> str | None:
    """The stamp git wrote into a source archive, or None in a checkout."""
    for stamp in (_ARCHIVE_DESCRIBE, _ARCHIVE_COMMIT):
        if _PLAUSIBLE_STAMP.match(stamp):
            return stamp
    return None


def _git_describe(base: Path) -> str | None:
    """``git describe`` run in ``base``, or None if git can't answer.

    Never raises: no git CLI, not a checkout, or any other failure all come
    back as None.
    """
    try:
        result = tool_env.run(
            ["git", "-C", str(base), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError (no git binary) is an OSError; timeouts etc. too.
        return None
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


# pyproject.toml's [project] name, the installed package's metadata key.
_DISTRIBUTION = "speaker-tuning-to-easyeffects"
# Packaging normalises 2027.01 to 2027.1; the tags pad the month.
_CALVER = re.compile(r"\A(\d{4})\.(\d{1,2})((?:\.\d+)*)\Z")


def _package_version() -> str | None:
    """The installed package's version, spelled as its tag and marked
    ``+unstamped``, or None when this code was not installed as a package.

    One token, no spaces: the PipeWire doctor reads a conf's version back
    with ``\\S+`` (lib/pipewire/checks.py)."""
    try:
        raw = metadata.version(_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return None
    m = _CALVER.match(raw)
    tag = f"{m[1]}.{int(m[2]):02d}{m[3]}" if m else raw
    return f"v{tag}+unstamped"


def get_version(repo_dir: Path | None = None) -> str:
    """Return an archive stamp or a ``git describe`` string, or ``"unknown"``.

    Never raises. Falls back to ``"unknown"`` when this is neither a stamped
    archive, a git checkout git can read, nor an installed package (e.g.
    files copied out of a checkout, or a system with no git CLI). With no
    tags yet, ``--always`` yields the short commit hash; a dirty tree gets a
    ``-dirty`` suffix.

    ``repo_dir`` redirects only the ``git describe`` lookup, and exists mainly
    so tests can point it at a temporary non-repo directory; it does not
    override an archive stamp. Results are only cached for the default
    (no-argument) call.
    """
    global _CACHE
    if repo_dir is None and _CACHE is not None:
        return _CACHE

    version = _archive_version()
    if not version and repo_dir is not None:
        version = _git_describe(repo_dir)
    elif not version and paths.is_checkout():
        version = _git_describe(paths.REPO_ROOT)
    elif not version:
        version = _package_version()
    version = version or "unknown"

    if repo_dir is None:
        _CACHE = version
    return version
