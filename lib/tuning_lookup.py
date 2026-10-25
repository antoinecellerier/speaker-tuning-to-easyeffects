"""Finding the tuning a run builds from: the Dolby XML, a Sonitude tuning,
or a Dolby DAX2 preset in a driver `.inf`, in a path the reader passed, a
driver download, or an installed Windows.

One finder per kind looks in each place, and `FINDERS` lists them in the
order a place holding several is read: the format of the most recent devices
first. Where none binds this machine, one rule picks what to say
(`_first_bound`).

It prints as it goes: what it auto-detected or unpacked, and what it found.
Reading the tuning once found is `lib/tuning_source.py`'s.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

from lib import console, doctor, driver_package, tuning_source
from lib.apo import dax2, sonitude
from lib.apo.layer import BoundButUnreadable
from lib.dax import discover


class _Miss(Exception):
    """A finder's kind of tuning is there but binds nothing on this machine;
    *error* is what the run says when no other kind binds either."""

    def __init__(self, error: Exception):
        super().__init__(str(error))
        self.error = error


class _Finder:
    """One kind's lookups, one per place. A lookup returns the kind's source
    bound to this machine, or None when there is none of the kind there. It
    raises `_Miss` when there is, but none binds this machine, and
    `BoundButUnreadable` when one binds but can't be read."""
    # The source class a lookup returns.
    kind: type[tuning_source.TuningSource]
    # How the run names this kind's tuning when it uses it over another's.
    used: str

    def claims(self, path: Path) -> bool:
        """Whether *path*, passed in place of the XML, is this kind's."""
        return False

    def at_path(self, path: Path, args) -> tuning_source.TuningSource:
        """The source *path* holds, which `claims` said is this kind's."""
        raise NotImplementedError

    def in_download(self, tree: Path, download: Path, args
                    ) -> tuning_source.TuningSource | None:
        """The source in *tree*, the unpacked driver download *download*."""
        return None

    def in_store(self, root: Path, store: Path, args
                 ) -> tuple[tuning_source.TuningSource, str] | None:
        """The source in an installed Windows at *root*, whose DriverStore
        is *store*, and what the run says it auto-detected."""
        return None

    def present_in_store(self, store: Path) -> bool:
        """Whether DriverStore *store* holds this kind's tuning for this
        machine, so auto-detection keeps the mount for it."""
        return False

    def outranked(self, store: Path) -> tuple[str, Path] | None:
        """This kind's tuning for this machine in *store*, as the run names
        it when another kind's, read first, is used instead, and where it
        is."""
        return None


class _DolbyXml(_Finder):
    kind = tuning_source.DolbyXml
    used = "the Dolby file"

    def in_download(self, tree, download, args):
        if not discover.walk_for_dolby_xml_dirs(tree):
            return None
        try:
            xml_path = driver_package.pick_xml(tree, download,
                                               best_guess=args.best_guess)
        except driver_package.NoTuningForThisDevice as exc:
            raise _Miss(exc) from None
        return tuning_source.DolbyXml.for_run(xml_path, args)

    def in_store(self, root, store, args):
        if not discover.holds_dolby_xmls(store):
            return None
        try:
            xml_path = discover.find_tuning_xml(root,
                                                best_guess=args.best_guess)
        except discover.TuningChoice:
            raise
        except FileNotFoundError as exc:
            raise _Miss(exc) from None
        return tuning_source.DolbyXml.for_run(xml_path, args), \
            doctor.tilde(xml_path)

    def present_in_store(self, store):
        return discover.holds_dolby_xmls(store)


