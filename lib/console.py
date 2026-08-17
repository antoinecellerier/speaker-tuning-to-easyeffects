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
shared argument groups on, kept as two copies because importing the
generator's was said to drag numpy/scipy into the converter's startup. Moving
the generator's DSP imports into its ``main()`` made that false, so the double
bought nothing and the copies collapse here — where neither root script has to
import the other to reach it, and where, being closures over the parser it is
handed, it imports nothing itself. The two bodies were identical; the two
docstrings were not, and the surviving one is the generator's. The converter's
called the filter key the "primary option string", which was wrong before the
copies met: ``names[0]`` is the key whether or not it starts with a dash, and
the generator registers its ``xml_file`` positional through this very adder.
``add_color_and_version_args`` is the third of that cluster and the only one
about this module's own subject: ``--no-color``
is the switch every helper above obeys, and it was declared three times, once
per entry point, beside a ``--version`` copied the same three times. Nothing
had diverged — all three declarations of each flag were character-identical —
so this collapses a count, not a repair. The one real difference between the
sites is structural, and the helper's own docstring below explains it.
``help_style`` is its other end — the flag has to be honoured a second time,
before argparse renders ``--help``, and answering that means reading both
private names above, which is a poor thing to make three callers do.

``run_guarded`` is the last of the cluster and the reason ``doctor`` is
imported below. It is how a *failed* run looks, and it was the generator's
alone: the two PipeWire entry points ended in a bare ``sys.exit(main())`` and
let a converter failure traceback at the user. It sits beside
``_HelpHintParser`` because the two are one message seen from either side of
argparse — the argv was wrong (stderr, exit 2), or something the run reached
raised (stdout, exit 1) — and they share ``_HELP_HINT``, the sentence both end
on when nothing more specific is known.
"""

import argparse
import os
import shutil
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path

from lib import doctor, version


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


# The sentence both failure paths end on: argparse's, when the argv itself is
# wrong, and run_guarded's, for any raiser that names no next step of its own.
# One spelling, not two that read alike. The paths genuinely differ — argv
# parsing writes to stderr and exits 2, a raised failure prints to stdout and
# exits 1 — but the claim is the same claim, and the parser below has always
# promised in its docstring that the two nudges match. A promise like that
# kept as a second copy of the string is what goes stale first.
_HELP_HINT = "Run with --help to see usage and all options."


class _HelpHintParser(argparse.ArgumentParser):
    """ArgumentParser that appends a --help pointer to usage errors, so a
    bad/unknown flag gets the same 'Run with --help' nudge that runtime
    errors get from the top-level handler. Mirrors argparse's default
    error(): usage synopsis to stderr, then 'prog: error: message', exit 2.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{_HELP_HINT}\n")


# What a user can cause, as against what only we can. Everything listed is an
# environment condition or an input the run was handed — a missing file, an
# unreadable directory, an XML or preset whose shape we reject — and for those
# a traceback tells the reader nothing they can act on.
#
# OSError rather than FileNotFoundError, which is the only one of its family
# the generator used to name: every one of these scripts writes files into
# directories it does not own (~/.local/share/easyeffects, ~/.config/pipewire),
# so a read-only mount, a root-owned leftover or a full disk is as ordinary a
# failure as a missing XML, and Python never uses OSError for a logic bug.
#
# TypeError is deliberately absent, though the converter has two raise sites
# for it (lib/pipewire/conf.py's _fmt_num / _fmt_value). Those cannot fire on
# any preset a user could write: _fmt_value already handles every JSON type,
# so reaching its raise means a non-JSON Python object came from our own code,
# and the traceback is the only thing that says so. Same argument, and the
# same line, as ModuleNotFoundError-not-ImportError in the generator's DSP
# import. A bare `Exception` fails it harder still: it would turn every bug
# in either converter into a friendly sentence with nowhere to report from.
_HANDLED = (OSError, RuntimeError, ValueError, ET.ParseError)


def no_next_step(exc):
    """Mark ``exc`` as already ending on its own instruction, and return it.

    For the failures whose message closes on the thing to do — install this
    package, pick another profile. There the generic --help pointer is a
    second and weaker call to action, and for a missing dependency it is a
    wrong one: no flag installs numpy, so it sends a reader who has just been
    told exactly what to type off to read a flag list instead. The
    no-"nothing to do" rule in .claude/rules/user-messages.md is the same
    argument one level up — an action-shaped line with no action in it
    teaches people to stop reading the ones that have.

    ``raise console.no_next_step(ValueError(msg))`` at the site, rather than a
    flag threaded through the raise, because it is the message's own wording
    that decides this and the wording is right there.
    """
    exc.next_step = ""
    return exc


