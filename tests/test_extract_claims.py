"""The claim inventory the /copy-audit reviewers read, and the ways it goes quiet.

`tools/extract_claims.py` has failed twice by *shrinking*, never by erroring.
`16c4723` moved `doctor.py` into `lib/` and its three verdict lines left the
inventory with it, because the harvester walked a hardcoded list of root
filenames. `d6eed66` hoisted `PIPEWIRE_RESTART_CMD` into `lib/pipewire/conf.py`
and the lines that spell it in became `{PIPEWIRE_RESTART_CMD}` — a stub with no
sentence in it, which the copy filter then dropped. Both times the tool exited
0, printed a plausible row count, and the next audit reviewed less than it
believed it was reviewing. `docs/design-notes.md`, "A tool keyed on a fixed path
list goes quiet when code moves", is the write-up.

So the guards here are in two halves. The first builds the real inventory with
the real CLI and asserts that specific lines a run prints are *in* it — the
shape of check that would have failed on both commits above, rather than a
count nobody was watching. The second covers the two filter behaviours on
synthetic modules, where the input can be stated in full.

Anchors are matched as substrings, and picked for being hard to reword rather
than for being pretty: the guard is about a surface disappearing, and it is
worth nothing if ordinary copy edits keep tripping it.

Fast tier: the CLI run below is ~1 s and touches no corpus XML.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tools import extract_claims

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "extract_claims.py"

_ROW = re.compile(r"^[\w.]+-\d{3}$")


@pytest.fixture(scope="module")
def inventory(tmp_path_factory):
    """Every row the real CLI writes, over the real tree.

    `--since HEAD` makes the range empty, so every row is tagged `-` and the
    slices come out empty. Nothing here asks what a commit changed; running the
    entry point rather than re-assembling its loop is the point, since the loop
    is what went quiet the last two times.
    """
    out = tmp_path_factory.mktemp("claims")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--since", "HEAD", "--out-dir", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rows = []
    for line in (out / "claims.md").read_text().splitlines():
        parts = line.split(" | ", 5)
        if len(parts) == 6 and _ROW.match(parts[0]):
            rows.append({"id": parts[0], "file": parts[2].rsplit(":", 1)[0],
                         "kind": parts[4], "text": parts[5]})
    return rows


# (file, substring) — one line per surface, each chosen because rewording it is
# a deliberate act rather than a passing tidy-up.
ANCHORS = (
    # The exact casualty of `16c4723`. Safe to pin: `lib/doctor.py`'s own
    # docstring commits to the two doctors sharing one verdict vocabulary
    # ("same status boxes, same counts, same verdict wording"), so this
    # sentence cannot drift on one side without the other being edited too.
    ("lib/doctor.py", "No blocking problems"),
    # The generator's positional-argument help, and the first thing `--help`
    # prints. A noun phrase naming the vendor and the file format — there is
    # no opinion in it to revise.
    ("dolby_to_easyeffects.py", "Dolby DAX3 tuning XML"),
    # Two lines in this module interpolate the constant, so the anchor holds
    # even if the footnote around one of them is rewritten. Pins that the
    # PipeWire installer's copy is inventoried at all.
    ("lib/pipewire/install.py", "{QUIT_EE_HINT}"),
    # The prose harvester is a separate path from the AST one; without an
    # anchor on it, the README could stop being read and nothing would say so.
    ("README.md", "EasyEffects"),
)


@pytest.mark.parametrize("rel,needle", ANCHORS)
def test_a_line_a_run_prints_is_in_the_inventory(inventory, rel, needle):
    """A reviewer can only audit what the inventory hands them."""
    assert (ROOT / rel).exists(), f"{rel} moved — re-point this anchor"
    assert any(r["file"] == rel and needle in r["text"] for r in inventory), (
        f"no row from {rel} contains {needle!r}. Either that copy is gone "
        "(re-point the anchor), or the harvester stopped reaching this file "
        "and the next copy audit will silently review less than it thinks."
    )


def test_the_inventory_still_covers_every_surface(inventory):
    """Row counts are the wrong alarm on their own — the tool loses a *file*
    at a time, which a total barely moves.

    So this pins breadth: each entry point, each prose surface, and a floor on
    how many `lib/` modules contribute. The floor is a floor, not a target —
    `lib/` only gains modules, and the number is here to catch the discovery
    walk returning nothing, not to be kept current. `L` (CHANGELOG) is
    deliberately absent: its section is legitimately empty right after a
    release.
    """
    prefixes = {r["id"].rsplit("-", 1)[0] for r in inventory}
    for surface in ("E", "W", "C", "R", "I"):
        assert surface in prefixes, (
            f"no rows with prefix {surface} — a whole surface dropped out of "
            "the inventory")
    lib_modules = {p for p in prefixes if p.startswith("lib.")}
    assert len(lib_modules) >= 15, (
        f"only {len(lib_modules)} lib/ modules contributed rows — the "
        "module discovery walk is not reaching the package")
    assert len(inventory) >= 600, (
        f"{len(inventory)} rows is far below what this tree emits; the "
        "harvester is dropping copy wholesale")


# --------------------------------------------------------------------------
# Resolving a constant into the filter probe.
# --------------------------------------------------------------------------

def _harvest(tmp_path, monkeypatch, source: str, *, elsewhere: str = "") -> list:
    """Rows the harvester produces for `source`, as a module of its own.

    `elsewhere` is a second module contributing constants and nothing else —
    the cross-file case the map exists for. Both are addressed by absolute
    path: the harvester resolves its argument against the repo root, and
    pathlib passes an absolute path straight through, so a synthetic module
    never has to be written into the tree. The git range is stubbed because
    none of these tests are about which lines a commit touched.
    """
    monkeypatch.setattr(extract_claims, "changed_lines",
                        lambda rel, base: set())
    target = tmp_path / "target.py"
    target.write_text(source)
    other = tmp_path / "elsewhere.py"
    other.write_text(elsewhere)
    consts = extract_claims.module_constants([str(other), str(target)])
    rows: list[dict] = []
    extract_claims.harvest_script("T", str(target), rows, "HEAD", consts)
    return rows


def test_a_sentence_behind_a_constant_still_reaches_the_filter(tmp_path,
                                                               monkeypatch):
    rows = _harvest(tmp_path, monkeypatch,
                    'QUIT_EE_HINT = ("If you also run EasyEffects, quit it "\n'
                    '                "and stop it starting again")\n'
                    '\n'
                    'def show(other):\n'
                    '    cprint("dim", f"  Note: {QUIT_EE_HINT}.")\n'
                    '    cprint("dim", f"  Note: {other}.")\n')
    texts = [r["text"] for r in rows]

    # Admitted on what the constant says...
    assert "Note: {QUIT_EE_HINT}." in texts
    # ...and still rendered as the source form. The reviewer is being asked
    # about the line as written, and cites it back by id; a row whose text had
    # been quietly expanded would not be findable in the file.
    assert not any(t.startswith("Note: If you also run") for t in texts)
    # The control. Same sentence shape with nothing resolvable behind it stays
    # out, so the test above cannot be satisfied by a looser filter.
    assert "Note: {other}." not in texts


def test_a_constant_resolves_across_a_module_boundary(tmp_path, monkeypatch):
    """Definition and use routinely sit in different files — the real case is
    `PIPEWIRE_RESTART_CMD`, defined in `lib/pipewire/conf.py` and spelled into
    a line of copy in three other modules. A per-file map would resolve none
    of them."""
    rows = _harvest(
        tmp_path, monkeypatch,
        'def undo(files):\n'
        '    cprint("dim", "  To undo: rm " + files)\n'
        '    cprint("dim", f"           {PIPEWIRE_RESTART_CMD}")\n',
        elsewhere='PIPEWIRE_RESTART_CMD = "systemctl --user restart pipewire"\n')
    assert "{PIPEWIRE_RESTART_CMD}" in [r["text"] for r in rows]


def test_only_a_module_scope_string_constant_resolves(tmp_path):
    """An f-string would need the same resolution one level down, and a name
    bound inside a function body is not what `{NAME}` in another module refers
    to. Neither is worth guessing at for a filter decision."""
    path = tmp_path / "m.py"
    path.write_text(
        'GOOD = "a real sentence about your speakers"\n'
        'lower = "not shouted, so not a constant"\n'
        'FSTRING = f"interpolated {thing} and no more"\n'
        'NUMBER = 42\n'
        '\n'
        'def f():\n'
        '    LOCAL = "bound inside a function body"\n'
    )
    assert extract_claims.module_constants([str(path)]) == {
        "GOOD": "a real sentence about your speakers"}


def test_a_name_two_modules_disagree_on_resolves_to_neither(tmp_path):
    """The map is repo-wide, so one name can have two definitions. Guessing
    between them would decide a row on text from the wrong file."""
    (tmp_path / "a.py").write_text('HINT = "quit EasyEffects before you start"\n')
    (tmp_path / "b.py").write_text('HINT = "restart PipeWire before you start"\n')
    got = extract_claims.module_constants(
        [str(tmp_path / "a.py"), str(tmp_path / "b.py")])
    assert "HINT" not in got


# --------------------------------------------------------------------------
# Not counting an f-string's chunks alongside the whole f-string.
# --------------------------------------------------------------------------

def test_an_f_strings_chunks_are_not_rows_of_their_own(tmp_path, monkeypatch):
    chunk = "presets to the output directory you picked"
    rows = _harvest(tmp_path, monkeypatch,
                    'def show(n):\n'
                    '    cprint("head", f"Wrote {n} ' + chunk + '")\n')
    texts = [r["text"] for r in rows]
    assert "Wrote {n} " + chunk in texts
    # Non-vacuous: the chunk reads as copy on its own, so before this it was a
    # second row — a truncated half of the sentence above it, carrying its own
    # citable id and looking to a reviewer like a separate claim.
    assert extract_claims.is_copy(chunk)
    assert chunk not in texts


def test_dropping_a_chunk_never_drops_a_claim():
    """Skipping the chunks deduplicates only while the whole f-string is itself
    a row; where it is not, the same skip would lose the claim outright.

    It holds by containment — `literal()` builds the whole text out of the
    chunks verbatim, and each of the three filters is monotone in the text: a
    longer string is never under 12 characters, never loses a `_HAS_WORDS`
    match, and is code-ish only if every chunk of it is. The probe substitution
    is monotone the same way, since a chunk's `{NAME}` appears in the whole
    too. Cheap enough over the real tree to assert rather than argue.
    """
    sources = extract_claims.SCRIPTS | extract_claims.lib_modules()
    for rel in sorted(set(sources.values())):
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            whole = " ".join(extract_claims.literal(node).split())
            for value in node.values:
                if not (isinstance(value, ast.Constant)
                        and isinstance(value.value, str)):
                    continue
                chunk = " ".join(value.value.split())
                if not extract_claims.is_copy(chunk):
                    continue
                assert extract_claims.is_copy(whole), (
                    f"{rel}:{value.lineno}: the chunk {chunk!r} reads as copy "
                    f"but the f-string holding it, {whole!r}, does not — "
                    "skipping the chunk loses that claim instead of "
                    "deduplicating it"
                )