class _Sonitude(_Finder):
    kind = tuning_source.Sonitude
    used = "the Sonitude one"

    # A folder in the XML's place stays on the XML path: only an unpacked
    # download's tree is looked into for a Sonitude tuning.
    def claims(self, path):
        return not path.is_dir() and sonitude.is_source(path)

    def at_path(self, path, args):
        # Said as a missing XML is, rather than as an archive holding no
        # tuning for this device.
        if not path.is_file():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT),
                                    str(path))
        return tuning_source.Sonitude.for_run(path, args)

    def in_download(self, tree, download, args):
        if not sonitude.is_source(tree):
            return None
        return self._bound(tree, args)

    def in_store(self, root, store, args):
        folders = sonitude.driver_store_packages(store)
        if not folders:
            return None
        newest = (f" (the newest of the {len(folders)} Sonitude driver "
                  "versions there)" if len(folders) > 1 else "")
        return self._bound(folders[0], args), \
            doctor.tilde(folders[0]) + newest

    @staticmethod
    def _bound(origin: Path, args) -> tuning_source.Sonitude:
        try:
            return tuning_source.Sonitude.for_run(origin, args)
        except BoundButUnreadable:
            raise
        except RuntimeError as exc:
            raise _Miss(exc) from None

    def present_in_store(self, store):
        return bool(sonitude.driver_store_packages(store))

    def outranked(self, store):
        folders = sonitude.driver_store_packages(store)
        if not folders:
            return None
        return "A Sonitude tuning", folders[0]


class _Dax2(_Finder):
    kind = tuning_source.Dax2
    used = "the DAX2 one"

    def claims(self, path):
        return path.suffix.lower() == ".inf" and not path.is_dir()

    def at_path(self, path, args):
        if not path.is_file():
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT),
                                    str(path))
        if not dax2.mentions_dax2(path):
            raise dax2.not_a_dax2_inf(path)
        preset = dax2.for_this_machine(path)
        if preset is None:
            raise dax2.none_for_this_machine(path)
        return tuning_source.Dax2.for_run(preset, args)

    def in_download(self, tree, download, args):
        # A download's tree holds its .inf files without the APO beside them
        # (`dax2.wanted_members`).
        if not dax2.infs_below(tree, first=True, beside_apo=False):
            return None
        preset = dax2.for_this_machine(tree, beside_apo=False)
        if preset is None:
            raise _Miss(dax2.none_for_this_machine(download))
        return tuning_source.Dax2.for_run(preset, args)

    def in_store(self, root, store, args):
        # A DriverStore keeps the packages of drivers for other hardware
        # too, so one binding nothing here is no news.
        preset = dax2.for_this_machine(store)
        if preset is None:
            return None
        return tuning_source.Dax2.for_run(preset, args), \
            doctor.tilde(preset.inf_path)

    def present_in_store(self, store):
        return dax2.binds_this_machine(store)

    # No `outranked`: whether a DAX2 tuning binds here takes a walk of the
    # whole DriverStore, too dear for a note after every DAX3 run.


# Read in this order where a place holds several: the format of the most
# recent devices first.
FINDERS: tuple[_Finder, ...] = (_DolbyXml(), _Sonitude(), _Dax2())


def _first_bound(lookup: Callable[[_Finder], object],
                 otherwise: Callable[[], object]
                 ) -> tuple[_Finder | None, object]:
    """The first finder whose *lookup* binds this machine, and what it
    returned; no finder when *otherwise* answers.

    When none does, a tuning that binds this device but can't be read is
    said over one that binds nothing, and of two such the first finder's
    is. With nothing of any kind there, *otherwise* says so, or finds what
    the finders couldn't."""
    refused: list[Exception] = []
    missed: list[Exception] = []
    for finder in FINDERS:
        try:
            found = lookup(finder)
        except BoundButUnreadable as exc:
            refused.append(exc)
        except _Miss as miss:
            missed.append(miss.error)
        else:
            if found is not None:
                return finder, found
    if refused or missed:
        raise (refused or missed)[0]
    return None, otherwise()


