"""What gets written beside the preset: autoload files, the bypass, EE's rc.

Three jobs that share one property — they all touch state EasyEffects owns, so
a half-written file is worse than none. `_atomic_write` is the single home for
the temp-then-rename that prevents it, and every writer in the project goes
through it (the preset JSON and the `.irs` included, from the generator).

`read_ee_rc` is here rather than under `lib/report/` even though `--doctor` is
its loudest reader: parsing the rc is the other half of patching it, and
`set_autoload_fallback` cannot be separated from the parser whose output it
edits.

`BYPASS_PRESET_NAME` rides along for the same reason — the empty preset this
module writes is the one the doctor recognises, and one spelling has to be
authoritative. So does `starting_preset`: the rule a bare `--autoload`
follows is the rule the end-of-run reload and the closing copy must follow
too, and one function is how they can't disagree.

Stdlib-only, deliberately: nothing here is DSP, and `--doctor` reaches it.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import os
import re
from pathlib import Path

from lib import version


BYPASS_PRESET_NAME = "Nothing"

# The stamp every preset this tool writes carries at top level, and the one
# `--doctor` recognises them by (`lib/report/environment.py`
# `is_generated_preset`): the user's own presets share the folder, and the
# doctor must not judge those by this tool's standards (issue #84).
GENERATOR_PREFIX = "dolby_to_easyeffects.py"


def generator_stamp() -> str:
    """The ``_generator`` value for a preset written by this run."""
    return f"{GENERATOR_PREFIX} {version.get_version()}"


def generator_version(preset_json) -> str:
    """The tool version stamped into a preset we wrote, "" when there is no
    stamp of ours to read — a foreign preset, or an EasyEffects GUI re-save,
    which rebuilds the JSON and drops unknown top-level keys."""
    if not isinstance(preset_json, dict):
        return ""
    stamp = str(preset_json.get("_generator", ""))
    if not stamp.startswith(GENERATOR_PREFIX + " "):
        return ""
    return stamp[len(GENERATOR_PREFIX) + 1:].strip()


def kernel_belongs_to(preset_name: str, stem: str) -> bool:
    """Whether an impulse-file stem is one this tool writes for *preset_name*:
    ``{preset_name}-<8 hex>`` (`lib.preset.emit.kernel_name`) or the legacy
    unhashed ``{preset_name}``. The voicing is always the last name part, so
    nothing else this tool writes can match. Shared by the stale-impulse sweep
    in `lib/preset/emit.py` and the doctor's "is this preset ours?" test, so
    the two cannot drift apart."""
    return re.fullmatch(rf"{re.escape(preset_name)}(-[0-9a-f]{{8}})?", stem) is not None


def starting_preset(autoload_arg, preset_names: list[str]) -> str:
    """The one preset a run points at.

    The single source for three readers that must agree: what a bare
    ``--autoload`` wires to the speakers, what the end-of-run reload loads
    when nothing of ours is playing (``lib/preset/reload.py``), and what
    the closing copy says to start with. ``--autoload <name>`` names it
    outright; otherwise the first preset built — under ``--all-profiles``
    that is the endpoint's first profile, the one a run without
    ``--profile`` builds, so the two runs point at the same voicing.
    ``<setting><default_profile>`` is reported, never acted on
    (docs/reference.md); if that ever changes, it changes here and in the
    bare run's profile pick together. The entry script resolves it once
    and hands the name to all three, so a change here reaches every
    reader. Empty when nothing was built.
    """
    if isinstance(autoload_arg, str):
        return autoload_arg
    return preset_names[0] if preset_names else ""


@contextlib.contextmanager
def _atomic_write(path: Path):
    """Yield a same-directory temp path, then os.replace it into place when the
    block completes — so a crash mid-write can't leave a truncated file that
    EasyEffects would silently fail to load. The dotfile temp name keeps a
    leftover from a failed write out of EE's ``*.json`` / ``*.irs`` scan. The
    single home for the temp-then-rename pattern; callers fill the temp however
    they like (text, WAV, configparser)."""
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        yield tmp
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


def _atomic_write_text(path: Path, data: str) -> None:
    """Atomically write text to ``path`` (see _atomic_write)."""
    with _atomic_write(path) as tmp:
        tmp.write_text(data)


def write_autoload(autoload_dir: Path, device_name: str, device_description: str,
                   device_profile: str, preset_name: str, dry_run: bool = False) -> Path:
    """Write an EasyEffects autoload config file for a device/route → preset mapping.

    EasyEffects loads this file when the given PipeWire sink becomes the active
    output, automatically switching to the named preset.

    File is named '{device_name}:{device_profile}.json' (with '/' replaced by '_'),
    matching EasyEffects' AutoloadManager::getFilePath() convention.
    """
    safe_name = device_name.replace("/", "_")
    safe_profile = device_profile.replace("/", "_")
    path = autoload_dir / f"{safe_name}:{safe_profile}.json"
    if dry_run:
        return path
    autoload_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps({
        "device": device_name,
        "device-description": device_description,
        "device-profile": device_profile,
        "preset-name": preset_name,
    }, indent=4) + "\n")
    return path


def read_autoload_entries(autoload_dir: Path) -> list[dict]:
    """Every autoload mapping EasyEffects would act on, as written above.

    The reader for `write_autoload`'s format, kept beside it so the four key
    names have one owner. `--doctor` is the caller: it needs to answer "what
    loads on the speakers?" while the current output is something else.

    Never raises. A missing directory, an unreadable file and a stray
    non-JSON one all come back as "no mapping" — a diagnostic that crashes on
    a file EasyEffects tolerates is worse than one that reports nothing.
    """
    entries: list[dict] = []
    try:
        paths = sorted(autoload_dir.glob("*.json"))
    except OSError:
        return entries
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        # EasyEffects reads these as objects; a JSON array or scalar in the
        # directory is somebody else's file, not a mapping we can act on.
        if isinstance(data, dict):
            entries.append(data)
    return entries


def write_bypass_preset(output_dir: Path, preset_name: str,
                        dry_run: bool = False) -> tuple[Path, str]:
    """Write an empty bypass preset used as EasyEffects' global fallback.

    Returns (path, status) where status is "written", "kept", or "would-write".
    If a preset of the same name already exists on disk, it is preserved — the
    user may have hand-built one and we don't want to clobber it.
    """
    path = output_dir / f"{preset_name}.json"
    if path.exists():
        return path, "kept"
    if dry_run:
        return path, "would-write"
    output_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps({
        "_generator": generator_stamp(),
        "output": {"blocklist": [], "plugins_order": []},
    }, indent=4) + "\n")
    return path, "written"


def _ee_rc_parser() -> configparser.ConfigParser:
    """A ConfigParser configured to read/write an easyeffectsrc faithfully.

    EE uses camelCase keys and INI files with no interpolation; the default
    parser would lowercase keys (EE then ignores them) and choke on '%'.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    return parser


