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
authoritative.

Stdlib-only, deliberately: nothing here is DSP, and `--doctor` reaches it.
"""

from __future__ import annotations

import configparser
import contextlib
import json
import os
from pathlib import Path

from lib import version


BYPASS_PRESET_NAME = "Nothing"


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
        "_generator": f"dolby_to_easyeffects.py {version.get_version()}",
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
