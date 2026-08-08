"""The rules the `lib/` split has to keep, enforced rather than asserted.

Three contracts live here, all about the *shape* of the tree rather than what
any module computes, and all invisible to every other test in the suite.

**Stdlib-only, at the converter's expense.** `lib/version.py`, `lib/ee_paths.py`
and `lib/doctor.py` each say in their own docstring that they import nothing
but the stdlib, because `ee_to_pipewire.py` imports all three at startup and
must not pull numpy/scipy into a converter that does no DSP. Until now that was
a promise in prose. As `lib/` grows, the tempting shortcut is to reach for
numpy in a module one of these already imports — this fails the moment that
happens, at the cheap end of the pipeline rather than in a user's startup
time.

`STDLIB_ONLY` is a list of modules that promised it, not a rule over `lib/`.
`lib/console.py` is deliberately absent: it owns the optional rich import, so
holding it to the stdlib would mean no coloured output anywhere. What binds
*every* module, listed or not, is the DSP half — `numpy`/`scipy` cost 0.4 s
where rich costs milliseconds — and that is checked from the converter's end
by `test_converter_startup_pulls_in_no_dsp`, which needs no list.
`test_the_dsp_import_is_deferred_past_every_early_return` beside it asks the
same question of the *generator*, where numpy is deferred rather than banned.
It reads as a completion trap and grew up in `tests/test_completions.py`, but
it needs no argcomplete, and that file's module-scope `importorskip` skips the
whole file — so on the plain install where a hoisted import hurts most, it did
not run at all.

**Import the module, not the name.** Across a `lib/` boundary a caller binds
the module (`from lib.hardware import codecs`, then `codecs.get_soundwire_ids()`)
so that a `monkeypatch.setattr` on the defining module reaches it. A root script
that instead re-exports the bare name hands the test suite a target that patches
only the root script's own copy — see
`test_no_test_patches_a_re_exported_name` for why that failure is silent.

**A path named outside the code still has to resolve.** `tools/` is named from
CLAUDE.md, the rules, the skills, the workflows, the docs, `lib/`, the suite
and the tools themselves — none of which any import graph reaches, so a moved
or renamed tool leaves every one of those hits pointing at nothing and no test
notices. `docs/design-notes.md` ("A tool keyed on a fixed path list goes quiet
when code moves") is the recorded cost: `16c4723` moved `doctor.py` into
`lib/`, and because `tools/extract_claims.py` harvested from a hardcoded table
of filenames, that module's three verdict lines dropped out of the copy-audit
inventory. Nothing errored — the next audit would simply have reviewed less
than it believed it was reviewing. The checklist that came out of it is to grep
the whole repo, `.github/` and `.claude/` included, for the name of any file
being moved; the sweep at the bottom of this file is that grep, run for you.

The import checks each run in a subprocess: `sys.modules` is process-wide, and
under `-n auto` some other test in the same worker has almost certainly
imported numpy already. The binding and reference checks are static reads, so
they cost nothing and cover files no test happens to import.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import write_synthetic_tuning_xml

ROOT = Path(__file__).resolve().parent.parent

# The DSP stack, plus the optional presentation deps. rich is absent on a
# plain install and every script is required to work without it, so a lib
# module that hard-imports it would break that contract too.
FORBIDDEN = ("numpy", "scipy", "rich", "rich_argparse")

STDLIB_ONLY = (
    "lib.version", "lib.ee_paths", "lib.doctor", "lib.paths",
    "lib.data.kernel_releases",
    "lib.data.speaker_pin_quirks",
    "lib.hardware.codecs",
    "lib.hardware.amps",
    "lib.hardware.speakers",
    # Neither lib.dax module is listed. Both are stdlib-only in the sense the
    # converter's startup cares about — no numpy, no scipy — but both print
    # (discover announces what it matched, parse reports dropped features), so
    # both reach lib.console and the optional rich import it owns, which
    # FORBIDDEN also covers. Absent for that reason, not by oversight.
    # The two halves of preset construction that need no DSP: the closed-form
    # band arithmetic, and the writers. Every other lib.preset module reaches
    # numpy — fir imports it, emit imports it and scipy besides, plugins gets
    # it through fir and build through plugins — so the four are absent here
    # for that reason, not by oversight.
    "lib.preset.bands",
    "lib.preset.autoload",
    # No lib.report module is listed, and none can be: the package exists to
    # print. doctor_run.py prints the --doctor report, speaker.py the hardware
    # dump, profile.py the per-profile diagnostics, findings/messages/
    # environment the copy the user acts on — so every one of them reaches
    # lib.console and the optional rich that FORBIDDEN also covers. Absent for
    # that reason, not by oversight; profile.py doubly so, since it reaches
    # numpy as well and the generator imports it inside main() for that.
    # The converter's translation half: an EE plugin block turned into LV2
    # node dicts, and those nodes rendered as SPA-JSON. Its siblings
    # lib.pipewire.install and lib.pipewire.checks print, so they reach
    # lib.console and are absent here for that reason, not by oversight.
    "lib.pipewire.plugins",
    "lib.pipewire.conf",
)


@pytest.mark.parametrize("module", STDLIB_ONLY)
def test_module_pulls_in_no_heavy_dependency(module):
    probe = (
        "import sys\n"
        f"import {module}\n"
        f"got = [n for n in {FORBIDDEN!r} if n in sys.modules]\n"
        "print(','.join(got))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    pulled = result.stdout.strip()
    assert not pulled, (
        f"{module} is documented as stdlib-only but pulls in {pulled}. "
        "Move whatever needs it into a module ee_to_pipewire.py doesn't "
        "import at startup."
    )


def test_converter_startup_pulls_in_no_dsp():
    """`ee_to_pipewire.py` does no DSP, so importing it must not cost the
    ~0.4 s the numpy/scipy import takes.

    STDLIB_ONLY above is the same rule written as a hand-maintained list, and
    a list only covers what someone remembered to add to it: the module that
    leaks numpy into the converter will be one nobody thought to name. This
    asks the question from the other end — whatever `ee_to_pipewire.py`
    reaches at import time, transitively and however deep, none of it may be
    the DSP stack — so it keeps working as lib/ grows without being edited.

    Distinct from `test_the_dsp_import_is_deferred_past_every_early_return`
    below, which covers the *generator*: that one guards a deferral, where
    numpy is merely postponed to the function-local imports in `main()` and a
    real conversion still pays for it. Here there is nothing to defer to — the
    converter never wants numpy at all, on any path.
    """
    probe = (
        "import sys\n"
        "import ee_to_pipewire\n"
        "print('numpy' in sys.modules, 'scipy' in sys.modules)\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False False", (
        "the DSP stack reached ee_to_pipewire.py's startup imports "
        f"(numpy, scipy in sys.modules: {result.stdout.strip()}). Whatever "
        "lib module now needs numpy is one the converter imports at module "
        "level — import it lazily inside the function that needs it, or move "
        "that function to a module the converter doesn't touch."
    )


def test_the_dsp_import_is_deferred_past_every_early_return(tmp_path):
    """NumPy/SciPy are ~0.35 s of the generator's ~0.5 s startup, and the
    generator imports them inside main(), just above the emit loop. So a path
    that returns before that loop — --version here, with --list, --doctor,
    --speaker-info and an argparse error alongside it — must cost nothing,
    while a real conversion still gets them.

    Both halves are load-bearing: an import hoisted back to module scope fails
    the first, one deferred past its own use fails the second. A regression on
    the first half is invisible except as a sluggish `--version`, hence a trap
    on sys.modules rather than on wall-clock.

    The conversion passes both output directories — --output-dir without
    --irs-dir writes the .irs into the live EasyEffects tree.
    """
    probe = (
        "import sys; sys.path.insert(0, %r)\n"
        "import dolby_to_easyeffects as d\n"
        "try:\n"
        "    d.main(sys.argv[1:])\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('numpy' in sys.modules, 'scipy' in sys.modules)\n" % str(ROOT)
    )

    def dsp_loaded(*argv: str) -> str:
        result = subprocess.run(
            [sys.executable, "-c", probe, *argv],
            capture_output=True, text=True, timeout=120, cwd=ROOT,
        )
        assert result.returncode == 0, result.stderr
        # main() prints the run's own output first; the probe's verdict is the
        # last line.
        return result.stdout.strip().splitlines()[-1]

    assert dsp_loaded("--version") == "False False", (
        "the DSP stack reached a path that returns before the emit loop — "
        "something imports numpy at module scope again"
    )

    xml = write_synthetic_tuning_xml(tmp_path / "DEV_SYNTH_SUBSYS_TEST.xml")
    assert dsp_loaded(str(xml),
                      "--output-dir", str(tmp_path / "presets"),
                      "--irs-dir", str(tmp_path / "irs")) == "True True"
    assert list((tmp_path / "irs").glob("*.irs")), "no conversion happened"


# Sub-packages, discovered rather than listed, so the check is already in
# place on the commit that creates lib/hardware/ or lib/dax/ — not on some
# later commit that remembers to come back here.
LIB_SUBPACKAGES = sorted(
    ".".join(p.parent.relative_to(ROOT).parts)
    for p in (ROOT / "lib").rglob("__init__.py")
    if p.parent != ROOT / "lib"
)


@pytest.mark.parametrize("package", LIB_SUBPACKAGES)
def test_subpackage_init_is_import_free(package):
    """A sub-package's `__init__.py` is under the same no-re-export rule as
    `lib/__init__.py`, and for a sharper reason.

    `lib/hardware/__init__.py` doing `from lib.hardware.codecs import *` looks
    like a kindness — callers get `lib.hardware.get_soundwire_ids()` — but it
    costs twice. Every import of any one sibling drags in all of them, which
    is how the stdlib-only modules above acquire a numpy dependency they never
    asked for. And it makes cycles reachable: `codecs` importing a sibling
    re-enters the half-initialised `__init__`, which fails as an ImportError
    pointing at a line that is not the problem.

    Importing ancestors is unavoidable (Python imports `lib` before
    `lib.hardware`); importing anything *below* the package, or from a
    different lib subtree, is not.

    Parametrised over what exists, so this skips cleanly on a flat lib/ and
    starts enforcing the moment the first sub-package lands.
    """
    ancestors = {package.rsplit(".", n)[0]
                 for n in range(package.count(".") + 1)}
    probe = (
        "import sys\n"
        f"import {package}\n"
        "print(','.join(sorted(n for n in sys.modules "
        f"if n.startswith('lib.') and n not in {ancestors!r})))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    dragged = result.stdout.strip()
    assert not dragged, (
        f"importing {package} also imported {dragged} — keep "
        f"{package.replace('.', '/')}/__init__.py free of submodule "
        "re-exports and let callers import the submodule they want by name."
    )


def test_lib_package_import_is_free():
    """`import lib` must stay side-effect-free — no submodule re-exports.

    A convenience re-export in `lib/__init__.py` would drag the whole package
    in behind any single import and hand every future module a ready-made
    import cycle.
    """
    probe = (
        "import sys\n"
        "import lib\n"
        "print(','.join(n for n in sys.modules if n.startswith('lib.')))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip(), (
        f"importing lib also imported {result.stdout.strip()}"
    )


def test_root_holds_only_entry_points():
    """Every root .py file is something a user types.

    The layout rule this repo keeps: the root is the command surface (those
    paths appear in the README, the issue template and the argcomplete
    registration line), and everything else lives under lib/.
    """
    entry_points = {"dolby_to_easyeffects.py", "ee_to_pipewire.py",
                    "dolby_to_pipewire.py"}
    found = {p.name for p in ROOT.glob("*.py")}
    assert found == entry_points, (
        f"unexpected root-level Python: {sorted(found - entry_points)} — "
        "a module that is not an entry point belongs in lib/"
    )


# --------------------------------------------------------------------------
# The stale-binding trap: what a root script re-exports vs. what tests patch.
# --------------------------------------------------------------------------

ROOT_MODULES = ("dolby_to_easyeffects", "ee_to_pipewire", "dolby_to_pipewire")

# Genuine violations that predate the guard. Deliberately tiny: an entry here
# is a latent bug someone has to fix, not a blessed exception to the rule.
# Empty, and worth keeping that way — the one violation this guard found on
# arrival (tests/test_cli.py patching a re-exported get_version) was fixed by
# importing the module instead, which is the whole rule in one commit.
KNOWN_STALE_BINDING_PATCHES: set[tuple[str, str]] = set()


def _module_level(tree):
    """Statements that execute at import time, functions and classes aside.

    Descends into `if`/`try`/`with` (guarded and version-gated imports are
    still module attributes) but never into a `def` or `class`: a name bound
    inside a function body is a local, so it can't be a monkeypatch target and
    isn't part of this contract.
    """
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With)):
            stack.extend(getattr(node, "body", []))
            stack.extend(getattr(node, "orelse", []))
            stack.extend(getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                stack.extend(handler.body)


def _dotted(node):
    """"a.b.c" for a chain of attribute accesses rooted at a plain name."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _names_lib_module(dotted):
    """True if `dotted` ("lib.hardware.codecs") is a real module under lib/."""
    base = ROOT.joinpath(*dotted.split("."))
    return base.with_suffix(".py").exists() or (base / "__init__.py").exists()


