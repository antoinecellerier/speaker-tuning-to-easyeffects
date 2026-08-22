"""Which package carries a dependency, on each distribution family.

Package names are per-distribution facts, and they differ more than they look
like they should: the LV2 build of LSP is ``lsp-plugins-lv2`` on Debian,
Fedora, Arch and Alpine but ``lv2-lsp-plugins`` on openSUSE; Calf's is
``calf-plugins``, ``lv2-calf-plugins``, ``calf`` and ``calf-lv2`` across the
rest; and ``lv2info`` lives in ``lilv-utils``, ``lilv`` and ``lilv-tools``
depending on where you are. A message that names one distribution's package
sends everyone else to a package that does not exist — and naming the wrong
*sub*-package is worse than naming none, because it installs cleanly and
still leaves the chain unable to load.

Stdlib-only, like ``version.py`` and ``ee_paths.py``: this is imported by both
PipeWire scripts and must not drag anything in
(``tests/test_layout.py``'s ``STDLIB_ONLY``).

Names verified on 2026-08-22 against each distribution's own binary index,
not against a project page: repology tracks Debian, Fedora and openSUSE at
*source* granularity, so its listing says ``lsp-plugins`` where the installable
package is ``lsp-plugins-lv2``. What was actually consulted:
``apt-cache show`` on Debian sid, Fedora's mdapi (rawhide), the openSUSE
Factory binary index and the ``pipewire`` spec's own ``%files`` sections,
archlinux.org's package API, pkgs.alpinelinux.org (including its file-contents
index), packages.gentoo.org plus the ebuilds' ``IUSE``, and nixpkgs master.
The LV2 build is the one that matters — the base ``lsp-plugins`` and ``calf``
packages do not all ship the .lv2 bundle PipeWire loads.
"""

from __future__ import annotations

from pathlib import Path

DEBIAN = "debian"
FEDORA = "fedora"
SUSE = "suse"
ARCH = "arch"
ALPINE = "alpine"
GENTOO = "gentoo"
NIXOS = "nixos"

# Ordered as the README lists them, so the two read alike.
FAMILIES = (DEBIAN, FEDORA, SUSE, ARCH, ALPINE, GENTOO, NIXOS)

LABELS = {
    DEBIAN: "Debian/Ubuntu/Mint/Pop!_OS",
    FEDORA: "Fedora/RHEL/Rocky/Alma",
    SUSE: "openSUSE",
    ARCH: "Arch/Manjaro/EndeavourOS",
    ALPINE: "Alpine",
    GENTOO: "Gentoo",
    NIXOS: "NixOS",
}

_INSTALL = {
    DEBIAN: "sudo apt install",
    FEDORA: "sudo dnf install",
    SUSE: "sudo zypper install",
    ARCH: "sudo pacman -S",
    ALPINE: "sudo apk add",
    GENTOO: "sudo emerge",
    # NixOS has no "install this package" verb — see `_nixos_command`.
    NIXOS: "",
}

# The families whose install idiom really is "<prefix> <names>". NixOS is the
# one that isn't, and every invariant below that assumes a pasteable command
# is checked against this rather than `FAMILIES`.
COMMAND_FAMILIES = tuple(f for f in FAMILIES if _INSTALL[f])

# Keys are what a caller means, not what any one distribution calls it.
LSP_LV2 = "lsp-lv2"
CALF_LV2 = "calf-lv2"
LV2INFO = "lv2info"
PW_TOOLS = "pipewire-tools"
SPA_TOOLS = "spa-tools"
ALSA_UTILS = "alsa-utils"
EASYEFFECTS = "easyeffects"
NUMPY = "numpy"
SCIPY = "scipy"
RICH = "rich"
RICH_ARGPARSE = "rich-argparse"

PYTHON_KEYS = (NUMPY, SCIPY, RICH, RICH_ARGPARSE)

# The keys a `nix-shell` genuinely satisfies: the things *this process* runs —
# its own interpreter's modules, and the CLIs it execs. The rest are what the
# PipeWire daemon or the user's desktop has to find, and those live outside
# any shell this tool could suggest, so on NixOS they get a note about
# `environment.systemPackages` instead of a command. The distinction only
# matters there; every other family installs into the system either way.
NIX_SHELL_KEYS = PYTHON_KEYS + (LV2INFO, ALSA_UTILS)

