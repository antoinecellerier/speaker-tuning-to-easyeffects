"""Which tuning a run builds from, and reading it: a Dolby DAX3 XML, a
Sonitude tuning, or a Dolby DAX2 preset in a driver `.inf`.

Most devices ship a DAX3 XML, and a vendor APO layer may stack on it
(`lib/apo/stacked.py` `find_for_xml`). A device that ships no DAX3 XML has
its whole tuning in one of the other two. One class per kind answers what a
run from it can do and reads it: whether its presets come in voicings, which
profiles it builds, the headings and `ParsedTuning` of each, the flags that
can't apply, and the file the closing asks for. main() asks the source, never
which kind it is. Finding the source is `lib/tuning_lookup.py`'s.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`): the readers that aren't
are imported by the members that read, as `lib/apo/dax2.py` `tuning` does.
It prints nothing but what the DAX3 reader announces as it reads a profile;
the run reports what the rest returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from lib.apo import dax2, layer, sonitude, stacked
from lib.apo.dax2 import Dax2Preset
from lib.apo.layer import ApoLayer

# Each builds a Dolby stage, and a Sonitude source has none:
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
    """The tuning a run builds from, as main() reads it. Each kind overrides
    what it has; the defaults here are what a kind without it answers."""
    # The preset-name stem: --prefix when given, the kind's own otherwise.
    prefix: str

    # --- what the source is --------------------------------------------------

    @property
    def closing_file(self) -> Path | None:
        """The tuning file the closing asks a report to attach."""
        raise NotImplementedError

    def found(self) -> tuple[str, str] | None:
        """What the run says it found, once bound, when that isn't the
        Dolby file it was given or auto-detected: the line naming it, and
        the line saying the run builds from it."""
        return None

    def listing(self) -> list[str]:
        """The --list lines below the "Endpoints and profiles in" header."""
        raise NotImplementedError

    @property
    def is_soundwire(self) -> bool:
        """Whether the tuning is for SoundWire speaker hardware, which
        several emitted parameters key off."""
        return False

    # --- what a run from it can do -------------------------------------------

    @property
    def profiles(self) -> dict[str, ApoLayer]:
        """Profile name → the layer that is that profile's whole tuning;
        none when no vendor layer is one."""
        return {}

    def experimental(self, profile: str | None) -> bool:
        """Whether no listener has heard *profile*'s tuning yet."""
        return False

    @property
    def has_voicings(self) -> bool:
        """Whether each profile's presets come in Dolby's voicings
        (Balanced, Detailed, Warm)."""
        return True

    @property
    def voicings_carried(self) -> tuple[str, ...] | None:
        """The voicings the source carries when it has only some of Dolby's
        three, so a wrapper can refuse the others; None for all three."""
        return None

    @property
    def level_steps(self) -> bool:
        """Whether the source has presets whose level differs from one
        another's: several voicings or modes, built now or by a later run."""
        return True

    def mode_count(self, args) -> int:
        """How many sound modes the run could build from the source."""
        raise NotImplementedError

    def profiles_to_build(self, args) -> list[str | None]:
        """The profiles this run builds; None stands for the first. Empty
        when --all-profiles finds none."""
        raise NotImplementedError

    def layer_for(self, profile: str | None) -> ApoLayer | None:
        """The layer that is *profile*'s whole tuning, or None when no vendor
        layer is."""
        return self.profiles.get(profile)

    @property
    def buildable(self) -> list[str]:
        """The profiles the source carries whose layer changes the sound,
        in order. A preset of one that changes nothing would be of no
        use."""
        return [p for p, bound in self.profiles.items()
                if not bound.changes_nothing]

    @property
    def unbuildable(self) -> list[str]:
        """The profiles the source carries whose layer changes nothing."""
        return [p for p in self.profiles if p not in self.buildable]

    @property
    def unoffered(self) -> tuple[str, ...]:
        """Filter markers the menus and the closing must not offer."""
        return ()

    @classmethod
    def refusal(cls, args) -> str | None:
        """The error for the first flag in *args* this kind can't take,
        or None."""
        return None

    @property
    def warnings(self) -> tuple[str, ...]:
        """What binding the source to this machine noted for the run to
        warn of: each a sentence, then after a newline each, lines to print
        unwrapped, such as a path to copy."""
        return ()

    # --- reading it ----------------------------------------------------------

    def vendor_layer(self) -> ApoLayer | None:
        """The vendor APO layer bound to the device, looked up once for the
        run. Raises `UnsupportedApoConfig` for one that binds it but can't
        be read."""
        return None

    def headings(self, profile: str | None, args) -> list[str]:
        """The run's heading lines for *profile*, before its report."""
        # "(mode=normal)" is suppressed when it is the default: an
        # unexplained internal knob on every run's second line.
        mode = "" if args.mode == "normal" else f" (mode={args.mode})"
        return [f"Endpoint: {args.endpoint}{mode} (the output these "
                "presets are for)"]

    def tuning(self, profile: str | None, args):
        """*profile*'s `ParsedTuning` (`lib/dax/parse.py`)."""
        raise NotImplementedError

    def unconfirmed_finding(self):
        """The `Finding` asking for a comparison with Windows, for a source
        whose presets nobody has compared yet; None otherwise."""
        return None