def _lib_reexports(script):
    """{name a root script binds: the lib path it really lives at}.

    Two shapes qualify, and both leave one object reachable under two names:

        from lib.doctor import CheckResult    # name lifted out of a lib module
        DOCTOR_PASS = doctor.DOCTOR_PASS      # attribute copied onto this one

    `from lib import doctor` does *not* qualify. That binds the module object
    itself — the form the refactor rule asks for — and setting an attribute on
    it is seen by every caller that imported the same module, which is the
    whole point.
    """
    tree = ast.parse(script.read_text(encoding="utf-8"))
    lib_modules = {"lib": "lib"}   # local name -> the lib module it refers to
    reexports = {}

    for node in _module_level(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lib" or alias.name.startswith("lib."):
                    if alias.asname:
                        lib_modules[alias.asname] = alias.name
                    else:
                        lib_modules["lib"] = "lib"
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module != "lib" and not module.startswith("lib."):
                continue
            for alias in node.names:
                origin = f"{module}.{alias.name}"
                if _names_lib_module(origin):
                    lib_modules[alias.asname or alias.name] = origin
                else:
                    reexports[alias.asname or alias.name] = origin

    # Second pass: assignments can only be read once every module alias is
    # known, and nothing guarantees the imports came first.
    for node in _module_level(tree):
        if not isinstance(node, ast.Assign):
            continue
        origin = _dotted(node.value)
        if origin is None:
            continue
        root, _, attr_path = origin.partition(".")
        if root not in lib_modules or not attr_path:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                reexports[target.id] = f"{lib_modules[root]}.{attr_path}"
    return reexports


def _root_module_patches(test_file):
    """(line, root module, attribute) for each setattr aimed at a root script.

    Aliases come from the test file's own `import X as Y` lines rather than a
    fixed list, so this keeps covering new tests whatever short name they
    pick — `d`, `e` and `pw` are all in use today, and the next one won't
    need an edit here.
    """
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ROOT_MODULES:
                    aliases[alias.asname or alias.name] = alias.name

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        is_setattr = (isinstance(func, ast.Attribute) and func.attr == "setattr"
                      or isinstance(func, ast.Name) and func.id == "setattr")
        target, attr = node.args[0], node.args[1]
        if not is_setattr or not isinstance(target, ast.Name):
            continue
        if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
            continue
        if target.id in aliases:
            found.append((node.lineno, aliases[target.id], attr.value))
    return found


def test_no_test_patches_a_re_exported_name():
    """A patch aimed at a re-exported name silently patches nothing.

    The refactor rule across a lib/ boundary is: import the module, not the
    name. `from lib.hardware import codecs`, then `codecs.get_soundwire_ids()`
    — never `from lib.hardware.codecs import get_soundwire_ids`. The reason is
    entirely about this suite. If a root script lifts the bare name out, it
    gets its own binding to that function, and

        monkeypatch.setattr(dolby_to_easyeffects, "get_soundwire_ids", fake)

    rebinds only that copy. Every caller still inside lib/ looks the function
    up through `codecs.__dict__` and gets the real one, which then goes and
    reads the developer's actual hardware. The test does not error; it passes,
    having exercised the unpatched path — the most expensive failure mode
    available, because it is indistinguishable from success and stays that way
    until someone notices a "unit" test is machine-dependent.

    There are ~150 such patches in tests/, so the exposure grows with every
    symbol the split moves. Checking it statically catches the mistake in the
    commit that makes it, on both sides at once: the re-export is read from
    the root scripts, the patch from the test files.

    If this fires, the fix is at the patch site, not here: patch the module
    named in the message. Ideally fix the re-export too, but a test patching
    the defining module is correct either way.
    """
    reexports = {module: _lib_reexports(ROOT / f"{module}.py")
                 for module in ROOT_MODULES}

    violations = []
    for test_file in sorted((ROOT / "tests").rglob("*.py")):
        for line, module, attr in _root_module_patches(test_file):
            if attr not in reexports[module]:
                continue
            if (module, attr) in KNOWN_STALE_BINDING_PATCHES:
                continue
            origin = reexports[module][attr]
            defining_module = origin.rsplit(".", 1)[0]
            violations.append(
                f"{test_file.relative_to(ROOT)}:{line}: patches "
                f"{module}.{attr}, but {module}.py only re-exports that name "
                f"from {origin} — patch {defining_module} instead, or the "
                f"patch will be invisible to callers inside {defining_module}"
            )

    assert not violations, (
        "monkeypatch target is a re-exported binding, so the patch reaches "
        "only the root script's own copy:\n  " + "\n  ".join(violations)
    )


# --------------------------------------------------------------------------
# tools/ has no import graph, so its paths are only as good as this sweep.
# --------------------------------------------------------------------------

# Read whole. `.claude/` is deliberately narrowed to the two committed
# subtrees — agent worktrees live under `.claude/worktrees/` and are a second
# copy of the repo, so rglob'ing all of `.claude/` would sweep stale clones.
REFERENCE_TREES = (".claude/rules", ".claude/skills", ".github", "docs",
                   "lib", "tests", "tools")

# Plus the loose files at the root, which are as full of these paths as any
# directory: the rules a reader is handed, the flag list, the release history.
REFERENCE_FILES = ("CLAUDE.md", "README.md", "CHANGELOG.md", "pyproject.toml",
                   "dolby_to_easyeffects.py", "ee_to_pipewire.py",
                   "dolby_to_pipewire.py")

# Nothing in these is text, and `docs/` carries screenshots.
BINARY_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".pdf", ".irs", ".wav",
                   ".npy", ".npz", ".zip"}

