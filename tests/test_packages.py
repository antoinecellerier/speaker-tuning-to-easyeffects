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
                            ("opensuse-tumbleweed", packages.SUSE)):
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
    """NixOS, Gentoo, Alpine, a container with no os-release.

    `""` has to travel all the way to an empty command: a family we cannot
    name must not fall back to whichever one this file happens to list first,
    because the reader would paste a package manager they do not have.
    """
    assert packages.family(tmp_path / "does-not-exist") == ""
    assert packages.family(_os_release(tmp_path, "ID=nixos\n")) == ""
    assert packages.install_command([packages.LSP_LV2], "") == ""
    # ...and every family's command is offered instead, so they can still act.
    offered = packages.install_commands([packages.LSP_LV2])
    assert len(offered) == len(packages.FAMILIES)


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
    message would name three distributions and silently drop the fourth."""
    for fam in packages.FAMILIES:
        assert packages.names([key], fam), f"{key} missing for {fam}"


def test_a_partial_row_drops_the_families_it_cannot_name():
    """`PW_TOOLS` is the deliberate exception, and must degrade, not guess.

    Only Debian's split-out package is verified. On the others the key has to
    vanish from the command rather than borrow Debian's name — `pipewire-bin`
    does not exist on Fedora, and a command that fails is worse than one line
    of prose saying where the tool comes from.
    """
    assert packages.PW_TOOLS not in packages.COMPLETE_KEYS
    assert packages.names([packages.PW_TOOLS], packages.DEBIAN) == [
        "pipewire-bin"]
    for fam in (packages.FEDORA, packages.SUSE, packages.ARCH):
        assert packages.names([packages.PW_TOOLS], fam) == []
    # And it folds into one command beside a key the family does have.
    assert packages.install_command(
        [packages.LV2INFO, packages.PW_TOOLS], packages.DEBIAN
    ) == "sudo apt install lilv-utils pipewire-bin"
    assert packages.install_command(
        [packages.LV2INFO, packages.PW_TOOLS], packages.FEDORA
    ) == "sudo dnf install lilv"


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


def test_print_install_hint_falls_back_to_every_family(monkeypatch):
    """No `cprint` line may carry a command for a machine we couldn't place."""
    lines: list[tuple[str, str]] = []
    monkeypatch.setattr(packages, "family", lambda *a, **k: "")
    packages.print_install_hint([packages.LSP_LV2],
                                lambda style, text="": lines.append((style, text)))
    commands = [t for s, t in lines if s == "cta"]
    assert len(commands) == len(packages.FAMILIES)
    assert any("zypper" in c for c in commands)

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
