"""Which tuning a run builds from: a Dolby DAX3 XML, or a standalone vendor
format that stands in for one.

Most devices ship a DAX3 XML, and a vendor APO layer may stack on it
(`lib/apo/discover.py` `find_for_xml`). A device that ships no Dolby file has
its whole tuning in a standalone vendor format instead (`STANDALONE` there).
main() resolves which once, from the parsed arguments, and asks the
`TuningSource` what the run can do rather than which kind it is: whether its
presets come in voicings, which profiles it carries itself, which layer is a
profile's whole tuning, the flags that can't apply, and the file the closing
asks for.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), and it prints nothing:
the run reports what it returns. Finding the XML itself, by auto-detection
or in a driver download, prints as it goes, so that stays in main().
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from lib.apo import dax2 as dax2_format
from lib.apo import layer as apo_layer
from lib.apo.dax2 import Dax2Preset
from lib.apo.layer import ApoLayer, StandaloneFormat

# Each builds a Dolby stage, and a standalone source has none:
# refused rather than run and reported as having had no effect.
_DOLBY_STAGE_FLAGS = ("autogain", "dialog", "virtual-bass")


def _ends_on_its_reason(exc: ValueError) -> ValueError:
    """*exc*, marked as `lib/console.py` `no_next_step` marks one, which this
    stdlib-only module can't import: its message says what is left to do,
    and no flag in --help makes a tuning change anything."""
    exc.next_step = ""
    return exc


@dataclass(frozen=True)
class TuningSource:
    """The tuning a run builds from, as main() reads it."""
    # The Dolby file the run reads, which the closing also asks for. None
    # for a standalone source, whose closing asks for its archive instead,
    # and for a DAX2 one, whose closing asks for its .inf (`closing_file`).
    xml_path: Path | None
    # The preset-name stem: --prefix when given, "Dolby" or the standalone
    # format's own otherwise.
    prefix: str
    # Standalone only: its format, and the archive, unpacked download or
    # DriverStore folder it was found in.
    format: StandaloneFormat | None = None
    origin: Path | None = None
    # Standalone only, once `resolved`: profile name → the layer that is that
    # profile's whole tuning. A run without --profile builds the first that
    # changes the sound (`buildable`).
    profiles: dict[str, ApoLayer] = field(default_factory=dict)
    # DAX2 only: the preset a driver's .inf binds to this device, read as a
    # Dolby tuning without an XML: one set of speaker settings and one
    # voicing curve, built through the Dolby stages it carries
    # (`lib/apo/dax2.py`).
    dax2: Dax2Preset | None = None

    @property
    def layer(self) -> ApoLayer | None:
        """Standalone only: the first-listed profile's layer, which names
        the tuning for the device as a whole: every profile's is read from
        the same preset."""
        return next(iter(self.profiles.values()), None)

    def experimental(self, profile: str | None) -> bool:
        """Whether no listener has heard *profile*'s tuning yet."""
        if self.dax2 is not None:
            return True
        return (self.format is not None
                and profile in self.format.experimental)

    @property
    def has_voicings(self) -> bool:
        """Whether each profile's presets come in Dolby's voicings
        (Balanced, Detailed, Warm). A standalone format has one stand-in
        curve, so its presets are told apart by profile alone. A DAX2 preset
        carries one IEQ curve, built as the Balanced voicing alone."""
        return self.format is None

    @property
    def voicings_carried(self) -> tuple[str, ...] | None:
        """The voicings the source carries when it has only some of Dolby's
        three, so a wrapper can refuse the others; None for all three."""
        return (dax2_format.VOICING,) if self.dax2 is not None else None

    @property
    def mode_count(self) -> int | None:
        """How many sound modes the source carries itself; None when the
        Dolby XML holds them."""
        if self.profiles:
            return len(self.buildable)
        return 1 if self.dax2 is not None else None

    @property
    def level_steps(self) -> bool:
        """Whether the source has presets whose level differs from one
        another's: several voicings or modes, built now or by a later run."""
        return self.dax2 is None

    def layer_for(self, profile: str | None) -> ApoLayer | None:
        """The layer that is *profile*'s whole tuning, or None when the
        Dolby file is."""
        return self.profiles.get(profile)

    @property
    def buildable(self) -> list[str]:
        """The profiles the source carries whose layer changes the sound,
        in order; none when the Dolby file holds them. A preset of one that
        changes nothing would be of no use."""
        return [p for p, layer in self.profiles.items()
                if not layer.changes_nothing]

    @property
    def unbuildable(self) -> list[str]:
        """The profiles the source carries whose layer changes nothing."""
        return [p for p in self.profiles if p not in self.buildable]

    def profiles_to_build(self, profile: str | None,
                          all_profiles: bool) -> list[str]:
        """Standalone only: the profiles this run builds, as --profile and
        --all-profiles pick them; the first buildable one when neither is
        given.

        Raises ValueError, as the Dolby file's own lookup does, for a
        profile the tuning doesn't hold or one that changes nothing, and
        when no profile changes anything."""
        names, buildable = list(self.profiles), self.buildable
        if profile is not None and profile.lower() not in self.profiles:
            raise ValueError(f"Profile '{profile}' not found. Available: "
                             f"{', '.join(names)}")
        if not buildable:
            raise _ends_on_its_reason(ValueError(
                "This tuning changes nothing in any of its sound modes "
                f"({', '.join(names)}), so there is no preset to build."))
        if all_profiles:
            return buildable
        if profile is None:
            return buildable[:1]
        if profile.lower() not in buildable:
            raise _ends_on_its_reason(ValueError(
                f"Profile '{profile}' changes nothing in this tuning, so "
                "there is no preset to build for it. Profiles that change "
                f"the sound: {', '.join(buildable)}"))
        return [profile.lower()]

    def listing(self) -> list[str]:
        """Standalone or DAX2: the --list lines below its header, in the
        Dolby listing's shape. A standalone tuning's profiles are its content
        modes, all for the one output it is read for; a DAX2 preset has
        none."""
        lines = ["  endpoint: internal_speaker (operating_mode=normal)"]
        if self.dax2 is not None:
            return lines + [f"    preset: {self.dax2.name} (the only one; "
                            "experimental)"]
        for name in self.profiles:
            mark = (" (changes nothing in this tuning)"
                    if name in self.unbuildable
                    else " (experimental)" if self.experimental(name)
                    else "")
            lines.append(f"    profile: {name}{mark}")
        return lines

    def profile_banner(self, profile: str, picked: bool) -> str:
        """Standalone only: what the run's "Profile: " line says of
        *profile*, in the Dolby banner's wording. *picked* is whether
        --profile or --all-profiles chose it, which needs no reason given.

        A mode that changes nothing is never built, so when the first is
        such, the pick is the first that changes the sound."""
        n_profiles, n_built = len(self.profiles), len(self.buildable)
        if picked:
            return profile
        if n_profiles == 1:
            return f"{profile} (this speaker's only sound mode)"
        if n_built == 1:
            return (f"{profile} (the only one of this speaker's "
                    f"{n_profiles} sound modes that changes the sound)")
        if profile != next(iter(self.profiles)):
            return (f"{profile} (the first-listed of the {n_built} sound "
                    "modes that change this speaker's sound — --list names "
                    "them; --profile picks)")
        return (f"{profile} (the first-listed of this speaker's "
                f"{n_profiles} sound modes — --list names them; --profile "
                "picks)")

    @property
    def closing_file(self) -> Path | None:
        """The tuning file the closing asks a report to attach: the XML, or
        on a standalone source once `resolved`, the archive its default
        profile was read from. In a driver download that is the archive as
        unpacked, which holds this device's tuning without the rest of the
        download."""
        if self.dax2 is not None:
            return self.dax2.inf_path
        if self.format is None:
            return self.xml_path
        return self.layer.inf_path if self.layer is not None else None

    @property
    def unoffered(self) -> tuple[str, ...]:
        """Filter markers the menus and the closing must not offer.

        A standalone preset is the vendor layer, so the menus' --disable row
        and the closing's "on by default" note would offer a run `refusal`
        refuses. level-restore's --enable row goes too: the curve only cuts,
        so the peak it would hand back is a rounding error, and the flag is
        refused.
        """
        if self.format is None:
            return ()
        return (f"{apo_layer.FLAG}-active", "level-restore")

    def refusal(self, args) -> str | None:
        """The error for the first flag in *args* this source can't take,
        or None."""
        if self.dax2 is not None:
            return self._dax2_refusal(args)
        if self.format is None:
            return None
        # No Dolby file: the tuning's profiles are its content modes, all for
        # the internal speakers, so there is no other endpoint or operating
        # mode to pick, and no Dolby file to fall back to. The vendor layer
        # is the whole tuning, so nothing is left to build without it.
        # Given means "not the default", as argparse sets them in
        # dolby_to_easyeffects.py.
        for flag, given in (("--endpoint",
                             args.endpoint != "internal_speaker"),
                            ("--mode", args.mode != "normal"),
                            ("--best-guess", args.best_guess)):
            if given:
                return (f"{flag} reads a Dolby file, and this tuning has "
                        "none — drop it")
        if args.volmax_slot != "input-gain":
            return ("--volmax-slot acts on a Dolby stage, and this tuning "
                    "has none — drop it")
        for name in _DOLBY_STAGE_FLAGS:
            if name in args.enable:
                return (f"--enable {name} acts on a Dolby stage, and this "
                        "tuning has none — drop it")
        # Not a Dolby stage: it hands back the FIR's peak normalisation, and
        # a cuts-only curve's peak sits at about 0 dB (`unoffered`).
        if "level-restore" in args.enable:
            return ("--enable level-restore gives back level the filter's "
                    "peak scaling removed, and this tuning's filter loses "
                    "next to none — drop it")
        # Every --disable name but the vendor layer's own drops a Dolby stage.
        for name in args.disable:
            if name != apo_layer.FLAG:
                return (f"--disable {name} acts on a Dolby stage, and this "
                        "tuning has none — drop it")
        if apo_layer.FLAG in args.disable:
            return (f"--disable {apo_layer.FLAG} would leave nothing to "
                    f"build: the {self.format.owner} tuning is this run's "
                    "only one — drop it")
        return None

    @staticmethod
    def _dax2_refusal(args) -> str | None:
        """A DAX2 preset is one set of settings for the internal speakers,
        the only one the run reads: there is no other endpoint, operating
        mode or sound mode to pick, and no Dolby file to guess among."""
        for flag, given, why in (
                ("--profile", args.profile is not None,
                 "picks a sound mode, and this tuning holds one set of "
                 "settings"),
                ("--all-profiles", args.all_profiles,
                 "builds every sound mode, and this tuning holds one set "
                 "of settings"),
                ("--endpoint", args.endpoint != "internal_speaker",
                 "picks an output, and this tuning is for the speakers only"),
                ("--mode", args.mode != "normal",
                 "picks an operating mode, and this tuning holds one set "
                 "of settings"),
                ("--best-guess", args.best_guess,
                 "guesses among Dolby XMLs, and this run reads none")):
            if given:
                return f"{flag} {why} — drop {flag}"
        return None

    def resolved(self, warn: Callable[[str], None]) -> TuningSource:
        """This source with its profiles' layers read; a Dolby source as it
        is.

        Raises RuntimeError, from the format, when no layer binds this
        machine."""
        if self.format is None:
            return self
        return replace(self, profiles=self.format.resolve(self.origin, warn))


def dolby(xml_path: Path, prefix: str | None) -> TuningSource:
    """A run from the DAX3 XML at *xml_path*."""
    # A prefix the reader typed stands, "Dolby" included.
    return TuningSource(xml_path, "Dolby" if prefix is None else prefix)


def dax2(preset: Dax2Preset, prefix: str | None) -> TuningSource:
    """A run from the DAX2 *preset* a driver's .inf binds to this device."""
    return TuningSource(None, "Dolby" if prefix is None else prefix,
                        dax2=preset)


def standalone(fmt: StandaloneFormat, origin: Path,
               prefix: str | None) -> TuningSource:
    """A run from *fmt*'s tuning in *origin*, read by `resolved`."""
    return TuningSource(None, fmt.prefix if prefix is None else prefix,
                        format=fmt, origin=Path(origin))
