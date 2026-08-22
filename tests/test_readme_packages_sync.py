"""README ↔ `lib/packages.py` sync trap.

The README carries three hand-written per-distribution package lists, and the
scripts print the same facts from `lib/packages.py` at the moment a dependency
turns up missing. The README itself promises they agree — "The converter prints
whichever of these matches your `/etc/os-release`, so you shouldn't need this
table on a run that fails" — and nothing checked it.

Drift here is not a documentation nit. `.claude/rules/docs.md` records that a
*fabricated package-name expansion* is what prompted `lib/packages.py`, and the
pass that added these families found two live errors in these very lists: an
Alpine row naming `py3-rich-argparse`, which does not exist, so `apk add`
failed the whole transaction and left the reader with none of the packages it
did name. A reader who pastes a README line and a reader who pastes what the
tool printed have to end up in the same place.

Like `tests/test_readme_cli_sync.py`, this locks names, order and labels — not
the prose around them. A trailing "— …" clause on a bullet is the row's own
caveat and is deliberately not compared; what is compared is the command.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib import packages

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(
    encoding="utf-8")

# Each mirrored list, as (the <summary> or lead-in that precedes it, the keys
# the scripts would ask `install_command` for). Keyed on the text immediately
# above the bullets rather than a line number, so the check survives edits
# elsewhere in the README.
LISTS = (
    ("Install commands for your distro", tuple(packages.PYTHON_KEYS)),
    ("Install the **LV2 builds**", (packages.LSP_LV2, packages.CALF_LV2)),
    ("Add your distribution's `lv2info`", (packages.LV2INFO,)),
)

_BULLET = re.compile(r"^- \*\*(?P<label>[^*]+):\*\*\s*(?P<rest>.*)$", re.M)
_FAMILY_OF_LABEL = {label: fam for fam, label in packages.LABELS.items()}

# What makes a backticked span a *command* rather than prose with code in it.
# The whole invocation prefix, not just its first word, and derived from the
# module's own so a new family cannot be added on one side only. Both halves
# of that matter in the NixOS rows, which name `pkgs.lsp-plugins` in a
# sentence about `environment.systemPackages` and mention a bare `nix-shell`
# in the reason it won't work — neither is a command, however much each looks
# like one inside backticks.
_VERBS = tuple(f"{v} " for v in packages._INSTALL.values() if v) + (
    "nix-shell -p ",)


def _rows(lead_in: str) -> list[tuple[str, str]]:
    """The `(label, command)` of each bullet under `lead_in`.

    The first backticked span that begins with a package-manager verb, because
    a row may carry a caveat with code in it (`media-plugins/calf` needs
    `USE=lv2`) and the command is what this trap is about. An empty string
    means the row names no command at all, which is a real answer — NixOS has
    no install verb for the LV2 plugins.
    """
    start = README.index(lead_in)
    block = README[start:]
    # Stop at the blank line that ends the bullet list.
    end = block.index("\n\n", block.index("\n- "))
    rows = []
    for m in _BULLET.finditer(block[:end]):
        spans = re.findall(r"`([^`]+)`", m.group("rest"))
        command = next((c for c in spans if c.startswith(_VERBS)), "")
        rows.append((m.group("label"), command))
    return rows


@pytest.mark.parametrize("lead_in,keys", LISTS, ids=lambda v: str(v)[:24])
def test_readme_lists_every_family_in_the_module_order(lead_in, keys):
    """A family the module can place but the README omits is a reader who is
    told to consult a table that does not mention them.

    Order too, and against `FAMILIES` rather than alphabetically: the module's
    own comment says the tuple is "ordered as the README lists them", which is
    only a useful thing to say while it is true.
    """
    labels = [label for label, _ in _rows(lead_in)]
    assert labels == [packages.LABELS[f] for f in packages.FAMILIES], (
        f"the list under {lead_in!r} does not mirror packages.FAMILIES"
    )


@pytest.mark.parametrize("lead_in,keys", LISTS, ids=lambda v: str(v)[:24])
def test_readme_commands_are_the_ones_the_scripts_print(lead_in, keys):
    """The claim the README makes about itself, checked.

    A mismatch means two answers to one question, and no way for a reader to
    tell which of them their machine will act on.
    """
    for label, command in _rows(lead_in):
        fam = _FAMILY_OF_LABEL[label]
        expected = packages.install_command(keys, fam)
        assert command == expected, (
            f"{label}: README says {command!r}, the scripts print "
            f"{expected!r}"
        )


def test_a_row_without_a_command_is_one_the_module_has_none_for():
    """The other direction of the same trap.

    NixOS's LV2 row is prose because `install_command` genuinely returns "" —
    a nix-shell cannot reach the PipeWire daemon. That is the only reason a
    bullet may carry no command, and without this a row whose command was
    accidentally deleted would read as the same deliberate case.
    """
    for lead_in, keys in LISTS:
        for label, command in _rows(lead_in):
            fam = _FAMILY_OF_LABEL[label]
            if not packages.install_command(keys, fam):
                assert not command, (
                    f"{label} names a command under {lead_in!r} that the "
                    "module does not have"
                )


def test_every_label_in_the_readme_is_a_module_label():
    """Labels are the join key both traps above use, so a README that spells
    one differently ("Debian / Ubuntu / …") silently drops that row from the
    comparison rather than failing it. This is what stops that."""
    for lead_in, _keys in LISTS:
        for label, _command in _rows(lead_in):
            assert label in _FAMILY_OF_LABEL, (
                f"{label!r} matches no packages.LABELS entry"
            )
