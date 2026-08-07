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
"""

import argparse
import shutil
import textwrap


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