def read_ee_rc(rc_text: str) -> dict:
    """Parse easyeffectsrc text into the fields the diagnostics care about.

    Pure (text in, dict out — no filesystem). Verified key locations against a
    live EE 8.x rc: the loaded output preset is ``[Presets]
    lastLoadedOutputPreset``; the global Fallback Preset toggle and the
    Background-Service ``autostartOnLogin`` / ``enableServiceMode`` flags are
    ``[Window]`` keys (``enableServiceMode`` is written only when toggled off —
    an absent key is the ON default); the target sink and active plugin chain
    are
    ``[StreamOutputs] outputDevice``/``plugins``. Missing sections/keys fall
    back to empty/False so callers never KeyError on a partial or older rc.

    Everything here is a *snapshot*, not live state: EasyEffects writes this
    file from ``saveAll()``, which runs on quit and on an autosave timer that
    ``Main.qml`` starts only while the window is open. In service mode with
    the window closed it is never rewritten, so values drift from what EE is
    actually doing. ``lib/report/doctor_run.py`` prefers live sources where
    they exist and falls back to these.
    """
    parser = _ee_rc_parser()
    try:
        parser.read_string(rc_text)
    except configparser.Error:
        pass  # garbage rc → all defaults below

    def g(section: str, key: str, default: str = "") -> str:
        return parser.get(section, key, fallback=default).strip()

    plugins = g("StreamOutputs", "plugins")
    return {
        "last_output_preset": g("Presets", "lastLoadedOutputPreset"),
        "fallback_preset": g("Window", "outputAutoloadingFallbackPreset"),
        # EE serialises booleans as the literal KConfig strings "true"/"false",
        # so `.lower() == "true"` is an exact-format check matching what EE 8.x
        # writes. If EE ever emitted "1"/"yes" these would read False and the
        # autoload patch would re-run on every invocation.
        "uses_fallback": g("Window", "outputAutoloadingUsesFallback",
                           "false").lower() == "true",
        "autostart_on_login": g("Window", "autostartOnLogin",
                                "false").lower() == "true",
        # enableServiceMode is written only when toggled OFF (non-default);
        # an absent key is the ON default — hence default "true" here, the
        # opposite polarity to autostartOnLogin above.
        "service_mode": g("Window", "enableServiceMode",
                          "true").lower() == "true",
        "output_device": g("StreamOutputs", "outputDevice"),
        "output_plugins": [p for p in plugins.split(",") if p],
        # Written only when toggled OFF, so an absent key is the ON default —
        # same polarity as service_mode above. True (the default) means EE
        # follows the system default sink and outputDevice is merely its cache
        # of it; false means the user pinned EE to that device, and then this
        # file is authoritative because nothing but the GUI can change it.
        "use_default_output_device": g("StreamOutputs", "useDefaultOutputDevice",
                                       "true").lower() == "true",
        # [EffectsPipelines] bypass, default false. Only a fallback for the
        # live get_global_bypass request over EE's local socket
        # (doctor_run._ee_query) — a stale copy of this must never raise a
        # confident "your audio is bypassed" verdict.
        "bypass": g("EffectsPipelines", "bypass", "false").lower() == "true",
    }