@dataclass(frozen=True)
class DolbyXml(TuningSource):
    """A DAX3 XML: the Dolby file holds every endpoint, mode, profile and
    voicing, and the run reads them from it."""
    xml_path: Path

    @classmethod
    def for_run(cls, xml_path: Path, args) -> DolbyXml:
        """A run from the DAX3 XML at *xml_path*."""
        # A prefix the reader typed stands, "Dolby" included.
        return cls("Dolby" if args.prefix is None else args.prefix, xml_path)

    @property
    def closing_file(self) -> Path:
        return self.xml_path

    def listing(self) -> list[str]:
        from lib.dax import parse
        return parse.list_endpoints(self.xml_path)

    @property
    def is_soundwire(self) -> bool:
        # The bus is recorded only in the filename (`lib/dax/discover.py`).
        from lib.dax import discover
        return discover.is_soundwire_xml(Path(self.xml_path).name)

    def mode_count(self, args) -> int:
        from lib.dax import parse
        return len(parse.get_profile_types(self.xml_path, args.endpoint,
                                           args.mode))

    def profiles_to_build(self, args) -> list[str | None]:
        if args.all_profiles:
            from lib.dax import parse
            return parse.get_profile_types(self.xml_path, args.endpoint,
                                           args.mode)
        return [args.profile]

    def vendor_layer(self) -> ApoLayer | None:
        return stacked.find_for_xml(Path(self.xml_path))

    def headings(self, profile: str | None, args) -> list[str]:
        if not self.is_soundwire:
            return super().headings(profile, args)
        # Names the practical difference. An earlier wording, "enhanced
        # preset generation", told the reader nothing and read as either
        # good news or a warning (round 2).
        # "where your tuning enables it": this prints from the filename,
        # before any profile is parsed, and plenty of profiles disable
        # the leveler outright (voice, off, most game). A flat "on by
        # default" would contradict the leveler section four lines
        # below, which correctly says "switched off in your tuning".
        return ["SoundWire speaker hardware detected — adds a bass enhancer, "
                "and keeps the volume leveler on where your tuning enables "
                "it", *super().headings(profile, args)]

    def tuning(self, profile: str | None, args):
        """*profile*'s tuning, read from the XML; its Profile lines print as
        it is read."""
        from lib.dax import parse
        return parse.parse_xml(self.xml_path, endpoint_type=args.endpoint,
                               operating_mode=args.mode,
                               profile_type=profile, announce_profile=True)