# Greedy over what a path may contain, then anchored on a character a path can
# plausibly end with, so trailing prose punctuation ("in `tools/measure_ee/`,")
# is left behind. A bare `tools/` matches nothing and is not worth checking.
TOOLS_REFERENCE = re.compile(r"tools/[A-Za-z0-9_./*-]*[A-Za-z0-9_*]")


def _reference_sources():
    """Every committed text file that might name a `tools/` path."""
    for name in REFERENCE_FILES:
        yield ROOT / name
    for tree in REFERENCE_TREES:
        for path in sorted((ROOT / tree).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() in BINARY_SUFFIXES:
                continue
            yield path


def _tools_references():
    """(file, line number, reference) for each `tools/…` path named in the tree.

    Lines holding a URL are skipped whole. Upstream projects have a `tools/`
    directory too, and the one such mention here — `thesofproject/sof`'s
    `tune/eq/` path, cited in `docs/alternative-pipelines.md` — is a link, so
    the URL test separates it from ours without a per-path exception list. It
    costs nothing today: that link is the only line in the sweep holding both
    a scheme and a `tools/` path. (Writing that upstream path out in full
    here, unlinked, is what this docstring did on its first draft — and this
    test failed on it, which is the check working.)
    """
    for path in _reference_sources():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if "://" in line:
                continue
            for match in TOOLS_REFERENCE.finditer(line):
                yield path, number, match.group(0)


def test_every_referenced_tools_path_exists():
    """A `tools/` path someone wrote down still points at a file.

    Nothing else can catch this. These references sit in prose, in YAML
    comments, in `paths:` frontmatter and in `sys.path` neighbours — no
    importer resolves them, so a renamed or moved tool leaves a doc that lies,
    a rule that scopes to nothing, and a workflow step that only fails the
    week it next runs. The one in `.claude/rules/docs.md`'s frontmatter is the
    sharpest: a `paths:` glob that matches no file doesn't error, it just
    stops loading the rule it was written to load.

    Globs are resolved as globs (`tools/**/*.py` in a `paths:` list has to
    match *something*, which is the same question one directory up), literal
    paths as literal paths.

    If this fires on a path you deliberately moved, fix the reference — that
    is the whole point of it firing. Sweeping a new directory is a one-line
    edit to REFERENCE_TREES above.
    """
    broken = [
        f"{path.relative_to(ROOT)}:{number}: {reference}"
        for path, number, reference in _tools_references()
        if not (list(ROOT.glob(reference)) if "*" in reference
                else (ROOT / reference).exists())
    ]
    assert not broken, (
        "these name a tools/ path that does not exist — the file moved, was "
        "renamed, or the reference was mistyped:\n  " + "\n  ".join(broken)
    )


# Discovered, not listed, so a new measure_* directory is covered by the
# commit that creates it rather than by a later one that remembers to come
# back here.
MEASURE_DIRS = sorted(p for p in (ROOT / "tools").glob("measure_*")
                      if p.is_dir())


@pytest.mark.parametrize("directory", MEASURE_DIRS, ids=lambda p: p.name)
def test_measure_readme_lists_every_script(directory):
    """A measurement script nobody lists is a script nobody finds.

    Each `measure_*/` directory is a *procedure* — set up a route, capture a
    battery, compare the results — and its README is the only map of that
    procedure. Nothing imports these scripts from outside their own directory,
    no test runs them (they want a sound card), and their filenames alone do
    not say which step they belong to. So a script that never makes it into
    the README is invisible: the next person re-derives it, or more likely
    re-implements it.

    That is not hypothetical. `sweep_variants.sh` and `summarise_variants.py`
    both landed in `60bed58` and neither reached `tools/measure_ee/README.md`;
    they sat unlisted for three months, in the one directory whose README is
    the most detailed of the four.

    A bare mention is enough — `measure_dax/` names its three scripts in prose
    and a usage block rather than a table, and that maps the procedure just as
    well. What this rejects is a filename that appears nowhere at all.
    """
    readme = directory / "README.md"
    assert readme.is_file(), (
        f"{directory.relative_to(ROOT)} has no README.md — every measure_* "
        "directory is a procedure, and the README is where it is written down"
    )
    text = readme.read_text(encoding="utf-8")
    missing = sorted(p.name for p in directory.iterdir()
                     if p.suffix in (".py", ".sh") and p.name not in text)
    assert not missing, (
        f"{readme.relative_to(ROOT)} never mentions {', '.join(missing)} — "
        "add a row saying what the script does and where in the procedure it "
        "runs, or delete the script"
    )


def test_the_converter_can_find_its_validator():
    """`ee_to_pipewire.py` shells out to a path in `tools/`, and degrades
    quietly when it isn't there.

    `lib/pipewire/install.py` assembles `VALIDATE_CONF_SCRIPT` from path
    components (`REPO_ROOT / "tools" / "measure_pw" / …`), so the string
    `tools/measure_pw/validate_conf.py` appears nowhere in that file and the
    sweep above is blind to it. Three files hold the path in code, and this is
    the only one the sweep cannot reach: `tests/test_validate_conf.py` loads
    it by `spec_from_file_location` and would fail loudly at collection, and
    `tests/corpus/test_ee_to_pipewire_corpus.py` assembles it the same way but
    writes it out in its module docstring, which the sweep does see.

    It is also the one worth checking most, because it is the only one that
    runs on a user's machine. `_validate_conf` returns -1 when the file is
    missing, its caller treats -1 as a soft warning, and `ee_to_pipewire.py`
    goes on writing confs — having silently stopped checking that every LV2
    port symbol and control value in them is real. Validation is on by default
    precisely because the check is cheap and what it catches is inaudible as a
    bug report: a mistyped port symbol, or the xm/MUTE inversion that once
    silently muted an active PEQ band. The corpus tier degrades the same way,
    from the other side — its `if ... VALIDATE_CONF_SCRIPT.is_file()` guard
    makes a missing script mean "don't check", and the XML passes green
    without a conf ever being validated.

    So moving this file is not a `tools/` reorganisation, it is a change to
    the converter — which is also why it is scheduled to move. A top-level
    user-runnable script may depend only on `lib/`, and this is the one place
    in the tree where that does not hold; the plan is for the runtime core to
    become `lib/pipewire/validate.py`, with a thin CLI wrapper left behind in
    `tools/measure_pw/`. This assert is not a claim that the path is
    permanent. It is the guard for exactly that move: whatever
    `VALIDATE_CONF_SCRIPT` ends up pointing at has to be a file that exists,
    and if the move re-points some of the sites above and not this one, the
    suite says so at collection instead of a user's conf silently going
    unvalidated.
    """
    from lib.pipewire import install

    assert install.VALIDATE_CONF_SCRIPT.is_file(), (
        f"ee_to_pipewire.py shells out to {install.VALIDATE_CONF_SCRIPT}, "
        "which does not exist — conf validation is silently a no-op. Either "
        "put the script back or re-point VALIDATE_CONF_SCRIPT in "
        "lib/pipewire/install.py (and the copy in "
        "tests/corpus/test_ee_to_pipewire_corpus.py) at where it went."
    )