_NAMES = {
    LSP_LV2: {DEBIAN: "lsp-plugins-lv2", FEDORA: "lsp-plugins-lv2",
              SUSE: "lv2-lsp-plugins", ARCH: "lsp-plugins-lv2",
              ALPINE: "lsp-plugins-lv2", GENTOO: "media-libs/lsp-plugins"},
    # No openSUSE row on purpose: Calf reaches openSUSE only through Packman,
    # a third-party repository, so `lv2-calf` does not resolve on a stock
    # system — and naming it beside LSP in one `zypper install` would fail the
    # whole transaction and leave the reader with neither.
    CALF_LV2: {DEBIAN: "calf-plugins", FEDORA: "lv2-calf-plugins",
               ARCH: "calf", ALPINE: "calf-lv2",
               GENTOO: "media-plugins/calf"},
    # NixOS has a row where the LV2 plugins do not, and the difference is who
    # runs the thing: `lv2info` is exec'd by this process, so a `nix-shell`
    # reaches it. nixpkgs puts lilv's binaries in the default `out` output.
    LV2INFO: {DEBIAN: "lilv-utils", FEDORA: "lilv",
              SUSE: "lilv", ARCH: "lilv-tools", ALPINE: "lilv",
              GENTOO: "media-libs/lilv", NIXOS: "lilv"},
    # `pw-cli` and `pw-dump` on one row, `spa-json-dump` on the next, because
    # openSUSE and Alpine ship them in *different* packages — openSUSE's
    # pipewire.spec puts `pw-*` in `%files tools` and `spa-json-dump` in
    # `%files spa-tools`. One key for both would have told an openSUSE reader
    # whose spa-json-dump is missing to install `pipewire-tools`, which
    # installs cleanly and changes nothing.
    PW_TOOLS: {DEBIAN: "pipewire-bin", FEDORA: "pipewire-utils",
               SUSE: "pipewire-tools", ALPINE: "pipewire-tools"},
    SPA_TOOLS: {DEBIAN: "pipewire-bin", FEDORA: "pipewire-utils",
                SUSE: "pipewire-spa-tools", ALPINE: "pipewire-spa-tools"},
    ALSA_UTILS: {DEBIAN: "alsa-utils", FEDORA: "alsa-utils",
                 SUSE: "alsa-utils", ARCH: "alsa-utils", ALPINE: "alsa-utils",
                 GENTOO: "media-sound/alsa-utils", NIXOS: "alsa-utils"},
    EASYEFFECTS: {DEBIAN: "easyeffects", FEDORA: "easyeffects",
                  SUSE: "easyeffects", ARCH: "easyeffects",
                  ALPINE: "easyeffects", GENTOO: "media-sound/easyeffects",
                  NIXOS: "easyeffects"},
    NUMPY: {DEBIAN: "python3-numpy", FEDORA: "python3-numpy",
            SUSE: "python3-numpy", ARCH: "python-numpy", ALPINE: "py3-numpy",
            GENTOO: "dev-python/numpy", NIXOS: "numpy"},
    SCIPY: {DEBIAN: "python3-scipy", FEDORA: "python3-scipy",
            SUSE: "python3-scipy", ARCH: "python-scipy", ALPINE: "py3-scipy",
            GENTOO: "dev-python/scipy", NIXOS: "scipy"},
    RICH: {DEBIAN: "python3-rich", FEDORA: "python3-rich",
           SUSE: "python3-rich", ARCH: "python-rich", ALPINE: "py3-rich",
           GENTOO: "dev-python/rich", NIXOS: "rich"},
    # No Alpine row: `py3-rich-argparse` does not exist there, and `apk add`
    # fails the whole transaction on one unknown name — so naming it would
    # have cost the reader the three packages that do exist.
    RICH_ARGPARSE: {DEBIAN: "python3-rich-argparse",
                    FEDORA: "python3-rich-argparse",
                    SUSE: "python3-rich-argparse",
                    ARCH: "python-rich-argparse",
                    GENTOO: "dev-python/rich-argparse",
                    NIXOS: "rich-argparse"},
}

