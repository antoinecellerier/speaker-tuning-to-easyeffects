"""lib/packages.py: which package name a message may print.

A wrong package name is worse than none — it installs cleanly and leaves the
chain exactly as broken — so the tests that matter here are the ones about
*not* answering: an unplaceable machine, and a package a distribution splits
differently from the one we happen to develop on.
"""

from __future__ import annotations

import pytest

from lib import packages


def _os_release(tmp_path, body: str):
    path = tmp_path / "os-release"
    path.write_text(body)
    return path


def test_id_places_the_common_families(tmp_path):
    for ident, expected in (("debian", packages.DEBIAN),
                            ("ubuntu", packages.DEBIAN),
                            ("fedora", packages.FEDORA),
                            ("arch", packages.ARCH),
                            ("opensuse-tumbleweed", packages.SUSE),
                            ("alpine", packages.ALPINE),
                            ("gentoo", packages.GENTOO),
                            ("nixos", packages.NIXOS)):
        path = _os_release(tmp_path, f'ID={ident}\nPRETTY_NAME="x"\n')
        assert packages.family(path) == expected, ident


def test_id_like_places_the_derivatives(tmp_path):
    """The reason ID_LIKE is read at all: the derivative list is unbounded.

    Mint, Pop!_OS, EndeavourOS, CachyOS and the rest each ship their own ID,
    and enumerating them is a losing game — they all declare a parent.
    """
    path = _os_release(tmp_path, 'ID=linuxmint\nID_LIKE="ubuntu debian"\n')
    assert packages.family(path) == packages.DEBIAN

    path = _os_release(tmp_path, "ID=someneworkdistro\nID_LIKE=arch\n")
    assert packages.family(path) == packages.ARCH


def test_an_unplaceable_machine_gets_no_command(tmp_path):
    """Void, Solus, a container with no os-release — the ones still unnamed.

    NixOS used to stand here, and can't any more: it has a row, a `nix-shell`
    line and a `systemPackages` note of its own now, so leaving it as the
    example would have left this test guarding nothing. The representatives
    have to be distributions we genuinely cannot place.

    `""` has to travel all the way to an empty command: a family we cannot
    name must not fall back to whichever one this file happens to list first,
    because the reader would paste a package manager they do not have.
    """
    assert packages.family(tmp_path / "does-not-exist") == ""
    assert packages.family(_os_release(tmp_path, "ID=void\n")) == ""
    assert packages.family(_os_release(tmp_path, "ID=solus\n")) == ""
    assert packages.install_command([packages.LSP_LV2], "") == ""
    # ...and every family with a command to offer offers it instead, so they
    # can still act. NixOS is not one of them — it is spoken in prose by
    # `test_print_install_hint_falls_back_to_every_family`.
    offered = packages.install_commands([packages.LSP_LV2])
    assert [label for label, _command in offered] == [
        packages.LABELS[fam] for fam in packages.COMMAND_FAMILIES]


def test_quoting_and_junk_lines_survive_the_parse(tmp_path):
    path = _os_release(
        tmp_path,
        '# a comment\n\nID="fedora"\nPRETTY_NAME=\'Fedora Linux 43\'\n'
        "MALFORMED\n")
    assert packages.family(path) == packages.FEDORA
    assert packages.read_os_release(path)["PRETTY_NAME"] == "Fedora Linux 43"


@pytest.mark.parametrize("key", packages.COMPLETE_KEYS)
def test_every_family_has_a_name_for_every_key(key):
    """A half-filled row is the failure mode this table exists to prevent: the
    message would name five distributions and silently drop the sixth.

    Checked against `COMMAND_FAMILIES`, not `FAMILIES`: NixOS is out of this
    sweep because it has no `<prefix> <names>` command at all — its Python
    keys go through one `nix-shell` line and everything else through a
    `systemPackages` note — not because a row was forgotten there.
    """
    for fam in packages.COMMAND_FAMILIES:
        assert packages.names([key], fam), f"{key} missing for {fam}"


