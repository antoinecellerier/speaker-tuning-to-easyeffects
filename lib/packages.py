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

The PipeWire LV2-loader rows (``PW_LV2_LOADER``) were checked end to end on
2026-10-09 by ``tools/pw_distro_matrix.py``: a stock PipeWire probed in a
container, the printed command run, and the probe asked again. Fedora 44,
Ubuntu 26.04 and Alpine edge went from absent to present on the package
named; Ubuntu 24.04 and Rocky 9 came back absent with no package their
package manager has; Debian 12 and stable, openSUSE Leap 16.0 and Arch (with
``pipewire-pulse``) had it already.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from lib import host, tool_env

DEBIAN = "debian"
FEDORA = "fedora"
SUSE = "suse"
ARCH = "arch"
ALPINE = "alpine"
GENTOO = "gentoo"
NIXOS = "nixos"

# Ordered as the install docs list them, so the two read alike.
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


def install_verb(fam: str) -> str:
    """The install-command prefix for `fam`, or "" when it has none.

    "" covers NixOS (no imperative install verb — see `_nixos_command`) and an
    unrecognised family. Exposed so a caller with its own out-of-tree package
    (`tools/fetch_driver`'s `innoextract`) reuses the per-family verb instead
    of copying this table and drifting from it.
    """
    return _INSTALL.get(fam, "")


# The families whose install idiom really is "<prefix> <names>". NixOS is the
# one that isn't, and every invariant below that assumes a pasteable command
# is checked against this rather than `FAMILIES`.
COMMAND_FAMILIES = tuple(f for f in FAMILIES if _INSTALL[f])

# Keys are what a caller means, not what any one distribution calls it.
LSP_LV2 = "lsp-lv2"
CALF_LV2 = "calf-lv2"
LV2INFO = "lv2info"
PW_TOOLS = "pipewire-tools"
PW_LV2_LOADER = "pipewire-lv2-loader"
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
    # system. Named beside LSP in one `zypper install`, it would be reported
    # missing, and under `zypper -n` the whole transaction would abort and
    # leave the reader with neither.
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
    # PipeWire's LV2 loader for filter chains: the filter-graph plugin
    # `libspa-filter-graph-plugin-lv2.so` from 1.4, the module
    # `libpipewire-module-filter-chain-lv2.so` on 0.3.73-1.2 (issue #123).
    # The Debian row is Ubuntu's name, from 25.10: Debian folds the loader
    # into `libspa-0.2-modules`, a hard dependency of libpipewire, and has no
    # `-extra` package. Ubuntu 24.04 and EL build PipeWire without LV2. Both
    # rows are in `_KNOWN_GAPS`, so the name is printed only where the
    # package manager has it. Arch's `pipewire-audio` also carries the graph
    # core every filter chain needs. Alpine edge splits the graph core
    # (`pipewire-filter-graph`) from one package per plugin type; its stable
    # releases ship the loader in `pipewire-libs`, which PipeWire needs, so
    # only edge can be missing it. openSUSE and NixOS ship it in packages
    # PipeWire already depends on, so they get no name to print.
    PW_LV2_LOADER: {DEBIAN: "libspa-0.2-modules-extra",
                    FEDORA: "pipewire-module-filter-chain-lv2",
                    ARCH: "pipewire-audio", ALPINE: "pipewire-filter-graph-lv2",
                    GENTOO: "media-video/pipewire"},
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
_NIX_PLAIN = (
    "add {} to environment.systemPackages and run nixos-rebuild switch")

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

# What to say where no package name for PipeWire's LV2 loader can be printed:
# a family with no row, a row its package manager doesn't have, or a
# distribution we can't place. The file names are what a reader can search
# their distribution's package index for.
LV2_LOADER_GENERIC = (
    "install the package that provides PipeWire's LV2 filter-chain support — "
    "libspa-filter-graph-plugin-lv2.so, or libpipewire-module-filter-chain-"
    "lv2.so before PipeWire 1.4; some distributions build PipeWire without "
    "it")
UNPACKAGED.update({(PW_LV2_LOADER, fam): LV2_LOADER_GENERIC
                   for fam in (SUSE, NIXOS)})

# What NixOS installs system-wide rather than into a shell, by attribute.
# These are the things the PipeWire daemon or the desktop has to find, and a
# `nix-shell` never reaches either. Kept as its own table because the answer
# is one declarative instruction for however many packages were asked for.
# Rendered per key, three missing plugins would repeat the same long sentence
# three times. `UNPACKAGED` is filled from it so a cell is still *spoken* when
# the fallback lists every family one by one.
_NIXOS_SYSTEM = {
    LSP_LV2: "pkgs.lsp-plugins",
    CALF_LV2: "pkgs.calf",
    EASYEFFECTS: "pkgs.easyeffects",
}

