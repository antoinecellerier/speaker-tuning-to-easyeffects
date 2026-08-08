"""One console for both converters, so a run of either reads as one tool.

The generator and the converter print the same kinds of thing — headings,
warnings, a closing call to action — and are routinely run back to back, by
hand or by ``dolby_to_pipewire.py`` inside a single process. Two consoles
built side by side drift: a width off by a column re-wraps every table, a
theme colour that differs makes one severity look like two, and
``--no-color`` has to find and silence each one. Built once, here, none of
that is possible.

Unlike the rest of ``lib/``, this module may import ``rich``: it is the one
place the optional presentation dependency is allowed to land, which is why
it is deliberately absent from ``tests/test_layout.py``'s ``STDLIB_ONLY``
list. The rule it does keep is the expensive one — nothing here reaches the
DSP stack, so ``ee_to_pipewire.py`` still starts without paying for
numpy/scipy. And rich stays optional: it is absent on a plain install, so
every helper below degrades to a plain ``print`` rather than failing.

``_HelpHintParser`` is here for the same reason as the rest: an argparse
error is output too, and it is the one message two of the three entry points
print from a class rather than a function. It lived on the generator and the
wrapper imported it from there — the last import that crossed between two
root scripts. ``_make_adder`` sits beside it as the other half of that story:
not output at all, but the argparse plumbing both converters build their
shared argument groups on, kept as two identical copies because importing the
generator's was said to drag numpy/scipy into the converter's startup. Moving
the generator's DSP imports into its ``main()`` made that false, so the double
bought nothing and the copies collapse here — where neither root script has to
import the other to reach it, and where, being closures over the parser it is
handed, it imports nothing itself. ``add_color_and_version_args`` is the third
of that cluster and the only one about this module's own subject: ``--no-color``
is the switch every helper above obeys, and it was declared three times, once
per entry point, beside a ``--version`` that had drifted along with it.
"""

import argparse
import shutil
import sys
import textwrap

from lib import version


# Prose wraps to the terminal, within bounds. Below the floor, a hanging
# indent eats the line and hyphenated XML element names get unreadable; above
# the cap, a paragraph stretches into one long unscannable line — measure, not
# window width, is what makes prose readable. Everything here is hand-wrapped
# rather than reflowed by rich (see cprint), so this is the number that
# matters.
_WRAP_CAP = 120
_WRAP_FLOOR = 60


def _wrap_width() -> int:
    return max(_WRAP_FLOOR,
               min(_WRAP_CAP, shutil.get_terminal_size((80, 24)).columns))


try:
    from rich.console import Console
    from rich.theme import Theme
    _CONSOLE = Console(
        theme=Theme({
            "err":  "bold red",
            "head": "bold cyan",
            "ok":   "green",
            "warn": "yellow",
            "cta":  "bold magenta",
            "dim":  "dim",
        }),
        markup=False,
        highlight=False,
        width=_wrap_width(),
    )
except ImportError:
    _CONSOLE = None

try:
    from rich_argparse import RichHelpFormatter as _HelpFormatter
except ImportError:
    _HelpFormatter = argparse.HelpFormatter

_MISSING_COLOR_DEPS = []
if _CONSOLE is None:
    _MISSING_COLOR_DEPS.append("rich")
if _HelpFormatter is argparse.HelpFormatter:
    _MISSING_COLOR_DEPS.append("rich-argparse")


class _HelpHintParser(argparse.ArgumentParser):
    """ArgumentParser that appends a --help pointer to usage errors, so a
    bad/unknown flag gets the same 'Run with --help' nudge that runtime
    errors get from the top-level handler. Mirrors argparse's default
    error(): usage synopsis to stderr, then 'prog: error: message', exit 2.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(
            2,
            f"{self.prog}: error: {message}\n"
            "Run with --help to see usage and all options.\n",
        )


def _make_adder(container, only):
    """Shared-group plumbing: an ``add_argument`` wrapper that skips flags not
    selected by ``only`` (keyed by primary name: first option string, or the
    positional's name) and records the added actions so callers — notably
    dolby_to_pipewire.py — can rebuild a child argv from them generically."""
    added = []

    def add(*names, **kwargs):
        if only is None or names[0] in only:
            added.append(container.add_argument(*names, **kwargs))

    return add, added


def add_color_and_version_args(add) -> None:
    """Add the two flags every entry point closes its "general" group with.

    ``add`` is an ``add_argument``-shaped callable — either a ``_make_adder``
    adder or a group's own ``add_argument``. Which one the caller passes is
    the load-bearing choice: dolby_to_pipewire.py rebuilds a child argv from
    the actions an adder records, and neither of these flags may land there.
    ``--version`` is not forwardable at all, and ``--no-color`` the wrapper
    propagates itself, appending it to each step's common argv — so the
    wrapper passes ``group.add_argument`` and records nothing, while the two
    converters pass their adder and keep the ``only`` filtering that lets the
    wrapper drop these from the groups it borrows.
    """
    add(
        "--no-color",
        action="store_true",
        help="disable colored terminal output",
    )
    add(
        "--version",
        action="version",
        version=f"%(prog)s {version.get_version()}",
        help="show version and exit",
    )


def cprint(style: str, text: str = "") -> None:
    """Print `text` in the given semantic style, or plain if rich is absent.

    ``soft_wrap=True`` keeps the text exactly as written. Without it rich
    reflows at the console width and folds anything longer — which silently
    broke the report-back URL (103 chars) mid string on any 80-column terminal,
    leaving the tool's main call to action unclickable and uncopyable. It also
    made output depend on whether rich was installed at all, since the fallback
    above never wraps. Prose that needs wrapping asks for it explicitly via
    _cprint_wrapped.
    """
    if _CONSOLE is None:
        print(text)
        return
    _CONSOLE.print(text, style=style, soft_wrap=True)


def warn(msg: str) -> None:
    """Emit a contextual detail warning with the standard ``  Warning: ``
    prefix, so per-band / per-filter / per-profile warnings read uniformly.
    Section-level warnings that want their own blank-line spacing call cprint
    directly."""
    cprint("warn", f"  Warning: {msg}")


def _disable_color() -> None:
    global _CONSOLE
    _CONSOLE = None


def _cprint_wrapped(style: str, text: str, width: int | None = None,
                    indent: str = "") -> None:
    """Print prose as wrapped lines, the way --doctor renders a check detail.

    Wraps to the terminal by default (``_wrap_width``), so a wide window is
    not stuck reading 72-column text and a narrow one does not overflow.
    Pass ``width`` only where the number is part of a fixed layout.
    Lets the end-of-run warnings share their wording with the doctor's
    CheckResult details instead of keeping a hand-wrapped second copy.

    ``indent`` prefixes continuation lines, so a bulleted or ⚠-prefixed
    paragraph stays visually attached to its marker. Hyphenated words are
    never split — most of what gets wrapped here is XML element names like
    ``volume-leveler-compressor-enable``, and breaking one across lines makes
    it unsearchable."""
    for line in textwrap.wrap(text, width=width or _wrap_width(),
                              subsequent_indent=indent,
                              break_on_hyphens=False):
        cprint(style, line)