@pytest.mark.parametrize("fam", packages.FAMILIES)
@pytest.mark.parametrize("key", packages.ALL_KEYS)
def test_every_cell_is_either_a_name_or_a_spoken_gap(key, fam):
    """What `COMPLETE_KEYS` cannot cover: every key against every family.

    `COMPLETE_KEYS` only holds the keys no family is exempt from, so it stops
    watching exactly where the interesting rows start — Calf on openSUSE,
    `pw-cli` on Arch, rich-argparse on Alpine, all of NixOS. Each of those
    still owes the reader the other half: a line saying where the thing comes
    from. A cell in neither table is a key that drops out of the command
    without a word, which is a command that installs less than the reader was
    told it would — and reports success doing it.
    """
    assert packages.names([key], fam) or (key, fam) in packages.UNPACKAGED, \
        f"{key} on {fam} is neither named nor explained"


def test_a_gap_is_spoken_not_silently_dropped():
    """The failure the openSUSE Calf row taught: a name that doesn't resolve.

    `lv2-calf` exists only in Packman, so `zypper install lv2-lsp-plugins
    lv2-calf` fails to resolve on a stock system — and because both names ride
    one transaction, the reader ends up with neither. Dropping the key silently
    is the other half of the trap: the command then installs one package and
    reports success, and the chain still won't load. So the gap has to print.
    """
    assert packages.names([packages.CALF_LV2], packages.SUSE) == []
    assert (packages.CALF_LV2, packages.SUSE) in packages.UNPACKAGED

    lines: list[tuple[str, str]] = []
    orig = packages.family
    packages.family = lambda *a, **k: packages.SUSE
    try:
        packages.print_install_hint(
            [packages.LSP_LV2, packages.CALF_LV2],
            lambda style, text="": lines.append((style, text)))
    finally:
        packages.family = orig
    assert [t for s, t in lines if s == "cta"] == [
        "  sudo zypper install lv2-lsp-plugins"]
    assert any("Packman" in t for s, t in lines if s == "dim"), lines


def test_a_placed_reader_never_gets_another_family_command(monkeypatch):
    """The fallback is for a machine we could not place, and only that one.

    When every requested key is unpackaged for a known family, the helper used
    to fall through to the all-families list — handing an Arch reader
    `sudo apt install pipewire-bin`. `PW_TOOLS` on Fedora no longer exercises
    that (Fedora has `pipewire-utils` now), so the pairs asked for here are
    ones whose row is still genuinely empty: `spa-json-dump` on Arch, where it
    ships inside pipewire itself, and Calf on openSUSE, where it ships nowhere.
    """
    managers = ("apt", "dnf", "zypper", "pacman", "apk", "emerge", "nix-shell")
    for fam, keys in ((packages.ARCH, [packages.SPA_TOOLS]),
                      (packages.SUSE, [packages.CALF_LV2])):
        lines: list[tuple[str, str]] = []
        monkeypatch.setattr(packages, "family", lambda *a, **k: fam)
        packages.print_install_hint(
            keys, lambda style, text="": lines.append((style, text)))
        assert not any(m in t for _s, t in lines for m in managers), lines
        # Not silence either: the reader still learns where the thing lives.
        assert [s for s, _t in lines] == ["dim"], lines