# An environment variable rather than a flag, for the case it exists for: the
# desktop session is itself root's, so what we write does reach it. That is
# rare enough not to earn a line in three --help listings and in the README's
# mirror of them, and this way it reaches dolby_to_pipewire.py's in-process
# child runs without being rebuilt into their argv. The cost is that it fails
# open: an ALLOW_ROOT inherited from something else skips the guard silently,
# and the run it exists to stop goes ahead.

_ALLOW_ROOT_ENV = "ALLOW_ROOT"


def refuse_root() -> None:
    """Stop a run whose output nothing in the user's session would read.

    Everything all three entry points write lands in a home directory
    (~/.local/share/easyeffects, ~/.config/pipewire), and everything that then
    consumes it — EasyEffects, WirePlumber, ``systemctl --user`` — is per-user.
    So root is not a more capable version of this run, it is a different one.
    Which way it fails depends on sudoers: with $HOME reset (the ``env_reset``
    default) what we write lands under /root, where the user's session never
    looks; with $HOME kept it lands in the right place owned by root, and the
    next ordinary run cannot replace it.

    Refuses every mode, including the ones that write nothing, and the message
    says so ("nothing was written") — which is a claim about *where callers
    put this*, not about anything it can check. It holds only while every
    entry point calls it directly after parse_args, ahead of its first mkdir.

    --doctor is in fact the worst of them, because it answers: it probes root's
    EasyEffects
    directories and root's XDG_RUNTIME_DIR and reports what it finds as facts
    about a user who is not the one running it — in a block written to be
    pasted into an issue.

    The message names no path on purpose. run_guarded renders it through
    doctor.tilde(), and under sudo $HOME *is* /root, so an interpolated
    /root/.local/share/easyeffects would print as ``~`` — reading as the
    user's own home, the opposite of the point. It names no user either:
    SUDO_USER would make the next step more concrete, at the cost of putting a
    login into output people paste, which tests/test_cli.py's
    test_a_run_never_prints_the_users_name holds the line on everywhere else.
    """
    if os.geteuid() != 0 or os.environ.get(_ALLOW_ROOT_ENV):
        return
    exc = RuntimeError(
        "you ran this as root, so nothing was written. As root the files land "
        "under root's home, where your session never looks — or, if sudo kept "
        "your HOME, in yours but owned by root, where your next run could not "
        "replace them.")
    # Two lines, and in this order, because both halves were misread when they
    # were one. "run as root" opened the sentence as an instruction rather than
    # a report of what just happened; the override, sharing the body with the
    # explanation, read as the equal of the real fix; and its condition — first
    # written "if root is the only user on this machine" — was taken to mean a
    # single-user laptop, which is every reader who reaches this and precisely
    # the one it must not license. The condition is now what actually makes the
    # override safe: the session consuming these files is root's own.
    exc.next_step = (
        "Re-run the same command as your normal user, without the sudo.\n"
        "  Only if you log into the desktop as root: prefix it with "
        f"{_ALLOW_ROOT_ENV}=1 instead.")
    raise exc


def _owner_uid(path: Path) -> int:
    """``path``'s owning uid. A function of its own so a test can answer for
    it: the only way to make a genuinely root-owned file is to be root."""
    return path.stat().st_uid


