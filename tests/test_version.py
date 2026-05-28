"""Provenance/version helper must degrade gracefully.

The converter is clone-and-run: it may execute from a tarball download
(no .git), or on a system with no `git` CLI installed. `get_version()`
must never crash and must fall back to "unknown" in those cases.
"""

import subprocess
from pathlib import Path

import pytest

import _version
from _version import get_version

ROOT = Path(__file__).resolve().parent.parent


def test_non_repo_dir_returns_unknown(tmp_path):
    # tmp_path is a fresh directory that is not a git checkout.
    assert get_version(tmp_path) == "unknown"


def test_no_git_cli_returns_unknown(tmp_path, monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("git")  # mimics git absent from PATH

    monkeypatch.setattr(_version.subprocess, "run", raise_not_found)
    assert get_version(tmp_path) == "unknown"


def test_git_nonzero_exit_returns_unknown(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(_version.subprocess, "run", fail)
    assert get_version(tmp_path) == "unknown"


@pytest.mark.skipif(not (ROOT / ".git").exists(), reason="not a git checkout")
def test_in_repo_returns_describe_string():
    version = get_version(ROOT)
    assert version
    assert version != "unknown"