ALL_KEYS = tuple(_NAMES)

# Keys every command family must name — a half-filled row here is a message
# that covers five distributions and silently drops the sixth. Only the two
# without a single exempt family qualify; the rest each owe the reader a line
# saying where the thing comes from instead, via `UNPACKAGED` below.
COMPLETE_KEYS = (LSP_LV2, LV2INFO, ALSA_UTILS, EASYEFFECTS, NUMPY, SCIPY,
                 RICH)

# What to say when a family has no package for a key. A gap has to be spoken:
# dropping the key silently turns "install these two" into a command that
# installs one and reports success.
# Phrased so the verb agrees whichever way it is filled in: one of these
# rows names a single tool and the other names two.
_PW_OWN = "the pipewire package itself carries {}"
_NIX_SYSTEM = (
    "add {} to environment.systemPackages and run nixos-rebuild switch — a "
    "nix-shell doesn't reach the PipeWire daemon")

UNPACKAGED = {
    (CALF_LV2, SUSE): "Calf is not in openSUSE's own repositories — add the "
                      "Packman repository to install it",
    (RICH_ARGPARSE, ALPINE): "Alpine has no rich-argparse package; --help "
                             "stays plain without it",
    (PW_TOOLS, ARCH): _PW_OWN.format("pw-cli and pw-dump"),
    (PW_TOOLS, GENTOO): _PW_OWN.format("pw-cli and pw-dump"),
    (SPA_TOOLS, ARCH): _PW_OWN.format("spa-json-dump"),
    (SPA_TOOLS, GENTOO): _PW_OWN.format("spa-json-dump"),
    (PW_TOOLS, NIXOS): _PW_OWN.format("pw-cli and pw-dump"),
    (SPA_TOOLS, NIXOS): _PW_OWN.format("spa-json-dump"),
}

# What NixOS installs system-wide rather than into a shell, by attribute.
# These are the things the PipeWire daemon or the desktop has to find, and a
# `nix-shell` never reaches either. Kept as its own table because the answer
# is one declarative instruction for however many packages were asked for —
# rendered per key, three missing plugins repeated the same long sentence
# three times. `UNPACKAGED` is filled from it so a cell is still *spoken* when
# the fallback lists every family one by one.
_NIXOS_SYSTEM = {
    LSP_LV2: "pkgs.lsp-plugins",
    CALF_LV2: "pkgs.calf",
    EASYEFFECTS: "pkgs.easyeffects",
}

UNPACKAGED.update({
    (key, NIXOS): _NIX_SYSTEM.format(attr)
    for key, attr in _NIXOS_SYSTEM.items()
})