def test_a_partial_row_drops_the_families_it_cannot_name():
    """`PW_TOOLS` is the deliberate exception, and must degrade, not guess.

    Four families ship a separate package for `pw-cli`/`pw-dump`; on Arch and
    Gentoo it ships inside pipewire itself. There the key has to vanish from
    the command rather than borrow another family's name — `pipewire-bin` does
    not exist on Arch, and a command that fails to resolve is worse than one
    line of prose saying where the tool comes from.
    """
    assert packages.PW_TOOLS not in packages.COMPLETE_KEYS
    assert packages.CALF_LV2 not in packages.COMPLETE_KEYS
    for fam, expected in ((packages.DEBIAN, "pipewire-bin"),
                          (packages.FEDORA, "pipewire-utils"),
                          (packages.SUSE, "pipewire-tools"),
                          (packages.ALPINE, "pipewire-tools")):
        assert packages.names([packages.PW_TOOLS], fam) == [expected], fam
    for fam in (packages.ARCH, packages.GENTOO, packages.NIXOS):
        assert packages.names([packages.PW_TOOLS], fam) == [], fam
    # And it folds into one command beside a key the family does have.
    assert packages.install_command(
        [packages.LV2INFO, packages.PW_TOOLS], packages.DEBIAN
    ) == "sudo apt install lilv-utils pipewire-bin"
    assert packages.install_command(
        [packages.LV2INFO, packages.PW_TOOLS], packages.ARCH
    ) == "sudo pacman -S lilv-tools"


def test_spa_tools_is_not_pipewire_tools_where_the_spec_splits_them():
    """One key for both would have named a package that fixes nothing.

    openSUSE's pipewire.spec puts `pw-cli` and `pw-dump` in `%files tools`
    (`pipewire-tools`) and `spa-json-dump` in `%files spa-tools`
    (`pipewire-spa-tools`), and Alpine splits them the same way. Conflating
    the two told an openSUSE reader whose `spa-json-dump` is missing to
    install `pipewire-tools` — which resolves, installs cleanly, and changes
    nothing, the exact shape of the wrong-sub-package trap this module exists
    to stop.
    """
    for fam in (packages.SUSE, packages.ALPINE):
        assert packages.names([packages.SPA_TOOLS], fam) == [
            "pipewire-spa-tools"], fam
        assert packages.names([packages.PW_TOOLS], fam) == [
            "pipewire-tools"], fam
    # Where one package carries both, both keys must land on that one name —
    # the split is openSUSE's and Alpine's, not something to invent elsewhere.
    for fam, expected in ((packages.DEBIAN, "pipewire-bin"),
                          (packages.FEDORA, "pipewire-utils")):
        assert packages.names([packages.PW_TOOLS], fam) == [expected], fam
        assert packages.names([packages.SPA_TOOLS], fam) == [expected], fam


def test_the_lv2_split_packages_are_not_the_base_ones():
    """The trap that made this module necessary.

    The README used to say LSP is `lsp-plugins` on Fedora and Arch. It is
    `lsp-plugins-lv2` on both — the base package does not ship the .lv2 bundle
    PipeWire loads, so the old advice installed something and left the chain
    just as unloadable. openSUSE reverses the word order again, and Calf and
    lilv each split differently.
    """
    assert packages.names([packages.LSP_LV2], packages.FEDORA) == [
        "lsp-plugins-lv2"]
    assert packages.names([packages.LSP_LV2], packages.ARCH) == [
        "lsp-plugins-lv2"]
    assert packages.names([packages.LSP_LV2], packages.SUSE) == [
        "lv2-lsp-plugins"]
    assert packages.names([packages.CALF_LV2], packages.FEDORA) == [
        "lv2-calf-plugins"]
    # lv2info is in `lilv-tools` on Arch, not `lilv` as on Fedora/openSUSE.
    assert packages.names([packages.LV2INFO], packages.ARCH) == ["lilv-tools"]


def test_install_command_uses_each_family_package_manager():
    assert packages.install_command(
        [packages.LSP_LV2, packages.CALF_LV2], packages.DEBIAN
    ) == "sudo apt install lsp-plugins-lv2 calf-plugins"
    assert packages.install_command(
        [packages.LV2INFO], packages.ARCH) == "sudo pacman -S lilv-tools"


