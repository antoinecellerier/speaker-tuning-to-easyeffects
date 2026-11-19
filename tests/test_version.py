"""Provenance/version helper must degrade gracefully, and stamp archives.

The converter is clone-and-run, and the copy most people click is GitHub's
"Source code" zip -- `git archive` output with no .git for `git describe` to
read. `.gitattributes` marks lib/version.py export-subst so git writes the
version in at archive time; these tests hold that wiring together and check
`get_version()` still falls back to "unknown" rather than crashing when
neither source is available.
"""

import importlib.util
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from lib import paths, tool_env, version
from lib.version import get_version

ROOT = Path(__file__).resolve().parent.parent

in_checkout = pytest.mark.skipif(
    not (ROOT / ".git").exists(), reason="not a git checkout")


@pytest.fixture
def no_archive_stamp(monkeypatch):
    """Force the archive-stamp path off, so the git fallback is under test.

    The suite ships inside the source archive, where these constants hold real
    values -- without this the fallback tests would silently assert the wrong
    thing there. Empty is what git substitutes for an undescribable commit, so
    it is a state the predicate must already reject.
    """
    monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE", "")
    monkeypatch.setattr(version, "_ARCHIVE_COMMIT", "")


def test_non_repo_dir_returns_unknown(tmp_path, no_archive_stamp):
    # tmp_path is a fresh directory that is not a git checkout.
    assert get_version(tmp_path) == "unknown"


def test_no_git_cli_returns_unknown(tmp_path, monkeypatch, no_archive_stamp):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("git")  # mimics git absent from PATH

    monkeypatch.setattr(tool_env, "run", raise_not_found)
    assert get_version(tmp_path) == "unknown"


def test_git_nonzero_exit_returns_unknown(tmp_path, monkeypatch, no_archive_stamp):
    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(tool_env, "run", fail)
    assert get_version(tmp_path) == "unknown"


@in_checkout
def test_in_repo_returns_describe_string(no_archive_stamp):
    described = get_version(ROOT)
    assert described
    assert described != "unknown"


