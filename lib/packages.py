"""Which package carries a dependency, on each distribution family.

Package names are per-distribution facts, and they differ more than they look
like they should: the LV2 build of LSP is ``lsp-plugins-lv2`` on Debian,
Fedora and Arch but ``lv2-lsp-plugins`` on openSUSE; Calf's is
``calf-plugins``, ``lv2-calf-plugins``, ``calf`` and ``lv2-calf`` across the
four; and ``lv2info`` lives in ``lilv-utils``, ``lilv`` and ``lilv-tools``
depending on where you are. A message that names one distribution's package
sends everyone else to a package that does not exist — and naming the wrong
*sub*-package is worse than naming none, because it installs cleanly and
still leaves the chain unable to load.

Stdlib-only, like ``version.py`` and ``ee_paths.py``: this is imported by both
PipeWire scripts and must not drag anything in.

Names verified against repology.org's per-repository binary lists on
2026-08-22 (Debian 13, Ubuntu 24.04, Fedora 43, Arch extra, openSUSE
Tumbleweed). The LV2 build is the one that matters — the base ``lsp-plugins``
and ``calf`` packages do not all ship the .lv2 bundle PipeWire loads.
"""

from __future__ import annotations

from pathlib import Path

DEBIAN, FEDORA, ARCH, SUSE = "debian", "fedora", "arch", "suse"

# Ordered as the README lists them, so the two read alike.
FAMILIES = (DEBIAN, FEDORA, SUSE, ARCH)

LABELS = {
    DEBIAN: "Debian/Ubuntu/Mint/Pop!_OS",
    FEDORA: "Fedora/RHEL/Rocky/Alma",
    SUSE: "openSUSE",
    ARCH: "Arch/Manjaro/EndeavourOS",
}

_INSTALL = {
    DEBIAN: "sudo apt install",
    FEDORA: "sudo dnf install",
    SUSE: "sudo zypper install",
    ARCH: "sudo pacman -S",
}

# Keys are what a caller means, not what any one distribution calls it.
LSP_LV2 = "lsp-lv2"
CALF_LV2 = "calf-lv2"
LV2INFO = "lv2info"
PW_TOOLS = "pipewire-tools"

_NAMES = {
    LSP_LV2: {DEBIAN: "lsp-plugins-lv2", FEDORA: "lsp-plugins-lv2",
              SUSE: "lv2-lsp-plugins", ARCH: "lsp-plugins-lv2"},
    # No openSUSE row on purpose: Calf reaches openSUSE only through Packman,
    # a third-party repository, so `lv2-calf` does not resolve on a stock
    # system — and naming it beside LSP in one `zypper install` would fail the
    # whole transaction and leave the reader with neither.
    CALF_LV2: {DEBIAN: "calf-plugins", FEDORA: "lv2-calf-plugins",
               ARCH: "calf"},
    LV2INFO: {DEBIAN: "lilv-utils", FEDORA: "lilv",
              SUSE: "lilv", ARCH: "lilv-tools"},
    # Deliberately partial, and the one row that may be: `spa-json-dump` ships
    # with PipeWire itself, which every reader of these messages is already
    # running, so the only thing worth naming is the split-out tools package
    # where a distribution has one. Debian's is verified; the rest are left
    # unclaimed rather than guessed, and `names` drops a family it lacks.
    PW_TOOLS: {DEBIAN: "pipewire-bin"},
}

# Keys every family must name — a half-filled row here is a message that
# covers three distributions and silently drops the fourth. The two exempt
# keys are exempt for a stated reason, and each owes the reader a line saying
# where the thing comes from instead.
COMPLETE_KEYS = (LSP_LV2, LV2INFO)

# What to say when a family has no package for a key. A gap has to be spoken:
# dropping the key silently turns "install these two" into a command that
# installs one and reports success.
UNPACKAGED = {
    (CALF_LV2, SUSE): "Calf is not in openSUSE's own repositories — it comes "
                      "from Packman",
    (PW_TOOLS, FEDORA): "spa-json-dump ships with PipeWire's own command-line "
                        "tools",
    (PW_TOOLS, SUSE): "spa-json-dump ships with PipeWire's own command-line "
                      "tools",
    (PW_TOOLS, ARCH): "spa-json-dump ships with PipeWire's own command-line "
                      "tools",
}