def test_install_steps_is_unindented_and_the_hint_owns_the_margin(monkeypatch):
    """The margin belongs to the caller, and there is more than one now.

    `install_steps` is the primitive: a `CheckResult`'s steps print under the
    doctor's gutter, a raised failure's `next_step` under none, and only
    `print_install_hint` under this project's two-space message margin. If the
    two drift apart the same remedy starts being worded — or indented — two
    ways, which is what having one builder was for. So the whole difference
    between them must stay exactly `"  "`, on every line, in every branch:
    placed with a command, placed with a gap, NixOS, and unplaceable.
    """
    keys = [packages.LSP_LV2, packages.CALF_LV2]
    for fam in (packages.DEBIAN, packages.SUSE, packages.NIXOS, ""):
        monkeypatch.setattr(packages, "family", lambda *a, **k: fam)
        lines: list[tuple[str, str]] = []
        packages.print_install_hint(
            keys, lambda style, text="": lines.append((style, text)))
        steps = packages.install_steps(keys)
        assert steps, fam
        assert all(text == text.lstrip() for _style, text in steps), fam
        assert lines == [(style, f"  {text}") for style, text in steps], fam


def test_a_gentoo_caveat_prints_after_the_command_not_instead_of_it(monkeypatch):
    """An atom that resolves and still builds without the .lv2 bundle.

    `media-plugins/calf` is the right atom and `emerge` installs it happily,
    but USE=lv2 is off by default — so the reader lands in the trap a wrong
    sub-package sets elsewhere: a clean install and a chain that still won't
    load. The command is right as far as it goes, which is why the caveat
    rides after it instead of replacing it; dropping the command would send a
    Gentoo reader looking for a package that is right there.
    """
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.GENTOO)
    packages.print_install_hint(
        [packages.LSP_LV2, packages.CALF_LV2],
        lambda style, text="": lines.append((style, text)))
    assert [t for s, t in lines if s == "cta"] == [
        "  sudo emerge media-libs/lsp-plugins media-plugins/calf"]
    assert any("USE=lv2" in t for s, t in lines if s == "dim"), lines
    # And no caveat where the ebuild already answers: lsp-plugins has `+lv2`,
    # so a note there would be a warning about nothing.
    assert (packages.LSP_LV2, packages.GENTOO) not in packages.CAVEATS