def _default_call_describes(monkeypatch, tmp_path, *, checkout: bool):
    """`get_version()`'s default call with REPO_ROOT at *tmp_path*, and the
    folder git was asked to describe (None when it wasn't asked)."""
    if checkout:
        (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(version, "_CACHE", None)
    asked = []

    def describe(cmd, **kwargs):
        asked.append(Path(cmd[2]))
        return subprocess.CompletedProcess(cmd, 0, stdout="v1.2-3-gabc\n")

    monkeypatch.setattr(tool_env, "run", describe)
    return version.get_version(), (asked[0] if asked else None)


def test_an_installed_package_never_describes_the_repo_around_it(
        tmp_path, monkeypatch, no_archive_stamp):
    """site-packages/ can sit inside some other git repository, whose
    version this would otherwise report."""
    def no_package(name):
        raise version.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(version.metadata, "version", no_package)
    got, asked = _default_call_describes(monkeypatch, tmp_path,
                                         checkout=False)
    assert (got, asked) == ("unknown", None)


def test_a_checkout_describes_its_own_root(tmp_path, monkeypatch,
                                           no_archive_stamp):
    got, asked = _default_call_describes(monkeypatch, tmp_path,
                                         checkout=True)
    assert (got, asked) == ("v1.2-3-gabc", tmp_path)


def test_an_unstamped_install_reports_its_package_version_marked(
        tmp_path, monkeypatch, no_archive_stamp):
    """Built with no git to ask, the package's own version is all there is.
    It is bumped only at a release, so it is marked as not naming a commit,
    in one token the conf header's version parse reads back whole."""
    monkeypatch.setattr(version.metadata, "version",
                        lambda name: {"speaker-tuning-to-easyeffects":
                                      "2026.10.1"}[name])
    got, asked = _default_call_describes(monkeypatch, tmp_path,
                                         checkout=False)
    assert (got, asked) == ("v2026.10.1+unstamped", None)
    assert version._PLAUSIBLE_STAMP.match(got)


@pytest.mark.parametrize("package, tag", [
    ("2027.1", "v2027.01+unstamped"),       # normalised from "2027.01"
    ("2026.10.1", "v2026.10.1+unstamped"),
    ("2026.4.2", "v2026.04.2+unstamped"),
])
def test_an_unstamped_version_is_spelled_as_its_tag(package, tag,
                                                    monkeypatch):
    """Packaging drops the month's zero; the tags keep it, so a version
    that names a release names it the way its tag does."""
    monkeypatch.setattr(version.metadata, "version", lambda name: package)
    assert version._package_version() == tag


def test_a_checkout_never_reports_an_installed_packages_version(
        tmp_path, monkeypatch, no_archive_stamp):
    """An editable install leaves package metadata behind; the checkout's
    own git answer is the true one."""
    monkeypatch.setattr(version.metadata, "version", lambda name: "0.0.1")
    got, _ = _default_call_describes(monkeypatch, tmp_path, checkout=True)
    assert got == "v1.2-3-gabc"


def test_archive_stamp_wins_over_git(monkeypatch):
    """A substituted stamp means we are in an archive, so it outranks git.

    Not just a preference: it is what keeps an archive unpacked inside some
    unrelated checkout from reporting that checkout's version.
    """
    monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE", "v9999.99-stamped")
    assert get_version(ROOT) == "v9999.99-stamped"


def test_commit_stamp_used_when_describe_is_empty(tmp_path, monkeypatch):
    # git substitutes an empty describe for a commit with no reachable tag;
    # the hash placeholder is what --always would have given instead.
    monkeypatch.setattr(version, "_ARCHIVE_DESCRIBE", "")
    monkeypatch.setattr(version, "_ARCHIVE_COMMIT", "deadbee")
    assert get_version(tmp_path) == "deadbee"


@in_checkout
def test_checkout_leaves_the_stamp_unexpanded():
    """The two halves of the wiring, checked where they are edited.

    Fails if someone hardcodes a version over the placeholders, or drops the
    export-subst line -- either of which sends every download back to
    "unknown". Reads the real constants, so no `no_archive_stamp` here.
    """
    assert version._archive_version() is None, (
        "lib/version.py's stamp constants are no longer unexpanded "
        "placeholders -- a checkout must fall through to git describe")

    attributes = (ROOT / ".gitattributes").read_text().splitlines()
    assert any(
        line.split()[:1] == ["lib/version.py"] and "export-subst" in line
        for line in attributes if line and not line.startswith("#")
    ), ".gitattributes no longer marks lib/version.py export-subst"


@in_checkout
def test_git_archive_substitutes_the_stamp(tmp_path):
    """End-to-end: the archive GitHub serves reports a real version.

    Archives HEAD the same way GitHub does, extracts lib/version.py from the
    tar, and runs *that* copy -- pointed at a non-repo directory, so a failed
    substitution cannot be papered over by the git fallback finding this
    checkout.
    """
    describe_fmt = "--pretty=format:%(describe:tags=true)"
    probe = subprocess.run(
        ["git", "-C", str(ROOT), "log", "-1", describe_fmt],
        capture_output=True, text=True)
    expected = probe.stdout.strip()
    if probe.returncode != 0 or not expected or expected.startswith("%("):
        # git < 2.32 leaves the placeholder alone: it cannot describe here, so
        # it cannot substitute in an archive either.
        pytest.skip("git too old for the describe placeholder")
    if version._ARCHIVE_DESCRIBE not in subprocess.run(
            ["git", "-C", str(ROOT), "show", "HEAD:lib/version.py"],
            capture_output=True, text=True).stdout:
        pytest.skip("stamp not committed yet; test_checkout_leaves_the_stamp_"
                    "unexpanded covers the working tree")

    archived = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "HEAD", "lib/version.py"],
        capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archived)) as tar:
        source = tar.extractfile("lib/version.py").read()

    extracted = tmp_path / "version.py"
    extracted.write_bytes(source)
    spec = importlib.util.spec_from_file_location("archived_version", extracted)
    archived_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archived_module)

    assert archived_module.get_version(tmp_path) == expected