@dataclass(frozen=True)
class Sonitude(TuningSource):
    """A Sonitude tuning (`lib/apo/sonitude.py`): its content modes are its
    profiles, each a vendor layer that is that profile's whole tuning, all
    for the internal speakers. One stand-in curve replaces the voicings, so
    its presets are told apart by profile alone."""
    # The archive, unpacked download or DriverStore folder it was found in.
    origin: Path
    # Profile name → the layer bound to this machine that is that profile's
    # whole tuning. A run without --profile builds the first that changes
    # the sound (`buildable`).
    bound: dict[str, ApoLayer]
    # What binding noted for the run to warn of (`warnings`).
    noted: tuple[str, ...] = ()

    @classmethod
    def for_run(cls, origin: Path, args) -> Sonitude:
        """A run from the Sonitude tuning in *origin*, its profiles' layers
        bound to this machine.

        Raises RuntimeError when none binds it (`lib/apo/sonitude.py`
        `resolve`)."""
        noted: list[str] = []
        bound = sonitude.resolve(Path(origin), noted.append)
        # "Dolby" would mislabel a preset built from another engine's tuning.
        return cls("Sonitude" if args.prefix is None else args.prefix,
                   Path(origin), bound, tuple(noted))

    # The content modes no listener has heard yet: the run marks their
    # presets experimental wherever it lists them.
    UNHEARD: ClassVar[tuple[str, ...]] = ()
    # Whose tuning it is, as the refusals of the Dolby-only flags name it.
    OWNER: ClassVar[str] = "Sonitude"

    @property
    def profiles(self) -> dict[str, ApoLayer]:
        return self.bound

    @property
    def layer(self) -> ApoLayer | None:
        """The first-listed profile's layer, which names the tuning for the
        device as a whole: every profile's is read from the same preset."""
        return next(iter(self.bound.values()), None)

    @property
    def closing_file(self) -> Path | None:
        """The archive its default profile was read from. In a driver
        download that is the archive as unpacked, which holds this device's
        tuning without the rest of the download."""
        return self.layer.inf_path if self.layer is not None else None

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.noted

    def found(self) -> tuple[str, str] | None:
        if self.layer is None:
            return None
        return (f"Found {self.layer.label} for {self.layer.hardware_id}",
                "No Dolby file was given or found, so the presets are built "
                "from this tuning alone.")

    def listing(self) -> list[str]:
        """The tuning's profiles are its content modes, all for the one
        output it is read for."""
        lines = ["  endpoint: internal_speaker (operating_mode=normal)"]
        for name in self.bound:
            mark = (" (changes nothing in this tuning)"
                    if name in self.unbuildable
                    else " (experimental)" if self.experimental(name)
                    else "")
            lines.append(f"    profile: {name}{mark}")
        return lines

    def experimental(self, profile: str | None) -> bool:
        return profile in self.UNHEARD

    @property
    def has_voicings(self) -> bool:
        return False

    def mode_count(self, args) -> int:
        # A mode that changes nothing is never built, so it is not one of
        # the modes --all-profiles would add.
        return len(self.buildable)

    def profiles_to_build(self, args) -> list[str]:
        """The profiles --profile and --all-profiles pick; the first
        buildable one when neither is given.

        Raises ValueError, as the Dolby file's own lookup does, for a
        profile the tuning doesn't hold or one that changes nothing, and
        when no profile changes anything."""
        profile = args.profile
        names, buildable = list(self.bound), self.buildable
        if profile is not None and profile.lower() not in self.bound:
            raise ValueError(f"Profile '{profile}' not found. Available: "
                             f"{', '.join(names)}")
        if not buildable:
            raise _ends_on_its_reason(ValueError(
                "This tuning changes nothing in any of its sound modes "
                f"({', '.join(names)}), so there is no preset to build."))
        if args.all_profiles:
            return buildable
        if profile is None:
            return buildable[:1]
        if profile.lower() not in buildable:
            raise _ends_on_its_reason(ValueError(
                f"Profile '{profile}' changes nothing in this tuning, so "
                "there is no preset to build for it. Profiles that change "
                f"the sound: {', '.join(buildable)}"))
        return [profile.lower()]

    @property
    def unoffered(self) -> tuple[str, ...]:
        """A Sonitude preset is the vendor layer, so the menus' --disable
        row and the closing's "on by default" note would offer a run
        `refusal` refuses. level-restore's --enable row goes too: the curve
        only cuts, so the peak it would hand back is a rounding error, and
        the flag is refused."""
        return (f"{layer.FLAG}-active", "level-restore")

    @classmethod
    def refusal(cls, args) -> str | None:
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
            if name != layer.FLAG:
                return (f"--disable {name} acts on a Dolby stage, and this "
                        "tuning has none — drop it")
        if layer.FLAG in args.disable:
            return (f"--disable {layer.FLAG} would leave nothing to "
                    f"build: the {cls.OWNER} tuning is this run's only "
                    "one — drop it")
        return None

    def vendor_layer(self) -> ApoLayer | None:
        return self.layer

    def headings(self, profile: str | None, args) -> list[str]:
        """The output, in place of the Dolby endpoint's, and the profile
        with why it was picked."""
        picked = bool(args.profile or args.all_profiles)
        return ["Internal speakers (the output this preset is for)",
                f"Profile: {self.profile_banner(profile, picked)}"]

    def profile_banner(self, profile: str, picked: bool) -> str:
        """What the run's "Profile: " line says of *profile*, in the Dolby
        banner's wording. *picked* is whether --profile or --all-profiles
        chose it, which needs no reason given.

        A mode that changes nothing is never built, so when the first is
        such, the pick is the first that changes the sound."""
        n_profiles, n_built = len(self.bound), len(self.buildable)
        if picked:
            return profile
        if n_profiles == 1:
            return f"{profile} (this speaker's only sound mode)"
        if n_built == 1:
            return (f"{profile} (the only one of this speaker's "
                    f"{n_profiles} sound modes that changes the sound)")
        if profile != next(iter(self.bound)):
            return (f"{profile} (the first-listed of the {n_built} sound "
                    "modes that change this speaker's sound — --list names "
                    "them; --profile picks)")
        return (f"{profile} (the first-listed of this speaker's "
                f"{n_profiles} sound modes — --list names them; --profile "
                "picks)")

    def tuning(self, profile: str | None, args):
        """A flat stand-in: every Dolby stage is absent, and *profile*'s
        layer is the whole tuning."""
        from lib.dax import parse
        return parse.flat_tuning(sonitude.probe_freqs(), profile)

    def unconfirmed_finding(self):
        """A preset built from a Sonitude tuning alone has been compared
        with two laptops' Windows recordings and heard by one listener: ask
        for the comparison."""
        from lib.report import findings
        return findings._vendor_only_finding()


