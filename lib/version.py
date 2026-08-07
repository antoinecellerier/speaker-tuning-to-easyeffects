"""Best-effort version string derived from git, shared by both converters.

Stdlib-only on purpose: ``ee_to_pipewire.py`` imports this for ``--version``
and must not pull numpy/scipy into its startup path just to read a version.
"""

import subprocess
from pathlib import Path

__all__ = ["get_version"]

_CACHE: str | None = None


def get_version(repo_dir: Path | None = None) -> str:
    """Return a ``git describe`` version string, or ``"unknown"``.

    Never raises. Falls back to ``"unknown"`` when there is no git CLI, the
    directory is not a git checkout (e.g. a tarball download), or git fails
    for any other reason. With no tags yet, ``--always`` yields the short
    commit hash; a dirty tree gets a ``-dirty`` suffix.

    ``repo_dir`` defaults to this module's directory and exists mainly so
    tests can point the lookup at a temporary non-repo directory; results are
    only cached for the default (no-argument) call.
    """
    global _CACHE
    if repo_dir is None and _CACHE is not None:
        return _CACHE

    base = repo_dir if repo_dir is not None else Path(__file__).resolve().parent
    version = "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(base), "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            version = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        # FileNotFoundError (no git binary) is an OSError; timeouts etc. too.
        pass

    if repo_dir is None:
        _CACHE = version
    return version
