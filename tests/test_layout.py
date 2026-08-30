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
so that a `monkeypatch.setattr` on the defining module reaches it. A module that
instead re-exports the bare name hands the test suite a target that patches only
one of the two bindings, and the guards below say which one from each side:
`test_no_test_patches_a_re_exported_name` catches a patch aimed at the copy, and
`test_no_test_patches_a_name_another_module_copied` a patch aimed at the
definition while a copy exists elsewhere. A third shape holds a copy without
importing or assigning anything — a parameter default, bound once when the
`def` runs — and `test_no_parameter_default_freezes_a_patched_name` reads that
one. All three failures are silent: the test passes, having exercised the
unpatched path.

**A path named outside the code still has to resolve.** `tools/` is named from
CLAUDE.md, the rules, the skills, the workflows, the docs, `lib/`, the suite
and the tools themselves — none of which any import graph reaches, so a moved
or renamed tool leaves every one of those hits pointing at nothing and no test
notices. `docs/code-organisation.md` ("A tool keyed on a fixed path list goes quiet
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
    # Its own docstring promises this, and nothing enforced it: the module is
    # imported by both PipeWire scripts, and it is exactly the kind of table
    # that grows a convenience import nobody prices.
    "lib.packages",
    # The socket transport to a running EasyEffects, reached from --doctor and
    # from the end of a generator run; its own docstring promises stdlib-only.
    "lib.ee_socket",
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
    "lib.pipewire.vbe",
    # The PipeWire clock / xrun reader both doctors print from; its own
    # docstring promises stdlib-only, and lib.report.doctor_layout reaches it.
    "lib.pipewire.clock",
    # The schema self-check the converter runs over that output — listed for
    # the other half of FORBIDDEN rather than for startup cost, which
    # test_converter_startup_pulls_in_no_dsp already covers transitively and
    # better. Its contract is to return errors rather than print them, and
    # nothing else in the suite would notice a console.cprint (and with it
    # rich) growing into it. Its own shell-outs, lv2info and spa-json-dump,
    # are execs rather than imports and cost this nothing.
    "lib.pipewire.validate",
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

# Discovered rather than listed, so a module added to lib/ is covered by the
# commit that adds it rather than by a later one that remembers to come back
# here. `__init__.py` files are excluded because the two tests above already
# hold them to something stricter: no submodule re-exports at all.
LIB_MODULES = tuple(sorted(
    ".".join(path.relative_to(ROOT).with_suffix("").parts)
    for path in (ROOT / "lib").rglob("*.py")
    if path.name != "__init__.py"
))

# Both guards below read the same two sides — what our own modules re-export,
# and what the suite patches — so they range over the same set of modules.
ALL_MODULES = ROOT_MODULES + LIB_MODULES

# Genuine violations that predate the guard. Deliberately tiny: an entry here
# is a latent bug someone has to fix, not a blessed exception to the rule.
# Empty, and worth keeping that way — the one violation this guard found on
# arrival (tests/test_cli.py patching a re-exported get_version) was fixed by
# importing the module instead, which is the whole rule in one commit. Widening
# the guard from the three root scripts to all of lib/, and adding the second
# direction below, needed no entry either: both were green on arrival.
KNOWN_STALE_BINDING_PATCHES: set[tuple[str, str]] = set()


def _module_file(dotted):
    """The .py behind a dotted module name, root scripts and lib/ alike."""
    return ROOT.joinpath(*dotted.split(".")).with_suffix(".py")


def _module_level(tree):
    """Statements that execute at import time, functions and classes aside.

    Descends into `if`/`try`/`with` (guarded and version-gated imports are
    still module attributes) but never into a `def` or `class`: a name bound
    inside a function *body* is a local, so it can't be a monkeypatch target
    and isn't part of this contract.

    A `def`'s parameter defaults are the exception that rule doesn't cover.
    They are not the body — Python evaluates them once, when the `def` itself
    runs, so each one is an import-time binding holding whatever the name
    meant at that moment. `test_no_parameter_default_freezes_a_patched_name`
    reads them off the signature instead, deliberately: callers of this
    function want statements to walk, and a default is an expression.
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


def _lib_reexports(module_path):
    """{name this module binds: the lib path it really lives at}.

    Two shapes qualify, and both leave one object reachable under two names:

        from lib.doctor import CheckResult    # name lifted out of a lib module
        DOCTOR_PASS = doctor.DOCTOR_PASS      # attribute copied onto this one

    `from lib import doctor` does *not* qualify. That binds the module object
    itself — the form the refactor rule asks for — and setting an attribute on
    it is seen by every caller that imported the same module, which is the
    whole point.

    Reads a root script and a lib/ module the same way: both are `lib.`-rooted
    absolute imports (there are no relative imports anywhere under lib/), so
    the same parse answers "what did this file copy in" for either.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
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


def _module_patches(test_file):
    """(line, module, attribute) for each setattr aimed at one of our modules.

    Aliases come from the test file's own import lines rather than a fixed
    list, so this keeps covering new tests whatever short name they pick —
    `d`, `e`, `pw`, `hw_sinks` and `report_speaker` are all in use today, and
    the next one won't need an edit here. Both spellings that bind a module
    count: `import dolby_to_easyeffects as d` for the root scripts, and
    `from lib.hardware import sinks as hw_sinks` for lib/, which is the form
    the refactor rule asks callers to use.

    Imports are read wherever they appear rather than at module level only:
    several tests import the lib module they patch inside the test function,
    to keep a heavy import off collection.
    """
    tree = ast.parse(test_file.read_text(encoding="utf-8"))
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # A dotted `import lib.x.y` with no `as` binds `lib`, not the
                # submodule, so it is not a usable patch target and the bound
                # name is skipped rather than mis-recorded.
                bound = alias.asname or alias.name
                if alias.name in ALL_MODULES and "." not in bound:
                    aliases[bound] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level:
            module = node.module or ""
            if module != "lib" and not module.startswith("lib."):
                continue
            for alias in node.names:
                origin = f"{module}.{alias.name}"
                if origin in LIB_MODULES:
                    aliases[alias.asname or alias.name] = origin

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
    the module, the patch from the test files.

    The three root scripts were the whole scope on arrival, back when they
    were the only things importing out of lib/. They no longer are, and the
    imbalance is not close: lib/ modules copy 46 names out of each other
    across 17 import statements, where the scripts copy 2, both by assignment
    rather than by import. A patch aimed at `lib.pipewire.checks` meets
    exactly the same stale copy as one aimed at `ee_to_pipewire`, so the scope
    is every module we ship.

    If this fires, the fix is at the patch site, not here: patch the module
    named in the message. Ideally fix the re-export too, but a test patching
    the defining module is correct either way.
    """
    reexports = {module: _lib_reexports(_module_file(module))
                 for module in ALL_MODULES}

    violations = []
    for test_file in sorted((ROOT / "tests").rglob("*.py")):
        for line, module, attr in _module_patches(test_file):
            if attr not in reexports[module]:
                continue
            if (module, attr) in KNOWN_STALE_BINDING_PATCHES:
                continue
            origin = reexports[module][attr]
            defining_module = origin.rsplit(".", 1)[0]
            violations.append(
                f"{test_file.relative_to(ROOT)}:{line}: patches "
                f"{module}.{attr}, but {module} only re-exports that name "
                f"from {origin} — patch {defining_module} instead, or the "
                f"patch will be invisible to callers inside {defining_module}"
            )

    assert not violations, (
        "monkeypatch target is a re-exported binding, so the patch reaches "
        "only that module's own copy:\n  " + "\n  ".join(violations)
    )


def test_no_test_patches_a_name_another_module_copied():
    """The same trap approached from the other end, and the sharper half.

    Above, the patch is aimed at a module holding a bare copy and misses the
    definition. Here it is aimed at the *definition* — which is the correct
    target, and what the fix message above tells you to do — but some third
    module took its own bare copy at import time, and that copy still holds
    the real object:

        # lib/report/messages.py
        from lib.report.findings import Finding      # bare copy, bound once

        # a test
        monkeypatch.setattr(findings, "Finding", Recording)

    `findings.Finding` is now the fake, and every caller that goes through the
    module sees it. `messages.Finding` does not — it was bound to the original
    class when `messages` was first imported, and rebinding the attribute on
    `findings` cannot reach through to it. So a test that patches the right
    module still exercises the unpatched path in whichever module copied the
    name, and passes while doing it.

    That makes this direction the one worth having. The first direction fires
    on a patch that is wrong on its face; this one fires on a patch that is
    correct, and is silent about a module that is nowhere near the test. It is
    also the direction that decays on its own: nothing about adding
    `from lib.x import helper` to a new module looks like it touches the
    suite, and the test it breaks does not live in that file.

    Both sides are read statically, so a re-export added in one commit and a
    patch added in another meet here on whichever lands second. If this fires,
    the fix is in the *holder* named in the message — import the module and
    look the name up through it — not at the patch site, which was right.
    """
    holders = {module: _lib_reexports(_module_file(module))
               for module in ALL_MODULES}

    violations = []
    for test_file in sorted((ROOT / "tests").rglob("*.py")):
        for line, module, attr in _module_patches(test_file):
            origin = f"{module}.{attr}"
            for holder, reexports in sorted(holders.items()):
                if holder == module:
                    continue
                for name in sorted(n for n, held in reexports.items()
                                   if held == origin):
                    violations.append(
                        f"{test_file.relative_to(ROOT)}:{line}: patches "
                        f"{origin} where it is defined, but {holder} holds a "
                        f"bare copy of it as {holder}.{name} — that copy keeps "
                        f"the real object, so the patch never reaches "
                        f"{holder}. Import the module in {holder} rather than "
                        "the name"
                    )

    assert not violations, (
        "a third module copied the patched name out at import time, so the "
        "patch reaches every caller except that one:\n  "
        + "\n  ".join(violations)
    )


def _frozen_parameter_defaults(module_path, dotted):
    """(line, defining module, attribute) per parameter default bound at import.

    `def f(sdw_bus=SDW_BUS)` evaluates `SDW_BUS` once, when the `def` runs, and
    the signature keeps that object for the life of the process. That is the
    same stale copy `from lib.x import SDW_BUS` makes, wearing a shape neither
    guard above looks at: both read imports and assignments, and this is
    neither.

    Two spellings qualify. A bare name can only refer to something this module
    already bound, so its definer is the module itself; `def f(bus=codecs.
    SDW_BUS)` freezes another module's attribute identically, and is resolved
    through this module's own `lib.` import aliases so the pair reported is the
    one a test would patch. Every import-time binding counts as a source — an
    assignment, a `def`, a class, an imported name — because a monkeypatch can
    aim at any of them.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    own = set()          # names bound here at import time
    lib_modules = {}     # local name -> the lib module it refers to
    for node in _module_level(tree):
        if isinstance(node, ast.Assign):
            own.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            own.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            own.add(node.name)
        elif isinstance(node, ast.Import):
            own.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level:
            own.update(a.asname or a.name for a in node.names)
            module = node.module or ""
            if module == "lib" or module.startswith("lib."):
                for alias in node.names:
                    origin = f"{module}.{alias.name}"
                    if _names_lib_module(origin):
                        lib_modules[alias.asname or alias.name] = origin

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for default in [*args.defaults, *(d for d in args.kw_defaults if d)]:
            if isinstance(default, ast.Name):
                if default.id in own:
                    found.append((default.lineno, dotted, default.id))
            elif isinstance(default, ast.Attribute):
                root, _, attr = (_dotted(default) or "").partition(".")
                if root in lib_modules and attr and "." not in attr:
                    found.append((default.lineno, lib_modules[root], attr))
    return found


def test_no_parameter_default_freezes_a_patched_name():
    """The third place a patched name goes stale, and the quietest.

    Both guards above read what a module *imports* or *assigns*. A parameter
    default is neither, and it is bound just as early:

        # lib/hardware/codecs.py
        def get_pci_audio_subsystem(..., sdw_bus=SDW_BUS):

    `SDW_BUS` is looked up once, while the `def` executes at import. From then
    on the signature holds that Path, and

        monkeypatch.setattr(codecs, "SDW_BUS", fake_bus)

    rebinds the module attribute the function no longer consults. Callers who
    pass the argument are unaffected, which is what makes this quiet: the seam
    looks tested, because the tests that use it all pass the root explicitly.
    The default is the one path nothing covers, and it is the production one.

    Fired on collision, not on shape. A blanket "no default may name a
    module-level constant" rule would reach three sites, two of which nobody
    patches (`lib/dax/discover.py`'s `_CWD_PROBE_MAX_DEPTH` and
    `lib/preset/plugins.py`'s `MBC_BLOCK_SIZE`) and where a `None` sentinel
    would replace a self-documenting signature with one that tells a reader
    nothing. Reading the suite's patch targets too costs one more pass and
    leaves those two alone for as long as they stay untested, which is exactly
    as long as they stay harmless.

    If this fires, the fix is at the definition, not the patch site: take the
    default to `None` and read the name in the body, so every call resolves it
    when it runs.
    """
    frozen = []
    for module in ALL_MODULES:
        path = _module_file(module)
        for line, target, attr in _frozen_parameter_defaults(path, module):
            frozen.append((f"{path.relative_to(ROOT)}:{line}", target, attr))

    patched = set()
    for test_file in sorted((ROOT / "tests").rglob("*.py")):
        patched.update((module, attr)
                       for _line, module, attr in _module_patches(test_file))

    violations = [
        f"{where}: a parameter default freezes {target}.{attr} at import "
        f"time, and tests/ patches that name — the patch rebinds the module "
        f"attribute, which this default stopped consulting the moment the "
        f"def ran. Default to None and read {attr} in the body"
        for where, target, attr in sorted(frozen)
        if (target, attr) in patched
    ]

    assert not violations, (
        "a parameter default holds the patched object, so the default path — "
        "the one production takes — runs unpatched:\n  " + "\n  ".join(violations)
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


def test_the_validator_cli_still_finds_its_runtime_core(tmp_path):
    """`tools/measure_pw/validate_conf.py` still starts, from outside the repo.

    What it checks is worth guarding: that every LV2 port symbol and control
    value in a generated conf is real and in range, and that no band carries
    the xm/MUTE inversion that once silently muted an active PEQ band. Both
    are inaudible as bug reports — a mistyped symbol makes PipeWire refuse the
    whole chain, a muted band merely sounds like a worse tuning — which is why
    the check is on by default, and why it matters that everything reaching it
    degrades *quietly*.

    Nothing shells out to it per conf any more: `ee_to_pipewire.py` and, since
    the session schema memo, `tests/corpus/test_ee_to_pipewire_corpus.py` both
    call `lib.pipewire.validate.run` in process, so no path under `tools/`
    reaches a user-runnable script any more. That corpus module runs the
    wrapper once, on one rendered conf, which is the only end-to-end exercise
    of it left — and it needs a corpus and both CLIs to say anything. What is
    left here is the command-line front end, whose own way of breaking is
    specific and silent to everything else: it inserts the repo root on `sys.path`,
    counted from `Path(__file__).resolve().parents[2]`, before importing that
    module. Move either file and the import fails — while
    `tests/test_validate_conf.py` goes on importing `lib.pipewire.validate`
    directly and the reference sweep above goes on finding a file that exists.

    `--help` is the whole test. It reaches the module-scope import and the
    argument parser, needs neither `lv2info` nor `spa-json-dump` nor a corpus,
    and costs about 30 ms. It runs from a directory outside the checkout because
    `python path/to/script.py` puts only the script's own directory on
    `sys.path` — so the bootstrap is the only thing that can make `lib`
    importable, which is exactly the claim being tested.
    """
    wrapper = ROOT / "tools" / "measure_pw" / "validate_conf.py"
    result = subprocess.run([sys.executable, str(wrapper), "--help"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"{wrapper.relative_to(ROOT)} does not start: it exited "
        f"{result.returncode}. The corpus tier's one end-to-end run of it "
        "fails with it, and by hand it stops being reachable at all.\n"
        f"{result.stderr.strip()}"
    )


def test_the_validator_cli_separates_setup_failure_from_a_bad_conf(tmp_path):
    """A missing or broken dependency exits 2, never 1.

    The wrapper's docstring defines 1 as "at least one error" — a statement
    about the *conf* — and 2 as a setup error. An unguarded exception exits 1
    with a traceback, so a caller gating on `$?` (the corpus tier does, and so
    would any CI step) reads "your tooling isn't installed" as "the conf this
    tool generated is invalid" and goes looking for a bug in the converter.

    Both arms are here because they fail in different places: the preflight
    runs before anything is read, the parse failure comes out of
    `spa-json-dump` mid-run. Neither needs a corpus, and the fake CLIs mean
    neither needs the real ones either.
    """
    wrapper = ROOT / "tools" / "measure_pw" / "validate_conf.py"
    conf = "context.modules = [ { name = libpipewire-module-filter-chain } ]\n"

    missing = subprocess.run(
        [sys.executable, str(wrapper), "-"],
        input=conf, cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": "", "HOME": str(tmp_path)},
    )
    assert missing.returncode == 2, (
        f"with no CLI on PATH the wrapper exited {missing.returncode}, which "
        f"its own docstring reads as a bad conf:\n{missing.stderr.strip()}"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in (("lv2info", "exit 0"),
                       ("spa-json-dump", "echo 'boom' >&2; exit 1")):
        script = fake_bin / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)

    broken = subprocess.run(
        [sys.executable, str(wrapper), "-"],
        input=conf, cwd=tmp_path, capture_output=True, text=True,
        env={"PATH": str(fake_bin), "HOME": str(tmp_path)},
    )
    assert broken.returncode == 2, (
        f"with a failing spa-json-dump the wrapper exited "
        f"{broken.returncode}, which its own docstring reads as a bad "
        f"conf:\n{broken.stderr.strip()}"
    )


def test_the_doctor_never_sends_a_mutating_socket_request():
    """--doctor's socket use is two reads behind an allowlist; the load lives
    in lib/preset/reload.py. Kept apart by name, so a convenience import can't
    quietly hand the diagnostic a request that changes the app it inspects."""
    source = (ROOT / "lib" / "report" / "doctor_run.py").read_text()
    assert "load_output_preset" not in source