@dataclass(frozen=True)
class Dax2(TuningSource):
    """The DAX2 preset a driver's .inf binds to this device, read as a Dolby
    tuning without an XML: one set of speaker settings and one voicing
    curve, built through the Dolby stages it carries (`lib/apo/dax2.py`)."""
    preset: Dax2Preset

    @classmethod
    def for_run(cls, preset: Dax2Preset, args) -> Dax2:
        """A run from the DAX2 *preset* a driver's .inf binds to this
        device."""
        return cls("Dolby" if args.prefix is None else args.prefix, preset)

    @property
    def closing_file(self) -> Path:
        return self.preset.inf_path

    def found(self) -> tuple[str, str]:
        return (f"Found {self.preset.label} for {self.preset.hardware_id}",
                "This driver keeps its Dolby tuning in an older format, "
                "DAX2, so the preset is built from that.")

    def listing(self) -> list[str]:
        """The one preset, for the one output it is read for."""
        return ["  endpoint: internal_speaker (operating_mode=normal)",
                f"    preset: {self.preset.name} (the only one; "
                "experimental)"]

    def experimental(self, profile: str | None) -> bool:
        return True

    @property
    def voicings_carried(self) -> tuple[str, ...]:
        """It carries one IEQ curve, built as the Balanced voicing alone."""
        return (dax2.VOICING,)

    @property
    def level_steps(self) -> bool:
        return False

    def mode_count(self, args) -> int:
        return 1

    def profiles_to_build(self, args) -> list[None]:
        # `refusal` turns away --profile and --all-profiles.
        return [None]

    @classmethod
    def refusal(cls, args) -> str | None:
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

    def headings(self, profile: str | None, args) -> list[str]:
        return [*super().headings(profile, args),
                f"Tuning: {self.preset.name} (Dolby DAX2, the only one)"]

    def tuning(self, profile: str | None, args):
        return dax2.tuning(self.preset)

    def unconfirmed_finding(self):
        from lib.report import findings
        return findings._dax2_finding()
