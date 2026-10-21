"""Which tuning a run builds from: a Dolby DAX3 XML, or a standalone vendor
format that stands in for one.

Most devices ship a DAX3 XML, and a vendor APO layer may stack on it
(`lib/apo/discover.py` `find_for_xml`). A device that ships no Dolby file has
its whole tuning in a standalone vendor format instead (`STANDALONE` there).
main() resolves which once, from the parsed arguments, and reads every
difference between the two kinds of run off the `TuningSource`: the preset
names, the profiles a standalone tuning holds, the flags that can't apply, and
the file the closing asks for.

Stdlib-only (`tests/test_layout.py` `STDLIB_ONLY`), and it prints nothing:
the run reports what it returns. Finding the XML itself, by auto-detection
or in a driver download, prints as it goes, so that stays in main().
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from lib.apo import layer as apo_layer
from lib.apo.layer import ApoLayer, StandaloneFormat

# `TuningSource.kind`'s two values.
DOLBY = "dolby"
STANDALONE = "standalone"

# --hw-id picks among one standalone format's presets (`lib/apo/sonitude.py`
# `parse_hw_id`), so a Dolby file leaves it nothing to pick.
_HW_ID_WITHOUT_SOURCE = (
    "--hw-id picks among Sonitude tunings; pass the driver download or "
    "SEAPODAT archive that holds them with it")

# Each builds or restores a Dolby stage, and a standalone source has none:
# refused rather than run and reported as having had no effect.
_DOLBY_STAGE_FLAGS = ("autogain", "dialog", "virtual-bass", "level-restore")


@dataclass(frozen=True)
class TuningSource:
    """The tuning a run builds from, as main() reads it."""
    # DOLBY or STANDALONE.
    kind: str
    # The Dolby file the run reads, which the closing also asks for. None
    # for a standalone source: there is no XML to ask for, and the download
    # names the vendor's file.
    xml_path: Path | None
    # The preset-name stem: --prefix when given, "Dolby" or the standalone
    # format's own otherwise.
    prefix: str
    # Standalone only: its format, and the archive or unpacked download it
    # was found in.
    format: StandaloneFormat | None = None
    origin: Path | None = None
    # Standalone only, once `resolved`: profile name → the layer that is that
    # profile's whole tuning, the one a run without --profile builds first.
    profiles: dict[str, ApoLayer] = field(default_factory=dict)

    @property
    def layer(self) -> ApoLayer | None:
        """Standalone only: the default profile's layer, which names the
        tuning for the device as a whole."""
        return next(iter(self.profiles.values()), None)

    def experimental(self, profile: str | None) -> bool:
        """Whether no listener has heard *profile*'s tuning yet."""
        return (self.kind == STANDALONE
                and profile in self.format.experimental)

    def profiles_to_build(self, profile: str | None,
                          all_profiles: bool) -> list[str]:
        """Standalone only: the profiles this run builds, as --profile and
        --all-profiles pick them; the default one when neither is given.

        Raises ValueError, as the Dolby file's own lookup does, for a
        profile the tuning doesn't hold."""
        names = list(self.profiles)
        if all_profiles:
            return names
        if profile is None:
            return names[:1]
        if profile.lower() not in self.profiles:
            raise ValueError(f"Profile '{profile}' not found. Available: "
                             f"{', '.join(names)}")
        return [profile.lower()]

    @property
    def closing_file(self) -> Path | None:
        """The tuning file the closing asks a report to attach."""
        return self.xml_path

    @property
    def unoffered(self) -> tuple[str, ...]:
        """Filter markers the menus and the closing must not offer.

        A standalone preset is the vendor layer, so the menus' --disable row
        and the closing's "on by default" note would offer a run `refusal`
        refuses. level-restore's --enable row goes too: the curve only cuts,
        so the peak it would hand back is a rounding error, and the flag is
        refused.
        """
        if self.kind == DOLBY:
            return ()
        return (f"{apo_layer.FLAG}-active", "level-restore")

    def refusal(self, args) -> str | None:
        """The error for the first flag in *args* this source can't take,
        or None."""
        if self.kind == DOLBY:
            return _HW_ID_WITHOUT_SOURCE if args.hw_id else None
        # No Dolby file: the tuning's profiles are its content modes, all for
        # the internal speakers, so there is no other endpoint or operating
        # mode to pick. The vendor layer is the whole tuning, so nothing is
        # left to build without it.
        for flag, given in (("--endpoint",
                             args.endpoint != "internal_speaker"),
                            ("--mode", args.mode != "normal")):
            if given:
                return f"{flag} reads a Dolby file, and this tuning has none"
        for name in _DOLBY_STAGE_FLAGS:
            if name in args.enable:
                return (f"--enable {name} acts on a Dolby stage, and this "
                        "tuning has none")
        if apo_layer.FLAG in args.disable:
            return (f"--disable {apo_layer.FLAG} would leave nothing to "
                    f"build: the {self.format.owner} tuning is this device's "
                    "only one")
        return None

    def resolved(self, hw_id: str | None,
                 warn: Callable[[str], None]) -> TuningSource:
        """This source with its profiles' layers read; a Dolby source as it
        is.

        Raises RuntimeError, from the format, when no layer binds this
        machine or *hw_id*'s IDs."""
        if self.kind == DOLBY:
            return self
        return replace(self, profiles=self.format.resolve(self.origin, hw_id,
                                                          warn))


def dolby(xml_path: Path, prefix: str | None) -> TuningSource:
    """A run from the DAX3 XML at *xml_path*."""
    # A prefix the reader typed stands, "Dolby" included.
    return TuningSource(DOLBY, xml_path,
                        "Dolby" if prefix is None else prefix)


def standalone(fmt: StandaloneFormat, origin: Path,
               prefix: str | None) -> TuningSource:
    """A run from *fmt*'s tuning in *origin*, read by `resolved`."""
    return TuningSource(STANDALONE, None,
                        fmt.prefix if prefix is None else prefix,
                        format=fmt, origin=Path(origin))