def windows_source(root: Path, args) -> tuning_source.TuningSource:
    """The tuning a Windows install or extracted DriverStore at *root* holds
    for this machine (`FINDERS`)."""
    # An extracted DriverStore holding no Dolby package is one the Dolby
    # lookup doesn't resolve, and is itself the folder to look in.
    store = discover._resolve_driver_store(Path(root)) or Path(root)

    def no_tuning():
        # The Dolby lookup's own error says what is missing.
        xml_path = discover.find_tuning_xml(root, best_guess=args.best_guess)
        return tuning_source.DolbyXml.for_run(xml_path, args), \
            doctor.tilde(xml_path)
    winner, (source, detected) = _first_bound(
        lambda f: f.in_store(root, store, args), no_tuning)
    console.cprint("ok", f"Auto-detected: {detected}")
    if winner is not None:
        for finder in FINDERS[FINDERS.index(winner) + 1:]:
            other = finder.outranked(store)
            if other:
                what, where = other
                # Unwrapped: wrapping would split the path.
                console.cprint("dim", f"{what} is there too "
                               f"({doctor.tilde(where)}); using "
                               f"{winner.used}.")
    return source


def _download_source(download: Path, args) -> tuning_source.TuningSource:
    """The tuning a vendor's driver download holds for this machine. Its
    tuning files are unpacked into driver-cache/, and this machine's is
    picked there (`FINDERS`)."""
    tree = driver_package.unpack(download)

    def no_tuning():
        # Neither kind of tuning: the XML lookup's own error says so.
        return tuning_source.DolbyXml.for_run(driver_package.pick_xml(
            tree, download, best_guess=args.best_guess), args)
    _, source = _first_bound(lambda f: f.in_download(tree, download, args),
                             no_tuning)
    return source


def _path_source(path: Path, args) -> tuning_source.TuningSource:
    """The tuning at *path*, passed in place of the XML."""
    for finder in FINDERS:
        if finder.claims(path):
            # The path names this kind, so a flag it can't take is said
            # before the tuning is read, not why it doesn't bind.
            _check_flags(finder.kind, args)
            return finder.at_path(path, args)
    if driver_package.kind(path):
        return _download_source(path, args)
    return tuning_source.DolbyXml.for_run(args.xml_file, args)


class _FlagRefused(ValueError):
    """A flag the tuning the run uses can't take (`TuningSource.refusal`)."""


def _check_flags(kind: type[tuning_source.TuningSource], args) -> None:
    refusal = kind.refusal(args)
    if refusal:
        raise _FlagRefused(refusal)


def print_format_warning(text: str) -> None:
    """A source's warning (`TuningSource.warnings`): its sentence wrapped,
    and each line after it, such as a path to copy, as it is."""
    sentence, *lines = text.split("\n")
    console._cprint_wrapped("warn", sentence)
    for line in lines:
        console.cprint("warn", line)


def source_for(args, parser) -> tuning_source.TuningSource:
    """The tuning this run builds from (`lib/tuning_source.py`), bound to
    this machine, with the flags it can't take refused."""
    if args.xml_file and args.windows:
        parser.error("specify either xml_file or --windows, not both")
    try:
        if args.windows:
            source = windows_source(args.windows, args)
        elif args.xml_file:
            source = _path_source(Path(args.xml_file), args)
        else:
            # An auto-detection miss/ambiguity is an environment condition,
            # not CLI misuse. It propagates to the top-level handler, which
            # prints a clean error (no usage banner) that points at --help.
            # Routing it through parser.error() would slap the usage synopsis
            # on top and exit 2, framing it as a syntax error the user can't
            # fix by reading usage.
            windows_root = discover.autoprobe_dolby_source(
                also=lambda store: any(f.present_in_store(store)
                                       for f in FINDERS))
            source = windows_source(windows_root, args)
        # Where several kinds were looked for, against the one used.
        _check_flags(type(source), args)
    except _FlagRefused as exc:
        parser.error(str(exc))

    for warning in source.warnings:
        print_format_warning(warning)
    found = source.found()
    if found is not None:
        headline, explanation = found
        console.cprint("ok", headline)
        console._cprint_wrapped("dim", explanation)
    return source