def set_autoload_fallback(rc_path: Path, preset_name: str,
                          dry_run: bool = False) -> tuple[str, str]:
    """Enable EasyEffects' global Fallback Preset toggle in its KConfig file.

    EasyEffects 8.x stores the toggle as two keys under the [Window] section
    (they're bound to QML properties attached to the main window object —
    quirky location, but matches EE's config binding). No EE CLI, D-Bus or
    local-socket command reaches this setting — the socket's set_property
    only addresses per-plugin databases (plugin#instance), not [Window] keys
    — so direct file edit is the only option.

    Returns (status, existing_preset) where status is one of:
      - "already-configured": both keys set and fallback enabled; file untouched.
      - "patched": file created or keys set/updated.
      - "would-patch": dry-run equivalent of "patched".
    """
    rc_text = ""
    if rc_path.exists():
        try:
            rc_text = rc_path.read_text(encoding="utf-8")
        except OSError:
            rc_text = ""

    rc = read_ee_rc(rc_text)
    existing_preset = rc["fallback_preset"]
    if rc["uses_fallback"] and existing_preset:
        return "already-configured", existing_preset

    if dry_run:
        return "would-patch", existing_preset

    parser = _ee_rc_parser()
    if rc_text:
        parser.read_string(rc_text)
    section = "Window"
    if not parser.has_section(section):
        parser.add_section(section)
    parser.set(section, "outputAutoloadingFallbackPreset", preset_name)
    parser.set(section, "outputAutoloadingUsesFallback", "true")

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    with _atomic_write(rc_path) as tmp, tmp.open("w", encoding="utf-8") as f:
        parser.write(f, space_around_delimiters=False)
    return "patched", existing_preset