# ID / ID_LIKE tokens from /etc/os-release. ID_LIKE is what makes the
# derivatives work without listing every one: Mint says `ID_LIKE=ubuntu
# debian`, EndeavourOS says `ID_LIKE=arch`.
_FAMILY_OF = {
    "debian": DEBIAN, "ubuntu": DEBIAN, "linuxmint": DEBIAN, "pop": DEBIAN,
    "raspbian": DEBIAN, "devuan": DEBIAN,
    "fedora": FEDORA, "rhel": FEDORA, "centos": FEDORA, "rocky": FEDORA,
    "almalinux": FEDORA,
    "opensuse": SUSE, "opensuse-leap": SUSE, "opensuse-tumbleweed": SUSE,
    "suse": SUSE, "sles": SUSE,
    "arch": ARCH, "archlinux": ARCH, "manjaro": ARCH, "endeavouros": ARCH,
    "cachyos": ARCH, "garuda": ARCH,
}


def read_os_release(path=Path("/etc/os-release")) -> dict[str, str]:
    """The os-release keys, unquoted. ``{}`` when the file can't be read.

    Hand-parsed rather than `platform.freedesktop_os_release()`, which needs
    Python 3.10 — the README asks only for "Python 3", and a package hint is
    not worth narrowing that. Takes a path so the families below are testable
    without the machine the test runs on deciding the answer.
    """
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def family(path=Path("/etc/os-release")) -> str:
    """Which of `FAMILIES` this machine is, or "" when we can't tell.

    "" is a real answer and callers must handle it: NixOS, Gentoo, Alpine, a
    container with no os-release, and anything new all land there, and a
    wrong install command is worse for them than none.
    """
    data = read_os_release(path)
    ident = data.get("ID", "").strip().lower()
    if ident in _FAMILY_OF:
        return _FAMILY_OF[ident]
    for token in data.get("ID_LIKE", "").lower().split():
        if token in _FAMILY_OF:
            return _FAMILY_OF[token]
    return ""


def names(keys, fam: str) -> list[str]:
    """The package names for `keys` on `fam`, in the order given."""
    return [_NAMES[k][fam] for k in keys if fam in _NAMES.get(k, {})]


def install_command(keys, fam: str) -> str:
    """The one command to paste on `fam`, or "" when we don't know `fam`."""
    wanted = names(keys, fam)
    if not fam or not wanted:
        return ""
    return f"{_INSTALL[fam]} {' '.join(wanted)}"


def install_commands(keys) -> list[tuple[str, str]]:
    """`(label, command)` for every family — for the reader we can't place."""
    return [(LABELS[f], install_command(keys, f)) for f in FAMILIES
            if install_command(keys, f)]


# The README section that stays right for a distribution this table does not
# list. Named, not linked: these are terminal messages, and the one-link rule
# in .claude/rules/user-messages.md keeps URLs out of message bodies.
README_SECTION = 'the README section "Plugin dependencies and validation"'


def print_install_hint(keys, cprint) -> None:
    """Print how to install `keys`, for this machine where we can tell.

    One command when os-release places the reader, every family's when it
    doesn't: a reader on NixOS or Alpine gets nothing from a `sudo apt` line,
    and everyone else gets a wall of four commands to find themselves in.

    Takes the caller's `cprint` for the reason `lib.doctor`'s printers do —
    this module is stdlib-only and imported by both PipeWire scripts, and an
    `import console` here would close a cycle.
    """
    fam = family()
    if fam:
        packaged = [k for k in keys if names([k], fam)]
        if packaged:
            # Just the command. A reader we could place needs no note about
            # the distributions they are not on, and the README pointer earns
            # its line only where we have nothing better — here it would push
            # an actionable error screen further down for no one's benefit. A
            # wrong guess is self-announcing: `sudo apt` on a Fedora box needs
            # no caption.
            cprint("cta", f"  {install_command(packaged, fam)}")
        # Every key the command could not carry, so a shorter command than the
        # reader expected is explained rather than just shorter.
        for key in keys:
            if key not in packaged:
                cprint("dim", f"  ({UNPACKAGED.get((key, fam), key)})")
        if packaged or len(keys) > len(packaged):
            return
    for label, alt in install_commands(keys):
        cprint("cta", f"  {label}: {alt}")
    for (key, gap_fam), note in sorted(UNPACKAGED.items()):
        if key in keys and gap_fam in FAMILIES:
            cprint("dim", f"  ({LABELS[gap_fam]}: {note})")
    cprint("dim", f"  (on another distribution, see {README_SECTION})")
