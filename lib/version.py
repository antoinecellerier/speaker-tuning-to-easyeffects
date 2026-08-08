"""Best-effort version string, shared by both converters.

Resolved in this order: the stamp git substituted into this file when it built
a source archive, then ``git describe`` in a checkout, then ``"unknown"``. The
archive stamp comes first because a substituted value proves we are *not* in a
checkout -- which also keeps an archive unpacked inside some unrelated git repo
from reporting that repo's version.

Stdlib-only on purpose: ``ee_to_pipewire.py`` imports this for ``--version``
and must not pull numpy/scipy into its startup path just to read a version.
"""

import re
import subprocess
from pathlib import Path

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
        result = subprocess.run(
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


def get_version(repo_dir: Path | None = None) -> str:
    """Return an archive stamp or a ``git describe`` string, or ``"unknown"``.

    Never raises. Falls back to ``"unknown"`` when this is neither a stamped
    archive nor a git checkout git can read (e.g. files copied out of one, or
    a system with no git CLI). With no tags yet, ``--always`` yields the short
    commit hash; a dirty tree gets a ``-dirty`` suffix.

    ``repo_dir`` redirects only the ``git describe`` lookup, and exists mainly
    so tests can point it at a temporary non-repo directory; it does not
    override an archive stamp. Results are only cached for the default
    (no-argument) call.
    """
    global _CACHE
    if repo_dir is None and _CACHE is not None:
        return _CACHE

    base = repo_dir if repo_dir is not None else Path(__file__).resolve().parent
    version = _archive_version() or _git_describe(base) or "unknown"

    if repo_dir is None:
        _CACHE = version
    return version