def test_nixos_gets_a_shell_for_python_and_a_note_for_the_daemon(monkeypatch):
    """A `nix-shell` does not reach the PipeWire daemon.

    The script's own imports live inside the shell, so `nix-shell -p
    python3.withPackages` is a real, pasteable answer for numpy and scipy. An
    LV2 plugin installed into that same shell is invisible to the daemon that
    has to load it, so a command there would be confidently wrong — worse than
    the silence this module refuses elsewhere, because it looks like it worked.
    Those keys get the attribute name and `environment.systemPackages` instead,
    and the only command printed beside them is the `nixos-rebuild switch` that
    makes that edit take effect — never a `nix-shell` carrying the plugin.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.NIXOS)

    lines: list[tuple[str, str]] = []
    packages.print_install_hint(
        [packages.NUMPY, packages.SCIPY],
        lambda style, text="": lines.append((style, text)))
    assert [t for s, t in lines if s == "cta"] == [
        '  nix-shell -p "python3.withPackages (ps: with ps; '
        '[ numpy scipy ])"']

    lines.clear()
    packages.print_install_hint(
        [packages.LSP_LV2],
        lambda style, text="": lines.append((style, text)))
    assert [t for s, t in lines if s == "cta"] == ["  sudo nixos-rebuild switch"]
    assert not any("nix-shell" in t for s, t in lines if s == "cta"), lines
    assert any("environment.systemPackages" in t
               for s, t in lines if s == "dim"), lines


def test_alpine_has_no_rich_argparse_and_the_command_drops_it(monkeypatch):
    """`apk add` fails the whole transaction on one unknown name.

    There is no `py3-rich-argparse` in Alpine's repositories. Naming it beside
    the packages that do exist would not cost the reader one nicety — it would
    abort the install and leave them with none of them. That is the openSUSE
    Calf trap again, on a different package manager, which is why the row is
    short and the gap is spoken.
    """
    assert packages.names([packages.RICH_ARGPARSE], packages.ALPINE) == []
    assert (packages.RICH_ARGPARSE, packages.ALPINE) in packages.UNPACKAGED
    assert packages.install_command(
        [packages.RICH, packages.RICH_ARGPARSE], packages.ALPINE
    ) == "sudo apk add py3-rich"

    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.ALPINE)
    packages.print_install_hint(
        [packages.RICH, packages.RICH_ARGPARSE],
        lambda style, text="": lines.append((style, text)))
    assert [t for s, t in lines if s == "cta"] == ["  sudo apk add py3-rich"]
    assert any("rich-argparse" in t for s, t in lines if s == "dim"), lines


def test_print_install_hint_forwards_the_readme_section_it_was_given(monkeypatch):
    """The pointer is a parameter for a reason, and nothing was checking it.

    Two kinds of dependency live in two different README sections — the LV2
    plugins in one, the Python modules in another — and `print_install_hint`
    only passes the caller's `see` through. Crossed, the two would still print
    a plausible line: someone whose numpy is missing gets sent to the plugin
    section, reads about lsp-plugins, and finds nothing about the thing that
    actually stopped the run. That is the wrong-package trap one layer up, and
    it would be as silent.

    Stubbed to an unplaceable machine because that is the only branch that
    prints the pointer at all: a reader we could place gets the command and
    nothing else.
    """
    monkeypatch.setattr(packages, "family", lambda *a, **k: "")
    sections = (packages.README_SECTION, packages.README_INSTALL_SECTION)
    for keys, see in (([packages.LSP_LV2], packages.README_SECTION),
                      ([packages.NUMPY], packages.README_INSTALL_SECTION)):
        lines: list[tuple[str, str]] = []
        packages.print_install_hint(
            keys, lambda style, text="": lines.append((style, text)), see)
        pointer = [t for _s, t in lines if "another distribution" in t]
        assert len(pointer) == 1, lines
        assert see in pointer[0]
        assert not any(other in pointer[0] for other in sections
                       if other != see), pointer

    # And the default is the plugin section, so the callers that don't say
    # keep pointing where they always did.
    lines = []
    packages.print_install_hint([packages.LSP_LV2],
                                lambda style, text="": lines.append((style, text)))
    assert any(packages.README_SECTION in t for _s, t in lines), lines


def test_print_install_hint_falls_back_to_every_family(monkeypatch):
    """No `cprint` line may carry a command for a machine we couldn't place.

    The count is one per *command* family, not one per family: NixOS has no
    `<prefix> <names>` line for an LV2 plugin at all, so `len(FAMILIES)` would
    demand a command this module is deliberately not inventing. It owes the
    NixOS reader prose instead, and that still has to be there.
    """
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(packages, "family", lambda *a, **k: "")
    packages.print_install_hint([packages.LSP_LV2],
                                lambda style, text="": lines.append((style, text)))
    commands = [t for s, t in lines if s == "cta"]
    assert len(commands) == len(packages.COMMAND_FAMILIES)
    assert any("zypper" in c for c in commands)
    assert not any(packages.LABELS[packages.NIXOS] in c for c in commands)
    assert any("environment.systemPackages" in t
               for s, t in lines if s == "dim"), lines

    lines.clear()
    monkeypatch.setattr(packages, "family", lambda *a, **k: packages.FEDORA)
    packages.print_install_hint([packages.LSP_LV2],
                                lambda style, text="": lines.append((style, text)))
    commands = [t for s, t in lines if s == "cta"]
    assert commands == ["  sudo dnf install lsp-plugins-lv2"]
    # And nothing else. A reader we placed does not need a caption naming the
    # distributions they are not on, and this prints on an error screen where
    # every extra line pushes the next step further down.
    assert [t for s, t in lines if s == "dim"] == []
