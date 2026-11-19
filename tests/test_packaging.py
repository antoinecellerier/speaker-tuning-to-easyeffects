"""pyproject.toml's package metadata against the tree it packages.

A broken install fails nowhere else in the suite: every other test imports
from the checkout. CI installs the package and runs its commands from outside
the checkout (.github/workflows/tests.yml); these catch the drift that would
break that step, before it runs.
"""

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest

# Python 3.11+; the suite itself runs on 3.10 (tests/test_layout.py).
tomllib = pytest.importorskip("tomllib")

from tests.test_layout import ROOT_MODULES

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _requirement_names(lines) -> set[str]:
    """The distribution names in requirement lines, lower-cased."""
    names = set()
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(re.match(r"[A-Za-z0-9._-]+", line).group(0).lower())
    return names


def test_requirements_txt_and_pyproject_list_the_same_packages():
    meta = PROJECT["project"]
    declared = _requirement_names(meta["dependencies"])
    for extra in meta["optional-dependencies"].values():
        declared |= _requirement_names(extra)
    listed = _requirement_names(
        (ROOT / "requirements.txt").read_text().splitlines())
    assert listed == declared


def test_every_entry_script_is_installed_as_a_command():
    scripts = PROJECT["project"]["scripts"]
    assert set(scripts) == set(ROOT_MODULES)
    for name, target in scripts.items():
        assert target == f"{name}:run_cli"
    assert set(PROJECT["tool"]["setuptools"]["py-modules"]) == set(ROOT_MODULES)



def test_pip_refuses_the_pythons_the_code_refuses():
    from lib import packages
    floor = ".".join(map(str, packages.MIN_PYTHON))
    assert PROJECT["project"]["requires-python"] == f">={floor}"

def test_every_lib_package_is_a_regular_package():
    """`packages.find` skips a folder with no __init__.py, so its modules
    would be missing from the install while every checkout test passes."""
    assert PROJECT["tool"]["setuptools"]["packages"]["find"]["include"] == [
        "lib", "lib.*"]
    for folder in [ROOT / "lib", *(ROOT / "lib").rglob("*")]:
        if folder.is_dir() and folder.name != "__pycache__" and any(
                folder.glob("*.py")):
            assert (folder / "__init__.py").is_file(), folder


def test_lib_holds_only_python():
    """Nothing here declares package data (MANIFEST.in carries only the
    build hook, outside lib/), so a data file read beside the code could be
    missing from an installed copy while every checkout test passes."""
    strays = [p for p in _lib_files() if p.suffix != ".py"]
    assert not strays


def _lib_files() -> list[Path]:
    """The files under lib/ that ship: git's tracked list in a clone, so an
    editor or merge leftover there isn't read as one; every file but
    bytecode in a source archive, which holds only what shipped."""
    if (ROOT / ".git").exists():
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "lib"],
                                capture_output=True, text=True, check=True)
        return [ROOT / line for line in listed.stdout.splitlines()]
    return [p for p in (ROOT / "lib").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts]


# --- the build-time version stamp (tools/packaging/stamp_version.py) --------

@pytest.fixture(scope="module")
def stamper():
    pytest.importorskip("setuptools")
    path = ROOT / "tools" / "packaging" / "stamp_version.py"
    spec = importlib.util.spec_from_file_location("stamp_version", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_build_hook_is_wired_and_carried_into_an_sdist():
    cmdclass = PROJECT["tool"]["setuptools"]["cmdclass"]
    assert cmdclass == {"build_py": "tools.packaging.stamp_version.BuildPy",
                        "sdist": "tools.packaging.stamp_version.Sdist"}
    assert "include tools/packaging/stamp_version.py" in (
        ROOT / "MANIFEST.in").read_text().splitlines()


def test_the_stamp_fills_both_placeholders_lib_version_reads(stamper):
    """Run against the real lib/version.py text, so a renamed placeholder
    fails here instead of shipping unstamped."""
    from lib import version
    text = (ROOT / "lib" / "version.py").read_text()
    if "$Format:" not in text:
        pytest.skip("a source archive: git already stamped lib/version.py")
    stamped = stamper.stamp_text(text, "v1.2-3-gabc", "abc")
    assert '_ARCHIVE_DESCRIBE = "v1.2-3-gabc"' in stamped
    assert '_ARCHIVE_COMMIT = "abc"' in stamped
    assert stamped.count("$Format:") == text.count("$Format:") - 2
    assert version._PLAUSIBLE_STAMP.match("v1.2-3-gabc-dirty")


def _checkout(tmp_path) -> Path:
    root = tmp_path / "src"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "version.py").write_text(
        '_ARCHIVE_DESCRIBE = "$Format:%(describe:tags=true)$"\n'
        '_ARCHIVE_COMMIT = "$Format:%h$"\n')
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"],
                ["tag", "v9.9"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env,
                       capture_output=True)
    return root


def test_the_stamp_replaces_the_build_copy_never_the_source(stamper,
                                                            tmp_path):
    """setuptools may hard-link a build copy to its source: an in-place
    write would stamp the checkout's own lib/version.py."""
    root = _checkout(tmp_path)
    source = root / "lib" / "version.py"
    built = tmp_path / "build" / "version.py"
    built.parent.mkdir()
    os.link(source, built)
    assert stamper.stamp(built, root)
    assert '_ARCHIVE_DESCRIBE = "v9.9"' in built.read_text()
    assert "$Format:" in source.read_text()


def test_a_build_copy_left_from_an_earlier_build_is_restamped(stamper,
                                                              tmp_path):
    """setuptools re-copies a file only when its source is newer, and the
    stamp made the copy newer: a later commit must still reach it."""
    root = _checkout(tmp_path)
    built = tmp_path / "build" / "version.py"
    built.parent.mkdir()
    built.write_text('_ARCHIVE_DESCRIBE = "v0.1"\n')
    assert stamper.stamp(built, root)
    assert '_ARCHIVE_DESCRIBE = "v9.9"' in built.read_text()


def test_a_tree_inside_another_checkout_is_not_stamped(stamper, tmp_path):
    """An unpacked source tree inside some other repository would
    otherwise carry that repository's version."""
    root = _checkout(tmp_path)
    inner = root / "unpacked"
    (inner / "lib").mkdir(parents=True)
    (inner / "lib" / "version.py").write_text(
        (root / "lib" / "version.py").read_text())
    built = inner / "build" / "version.py"
    built.parent.mkdir()
    built.write_text('_ARCHIVE_DESCRIBE = "v0.1"\n')  # an earlier stamp
    assert not stamper.stamp(built, inner)
    assert "$Format:" in built.read_text()


def test_an_archive_stamp_passes_through_unchanged(stamper, tmp_path):
    """A GitHub source archive's lib/version.py holds git's own stamp and
    no placeholder: the build copy carries it as it is, even over a copy
    left from an earlier build, and even inside a checkout that could be
    described."""
    root = _checkout(tmp_path)
    source = root / "lib" / "version.py"
    source.write_text('_ARCHIVE_DESCRIBE = "v2026.10.1"\n'
                      '_ARCHIVE_COMMIT = "4e0e675"\n')
    built = tmp_path / "build" / "version.py"
    built.parent.mkdir()
    built.write_text('_ARCHIVE_DESCRIBE = "v0.1"\n')
    assert not stamper.stamp(built, root)
    assert built.read_text() == source.read_text()