def _leftover_next_step(exc):
    """What to do about a permission failure an earlier root run caused, or
    None when this failure is not that.

    The other half of refuse_root, for whoever already got a root run in — it
    predates the guard, or the override let it through: a run that kept $HOME
    leaves root-owned files in the user's own tree, and every ordinary run
    after it dies on ``[Errno 13] Permission denied`` with the generic --help
    pointer under it — a flag list, for a problem no flag fixes.

    Says only what the errno and one stat support — this path is owned by
    root — and offers the sudo run as the mechanism that produces that, not as
    a history it cannot see. Asserted the other way round ("a root-owned file
    from an earlier sudo run"), a reader who did not remember running it with
    sudo read it as the tool claiming they had, in the one line they were
    about to run with sudo themselves. The same reader asked what a recursive
    chown was about to touch, so the -R is both scoped to the directory case
    and named: it appears only where the thing owned by root is a directory,
    and the sentence says it takes the contents too.

    Scoped to exactly what the errno named: the file when it exists, the
    directory we failed to create in when it doesn't. A sudo run usually left
    a whole subtree, so a second failure after the first chown is possible;
    naming a wider tree to pre-empt it would put a recursive chown on a path
    the user was never told about. The command goes on its own line for the
    reason doctor.emit_check splits detail from steps — one folded across two
    is not runnable.
    """
    if not isinstance(exc, PermissionError) or os.geteuid() == 0:
        return None
    named = getattr(exc, "filename", None)
    if not named:
        return None
    try:
        target = Path(named)
        if not target.exists():
            target = target.parent
        if _owner_uid(target) != 0:
            return None
        is_dir = target.is_dir()
    except OSError:
        # Raised from inside the handler below, this would replace a clean
        # error line with a traceback — the failure run_guarded exists to stop.
        return None
    what, take, flag = (
        ("The directory it goes in is", "Take it and everything in it", "-R ")
        if is_dir else ("That file is", "Take it", ""))
    return (f"{what} owned by root — a run made with sudo leaves them that "
            f"way. {take} back with:\n"
            f"  sudo chown {flag}$USER: {doctor.tilde(target)}")


def run_guarded(run) -> int:
    """Run ``run()``, rendering a failure the user can act on as one line.

    The one place all three entry points render a failure, so a run of any of
    them fails the same way. ``run`` is a no-argument callable — each script's
    ``main()``, with its own arguments already bound — and its return value is
    the process exit code, ``None`` counting as success for the generator's
    ``main()``, which returns nothing.

    Returns rather than re-raises, which is what keeps a failure from being
    rendered twice: dolby_to_pipewire.py calls the generator's ``run_cli`` in
    process, so an XML that cannot be read is printed by the guard inside that
    call and reaches the wrapper's own as a return code, not an exception.

    What follows the error line is the raiser's to choose, via a ``next_step``
    attribute on the exception (``no_next_step`` above sets the empty one).
    A raise that chose nothing falls to ``_leftover_next_step`` and then to
    ``_HELP_HINT``: the OSErrors reach here from a dozen write sites that
    cannot tell an ordinary permission failure from one a past sudo run left,
    and this is the one place that can look.
    Exception *class* cannot make that choice for it — FileNotFoundError is
    raised for three unrelated kinds of failure here and ValueError for two —
    so the site that wrote the message is the only place that knows whether
    --help is the right thing to say after it.
    """
    try:
        rc = run()
    except _HANDLED as e:
        # tilde over the whole message, not over a path we interpolated: the
        # discovery errors name several paths mid-sentence, and an OSError
        # names one inside repr quotes. This is where all of them print.
        cprint("err", f"Error: {doctor.tilde(e)}")
        # None, not _HELP_HINT, as the "nothing was chosen" value: the raiser
        # gets first say — including no_next_step's empty string, which has to
        # stay silent — and only an unclaimed failure is offered to the
        # leftover check, which is the one thing here that can recognise a
        # failure the raise site had no way to name.
        next_step = getattr(e, "next_step", None)
        if next_step is None:
            next_step = _leftover_next_step(e) or _HELP_HINT
        if next_step:
            cprint("cta", next_step)
        return 1
    return 0 if rc is None else rc


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


def help_style(argv: list[str] | None = None):
    """The two argparse knobs colour costs: ``(formatter_class, epilog)``.

    ``--no-color`` has to be honoured before argparse renders ``--help``,
    which is before there is a parsed namespace to read it from — hence the
    argv pre-scan. The epilog is the other half: it is the one thing that
    renders differently when a colour dependency is missing, naming the ones
    that are (``tests/test_optional_deps.py`` asserts the rich-absent help is
    the rich-present help plus exactly this line).

    Returns the two values rather than a built parser. All three entry points
    want the same formatter and the same epilog, but not the same parser class:
    ``ee_to_pipewire.py`` builds a bare ``argparse.ArgumentParser`` where the
    other two build ``_HelpHintParser``. Taking that class as a parameter would
    hide the one place they differ behind the thing they share.
    """
    _argv = sys.argv[1:] if argv is None else argv
    formatter_class = (argparse.HelpFormatter
                       if "--no-color" in _argv else _HelpFormatter)
    epilog = None
    if _MISSING_COLOR_DEPS:
        epilog = ("Tip: install " + " and ".join(_MISSING_COLOR_DEPS)
                  + " for colored output (see README for distro packages).")
    return formatter_class, epilog


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