# A name that resolves, and a command that still leaves the reader without the
# thing. Gentoo builds these behind USE flags that are not on by default, so
# the atom alone is the same trap as a wrong sub-package elsewhere — printed
# *before* the command rather than instead of it: the command is right as far
# as it goes, but a block read top to bottom is a block pasted top to bottom,
# and a caveat underneath arrives after the build it was meant to change.
# (`media-libs/lsp-plugins` needs no caveat: its ebuild has `+lv2`.)
CAVEATS = {
    (CALF_LV2, GENTOO): "first set USE=lv2 for media-plugins/calf — the "
                        "default build ships no .lv2 bundle",
    (LV2INFO, GENTOO): "first set USE=tools for media-libs/lilv — lv2info is "
                       "not in the default build",
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
    "alpine": ALPINE, "postmarketos": ALPINE,
    "gentoo": GENTOO, "funtoo": GENTOO,
    "nixos": NIXOS,
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

    "" is a real answer and callers must handle it: Void, Solus, a container
    with no os-release, and anything new all land there, and a wrong install
    command is worse for them than none.
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
    """The package names for `keys` on `fam`, in the order given.

    Deduplicated, because two keys can share a package: `pw-dump` and
    `spa-json-dump` are separate keys precisely because openSUSE and Alpine
    split them, and on Debian both resolve to `pipewire-bin` — which listed
    twice makes a correct command look like a bug.
    """
    return list(dict.fromkeys(
        _NAMES[k][fam] for k in keys if fam in _NAMES.get(k, {})))


def _nixos_command(keys) -> str:
    """NixOS's one runnable line, or "" when it has none for these keys.

    A `nix-shell` is the right answer for what this process runs — its own
    modules, `lv2info`, `amixer` — and the wrong answer for everything else,
    because the PipeWire daemon lives outside that shell and would never see
    an LV2 plugin installed into it. Those keys are in `UNPACKAGED` instead,
    which says what to do with the attribute rather than pretending there is a
    command.

    Python modules need the `withPackages` form rather than a bare attribute,
    so a request spanning both kinds renders as one shell with two arguments.
    """
    modules = names([k for k in keys if k in PYTHON_KEYS], NIXOS)
    plain = names([k for k in keys
                   if k in NIX_SHELL_KEYS and k not in PYTHON_KEYS], NIXOS)
    if not (modules or plain):
        return ""
    parts = list(plain)
    if modules:
        parts.append('"python3.withPackages (ps: with ps; [ '
                     + " ".join(modules) + ' ])"')
    return "nix-shell -p " + " ".join(parts)


def install_command(keys, fam: str) -> str:
    """The one command to paste on `fam`, or "" when we don't know `fam`."""
    if not fam:
        return ""
    if fam == NIXOS:
        return _nixos_command(keys)
    wanted = names(keys, fam)
    if not wanted:
        return ""
    return f"{_INSTALL[fam]} {' '.join(wanted)}"


def install_commands(keys) -> list[tuple[str, str]]:
    """`(label, command)` for every family — for the reader we can't place."""
    return [(LABELS[f], install_command(keys, f)) for f in FAMILIES
            if install_command(keys, f)]


def _covered(keys, fam: str) -> list[str]:
    """The keys `fam`'s own command actually carries."""
    if fam == NIXOS:
        return [k for k in keys
                if k in NIX_SHELL_KEYS and NIXOS in _NAMES.get(k, {})]
    return [k for k in keys if fam in _NAMES.get(k, {})]


# How to ask a package manager what it *would* install, without installing it.
# Only where the answer is local and cheap — `apt-cache policy` is ~20 ms
# against the lists already on disk, and every entry here is pinned to
# whatever metadata the machine has rather than allowed to fetch. Gentoo and
# NixOS are absent because neither has a cheap offline query, and a caller
# that gets nothing back is expected to fall back to the answer that does not
# depend on the distribution.
#
# This exists instead of a table of which release ships which version: that
# table would be right today and wrong by the next distro release, and being
# wrong here means naming a package that installs an older version — which is
# the failure this whole module is about. Asking the machine cannot go stale.
_AVAILABLE_VERSION = {
    DEBIAN: ("apt-cache", "policy"),
    FEDORA: ("dnf", "--cacheonly", "repoquery", "--qf", "%{version}"),
    SUSE: ("zypper", "--non-interactive", "--no-refresh", "info"),
    ARCH: ("pacman", "-Si"),
    ALPINE: ("apk", "policy"),
}


def available_version_cmd(key, fam: str) -> list[str] | None:
    """argv printing what `fam` would install for `key`, or None if we can't ask.

    Built from `_NAMES` rather than taking a package name, so the thing asked
    about and the thing later named in the command cannot drift apart.
    """
    named = names([key], fam)
    if not named or fam not in _AVAILABLE_VERSION:
        return None
    return [*_AVAILABLE_VERSION[fam], named[0]]


# The README section that stays right for a distribution this table does not
# list. Named, not linked: these are terminal messages, and the one-link rule
# in .claude/rules/user-messages.md keeps URLs out of message bodies.
README_SECTION = 'the README section "Plugin dependencies and validation"'
README_INSTALL_SECTION = "the README's Install section"


def install_steps(keys, see: str = README_SECTION, indent: str = ""
                  ) -> tuple[tuple[str, str], ...]:
    """How to install `keys` here, as ``(cprint style, text)`` pairs.

    Unindented: each consumer owns its own margin — ``print_install_hint``
    adds two spaces, ``doctor.emit_check`` prints a ``CheckResult``'s steps
    under a nine-space gutter, and ``console.run_guarded`` renders a raised
    failure's ``next_step``. One builder for all three, which is what stops
    the same remedy being worded three ways.

    One command when os-release places the reader, every family's when it
    doesn't: a reader on Void or Solus gets nothing from a `sudo apt` line,
    and everyone else gets a wall of commands to find themselves in.

    ``see`` names the README section that stays right for a distribution this
    table does not list, and it is a parameter because the two kinds of
    dependency live in different sections — sending someone whose numpy is
    missing to the plugin section is the same wrong answer as naming the wrong
    package.

    ``indent`` is for the caller whose own printer already applies a margin
    and who still wants these lines to sit *under* a lead-in ("Install
    them:") rather than beside it. Left to the caller because the margin is a
    property of where the remedy is being printed, not of the remedy.
    """
    out: list[tuple[str, str]] = []
    fam = family()
    if fam:
        command = install_command(keys, fam)
        covered = _covered(keys, fam)
        for key in keys:
            if key in covered and (key, fam) in CAVEATS:
                out.append(("dim", f"({CAVEATS[(key, fam)]})"))
        if command:
            # Just the command. A reader we could place needs no note about
            # the distributions they are not on, and the README pointer earns
            # its line only where we have nothing better — here it would push
            # an actionable error screen further down for no one's benefit. A
            # wrong guess is self-announcing: `sudo apt` on a Fedora box needs
            # no caption.
            out.append(("cta", command))
        # Every key the command could not carry, so a shorter command than the
        # reader expected is explained rather than just shorter — and every
        # caveat on a key it did carry, so a command that resolves but does
        # not deliver says so.
        declarative = [k for k in keys if fam == NIXOS
                       and k not in covered and k in _NIXOS_SYSTEM]
        for key in keys:
            if key in declarative:
                continue        # folded into the one instruction below
            if key not in covered:
                out.append(("dim", f"({UNPACKAGED.get((key, fam), key)})"))
        if declarative:
            # One instruction for all of them, and the rebuild on its own line
            # so it stays pasteable.
            attrs = " and ".join(_NIXOS_SYSTEM[k] for k in declarative)
            out.append(("dim", "(NixOS installs these system-wide, since a "
                               "nix-shell doesn't reach the PipeWire daemon — "
                               f"add {attrs} to environment.systemPackages)"))
            out.append(("cta", "sudo nixos-rebuild switch"))
        if out:
            return tuple((style, f"{indent}{text}") for style, text in out)
    for label, alt in install_commands(keys):
        out.append(("cta", f"{label}: {alt}"))
    # Grouped by note rather than walked as a sorted dict: keyed on (key,
    # family) the lines interleave — openSUSE's Calf landing between two NixOS
    # ones — and the same sentence repeats per family and per package, so
    # "the pipewire package itself carries pw-cli and pw-dump" printed three
    # times over and read as three different facts.
    grouped: dict[str, list[str]] = {}

    def note(text, label):
        seen = grouped.setdefault(text, [])
        if label not in seen:
            seen.append(label)

    for gap_fam in FAMILIES:
        declarative = [k for k in keys if gap_fam == NIXOS
                       and k in _NIXOS_SYSTEM and (k, gap_fam) in UNPACKAGED]
        if declarative:
            attrs = " and ".join(_NIXOS_SYSTEM[k] for k in declarative)
            note(f"add {attrs} to environment.systemPackages and run "
                 "nixos-rebuild switch — a nix-shell doesn't reach the "
                 "PipeWire daemon", LABELS[gap_fam])
        for key in keys:
            if key in declarative:
                continue
            if (key, gap_fam) in UNPACKAGED:
                note(UNPACKAGED[(key, gap_fam)], LABELS[gap_fam])
    for text, labels in grouped.items():
        out.append(("dim", f"({', '.join(labels)}: {text})"))
    out.append(("dim", f"(on another distribution, see {see})"))
    return tuple((style, f"{indent}{text}") for style, text in out)


def print_install_hint(keys, cprint, see: str = README_SECTION) -> None:
    """Print `install_steps` at this project's two-space message margin.

    Takes the caller's `cprint` for the reason `lib.doctor`'s printers do —
    this module is stdlib-only and imported by both PipeWire scripts, and an
    `import console` here would close a cycle.
    """
    for style, text in install_steps(keys, see):
        cprint(style, f"  {text}")