# Of those, the ones the *daemon* has to find — the reason a nix-shell is no
# use for them. EasyEffects is not one: it is an application the reader
# launches, and it wants to be in their environment permanently for the
# ordinary reason, not because of PipeWire.
_NIXOS_DAEMON_KEYS = (LSP_LV2, CALF_LV2)

UNPACKAGED.update({
    (key, NIXOS): (_NIX_SYSTEM if key in _NIXOS_DAEMON_KEYS
                   else _NIX_PLAIN).format(attr)
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
    (PW_LV2_LOADER, GENTOO): "first set USE=lv2 for media-video/pipewire — "
                             "the default build has no LV2 support",
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
        text = host.path(path).read_text()
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
    # One version per line: dnf5 adds no newline after a --qf format, and
    # runs a release's and its update's versions together without one.
    FEDORA: ("dnf", "-q", "--cacheonly", "repoquery", "--qf", "%{version}\n"),
    SUSE: ("zypper", "--non-interactive", "--no-refresh", "info"),
    ARCH: ("pacman", "-Si"),
    ALPINE: ("apk", "policy"),
    # The one row where the name is not the last word — hence the `{}`. Reads
    # the channel already on disk, so it needs no network, and `-A` forces
    # only that attribute rather than the whole of nixpkgs. A NixOS built from
    # flakes may have no `<nixpkgs>` at all; that errors, which the caller
    # treats like any other way of not knowing.
    NIXOS: ("nix-instantiate", "--eval", "-A", "{}.version", "<nixpkgs>"),
}


def available_version_cmd(key, fam: str) -> list[str] | None:
    """argv printing what `fam` would install for `key`, or None if we can't ask.

    Built from `_NAMES` (or `_NIXOS_SYSTEM`, whose attribute is what nixpkgs
    answers for) rather than taking a package name, so the thing asked about
    and the thing later named in the command cannot drift apart.

    A `{}` in any argument takes the name; otherwise it goes on the end, which
    is where every package manager but Nix wants it.
    """
    if fam not in _AVAILABLE_VERSION:
        return None
    if fam == NIXOS:
        attr = _NIXOS_SYSTEM.get(key) or (names([key], NIXOS) or [""])[0]
        # `pkgs.easyeffects` is how a configuration.nix names it; the eval
        # below is already inside that scope, so the prefix has to come off.
        # Sliced rather than `removeprefix`, which is 3.9+ — this module keeps
        # to what "Python 3" in the README can be relied on to mean.
        name = attr[len("pkgs."):] if attr.startswith("pkgs.") else attr
    else:
        name = (names([key], fam) or [""])[0]
    if not name:
        return None
    argv = [a.format(name) if "{}" in a else a
            for a in _AVAILABLE_VERSION[fam]]
    return argv if any("{}" in a for a in _AVAILABLE_VERSION[fam]) else [
        *argv, name]


# Per family, the keys `_NAMES` names right for its current release and
# wrong for a release it still supports. rich-argparse reached Debian in 13
# and Ubuntu in 25.04: Debian 12 and Ubuntu 22.04 and 24.04, and so Mint
# 21/22 and Pop!_OS built on them, have no python3-rich-argparse. RHEL, Rocky
# and Alma get python3-rich and python3-rich-argparse only from EPEL, and
# EPEL 8 has no rich-argparse. openSUSE Leap 16.0 has rich but no
# rich-argparse, and no scipy at all. apt and dnf abort the whole install
# over one name they cannot find. So the machine is asked before one of these
# names is printed, rather than given a per-release table that goes stale
# with the next release. Only these: an answer can be wrong in ways that cost
# nothing for an optional package and mislead for a required one, as an apt
# that never ran `apt update` saying it has no numpy.
_KNOWN_GAPS = {
    DEBIAN: (RICH_ARGPARSE, PW_LV2_LOADER),
    FEDORA: (RICH, RICH_ARGPARSE, PW_LV2_LOADER),
    SUSE: (RICH_ARGPARSE, SCIPY),
}

# How to ask, offline, whether a package manager would install anything for a
# name, keyed by family: the manager's own name, for a sentence about what it
# has, and the query, with the name going on the end. Each reads the
# provides, as `install` resolves them: openSUSE's python3-rich is a provide
# of python313-rich, which `zypper info` would miss. dnf's --whatprovides
# takes the name as its value, so it comes last: placed earlier, it swallows
# the next option and dnf5 prints nothing. dnf5 adds no newline after a --qf
# format, so the format carries its own.
_PRESENCE_QUERY = {
    DEBIAN: ("apt", ("apt-cache", "policy")),
    FEDORA: ("dnf", ("dnf", "-q", "--cacheonly", "repoquery",
                     "--qf", "%{version}\\n", "--whatprovides")),
    SUSE: ("zypper", ("zypper", "--non-interactive", "--no-refresh", "search",
                      "--match-exact", "--provides")),
}


def package_manager(fam: str) -> str:
    """The name of the package manager `unavailable` asks on `fam`, or ""."""
    return _PRESENCE_QUERY[fam][0] if fam in _PRESENCE_QUERY else ""


# zypper's exit code for "matched no package name or capability".
_ZYPPER_NOT_FOUND = 104


def _query(argv) -> subprocess.CompletedProcess | None:
    """`argv` run through `tool_env`, or None when it could not run."""
    try:
        return tool_env.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None


def available_version_output(key, fam: str) -> str | None:
    """What `available_version_cmd` prints for `key` on `fam`, or None.

    None for every way of not getting an answer: no query for this family,
    the tool absent, a timeout, a non-zero exit. Reading the answer is the
    caller's, since each asks a different question of it.
    """
    argv = available_version_cmd(key, fam)
    proc = _query(argv) if argv else None
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout or ""


def unavailable(key, fam: str) -> bool:
    """True when `fam`'s package manager says it has nothing to install for
    `key`, so a command naming it would fail.

    False when it can't be asked or its answer is unclear, because naming
    the package is right on the family's current release. What counts as
    "nothing", per family:

    - apt prints nothing for a name it has never heard of, and
      ``Candidate: (none)`` for one it knows but cannot install. One that
      has never run ``apt update`` also prints nothing; the two can't be
      told apart, and the miss costs nothing, since without package lists
      apt installs nothing at all.
    - dnf repoquery prints nothing and exits 0. A cacheless repository
      under ``skip_if_unavailable=True`` is skipped the same way and reads
      as absent too.
    - zypper search exits 104. 106, a repository it skipped, is unclear.

    Each answer was run as a non-root user in a container on 2026-10-04:
    Ubuntu 24.04, Debian 12 and Ubuntu 26.04; Fedora 44 (dnf5), with and
    without a metadata cache; Rocky 9 (dnf4), with and without EPEL; and
    openSUSE Leap 16.0. The ``skip_if_unavailable=True`` case is from dnf's
    source: Rocky 9 ships it False.
    """
    if key not in _KNOWN_GAPS.get(fam, ()):
        return False
    name = (names([key], fam) or [""])[0]
    proc = _query([*_PRESENCE_QUERY[fam][1], name]) if name else None
    if proc is None:
        return False
    if fam == SUSE:
        return proc.returncode == _ZYPPER_NOT_FOUND
    if proc.returncode != 0:
        return False
    out = proc.stdout or ""
    return not out.strip() or (fam == DEBIAN and any(
        line.strip() == "Candidate: (none)" for line in out.splitlines()))


def installable(key, fam: str) -> bool:
    """Whether `fam` names a package for `key` that this machine can install.

    For a nudge that is optional, which is better left unsaid than printed
    with nothing to run. A hard requirement goes through `install_steps`
    instead, which says why a package is missing from the command.
    """
    return bool(names([key], fam)) and not unavailable(key, fam)


# The doc section that stays right for a distribution this table does not
# list. Named, not linked: these are terminal messages, and the one-link rule
# in .claude/rules/user-messages.md keeps URLs out of message bodies.
PLUGINS_SECTION = ('the "Plugin dependencies and validation" section of '
                   "docs/dolby-to-pipewire.md")
README_INSTALL_SECTION = "the README's Install section"


def install_steps(keys, see: str = PLUGINS_SECTION, indent: str = ""
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

    ``see`` names the doc section that stays right for a distribution this
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
            # the distributions they are not on, and the docs pointer earns
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
            # The reason only where it holds: an LV2 plugin has to be visible
            # to the daemon, which is why a nix-shell is no use for it. Saying
            # that about EasyEffects, which the reader launches themselves,
            # would be a reason that isn't one.
            why = (", since a nix-shell doesn't reach the PipeWire daemon"
                   if any(k in _NIXOS_DAEMON_KEYS for k in declarative) else "")
            # Not "NixOS installs these": the reader is on NixOS, since this
            # line only prints once os-release has placed them, and "these" is
            # wrong whenever the list holds one package.
            out.append(("dim", f"(a configuration change, not an install{why}"
                               f" — add {attrs} to "
                               "environment.systemPackages)"))
            out.append(("cta", "sudo nixos-rebuild switch"))
        if out:
            return tuple((style, f"{indent}{text}") for style, text in out)
    for label, alt in install_commands(keys):
        out.append(("cta", f"{label}: {alt}"))
    # Grouped by note rather than walked as a sorted dict: keyed on (key,
    # family) the lines interleave, openSUSE's Calf landing between two NixOS
    # ones. The same sentence also repeats per family and per package, so
    # "the pipewire package itself carries pw-cli and pw-dump" would print
    # three times over and read as three different facts.
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
            # Gated like the placed path above, and for the same reason:
            # the daemon is why a nix-shell is no use for an LV2 plugin, and
            # is not why EasyEffects wants to be in the environment.
            note((_NIX_SYSTEM if any(k in _NIXOS_DAEMON_KEYS
                                     for k in declarative)
                  else _NIX_PLAIN).format(attrs), LABELS[gap_fam])
        for key in keys:
            if key in declarative:
                continue
            if (key, gap_fam) in UNPACKAGED:
                note(UNPACKAGED[(key, gap_fam)], LABELS[gap_fam])
    for text, labels in grouped.items():
        out.append(("dim", f"({', '.join(labels)}: {text})"))
    out.append(("dim", f"(on another distribution, see {see})"))
    return tuple((style, f"{indent}{text}") for style, text in out)


# Where every family that names a system package installs it for. NixOS has
# no such path, and the probe below then finds nothing to ask.
SYSTEM_PYTHON = "/usr/bin/python3"

# The oldest Python these scripts run on: RHEL, Rocky and Alma 9's.
MIN_PYTHON = (3, 9)


def system_python_has(modules) -> bool:
    """True when the distribution's own Python imports every one of
    `modules` and is new enough to run these scripts.

    Asked only after this process failed to import one of them, so a True
    means this run is on another interpreter, such as Homebrew's, pyenv's or
    a virtualenv's, which doesn't see the distribution's packages. Naming
    the install command to that reader is a loop, because the packages are
    already installed and the next run fails the same way (issue #111).

    This process's isolation flags go to the probe too. Without them,
    ``/usr/bin/python3 -s`` with numpy only in the user site would be told
    that /usr/bin/python3 has numpy.
    """
    flags = [flag for flag, on in (("-I", sys.flags.isolated),
                                   ("-E", sys.flags.ignore_environment),
                                   ("-s", sys.flags.no_user_site),
                                   ("-S", sys.flags.no_site)) if on]
    # Old enough a Python can have both and still not run these scripts:
    # RHEL 8's is 3.6. The check is written so 3.6 parses it.
    probe = (f"import sys, {', '.join(modules)}; "
             f"sys.exit(sys.version_info < {MIN_PYTHON!r})")
    try:
        proc = tool_env.run([SYSTEM_PYTHON, *flags, "-c", probe],
                            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def lv2_loader_steps(built_without: bool = False, indent: str = "",
                     line: str = "", then=()
                     ) -> tuple[tuple[str, str], ...]:
    """How to get PipeWire's LV2 loader here, as ``(cprint style, text)``.

    ``line`` is PipeWire's own error, printed first and verbatim: it is the
    journal line a reader searches for, and wrapped prose would split it.
    ``then`` is the caller's follow-up (restart, or re-run), dropped when no
    package can help, since there is then nothing to follow up.

    Unlike `install_steps`, a reader we can't place gets no list of every
    family: here the families' rows are mostly "already installed", and the
    one sentence that holds everywhere says more. A row is printed only where
    `installable` says the package manager has it, so Debian, Ubuntu 24.04
    and EL readers get that sentence too, not a name apt or dnf would reject.
    ``built_without`` is a PipeWire whose filter-chain module says it was
    compiled without LV2, which no package changes. A restart belongs in
    ``then``: a conf PipeWire already skipped loads only on the next one.
    """
    said = (("dim", f"{indent}PipeWire said: {line}"),) if line else ()
    if built_without:
        return said + (("dim", f"{indent}(this PipeWire was built without LV2 "
                               "support, and no package adds it)"),)
    fam = family()
    if fam and installable(PW_LV2_LOADER, fam):
        fix = install_steps([PW_LV2_LOADER], indent=indent)
    else:
        # The action itself here, not a note beside a command.
        fix = (("cta", f"{indent}{LV2_LOADER_GENERIC[0].upper()}"
                       f"{LV2_LOADER_GENERIC[1:]}."),)
    return said + fix + tuple((style, f"{indent}{text}")
                              for style, text in then)


def print_install_hint(keys, cprint, see: str = PLUGINS_SECTION) -> None:
    """Print `install_steps` at this project's two-space message margin.

    Takes the caller's `cprint` for the reason `lib.doctor`'s printers do —
    this module is stdlib-only and imported by both PipeWire scripts, and an
    `import console` here would close a cycle.
    """
    for style, text in install_steps(keys, see):
        cprint(style, f"  {text}")
