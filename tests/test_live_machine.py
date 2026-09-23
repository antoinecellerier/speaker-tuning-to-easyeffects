"""The real machine still says what the parsers read — its tools, and its
/proc, /sys and /etc.

Every other test runs with this machine's tools switched off
(`lib/tool_env.py`) and its host files re-rooted under an empty directory
(`lib/host.py`): the parsers are fed recorded or hand-written output, and
nothing in a hermetic suite notices when a tool's real output drifts away from
those fakes — a new `pw-top` column, a `pw-dump` schema change, `amixer`
quoting a name differently, a kernel reshaping the `/proc/asound` codec dump.
This file is the one place each of those is read for real, through the same
function the code calls, so a drift
fails here on the developer's machine instead of reaching a user as a row that
reads "not read".

The assertions are about the parse, not the machine: that an answer came back
parsed — a version, a clock rate, a node row, a control header — rather than
as the "didn't answer" a format change degrades into. Never what the values
are. Each test skips where its tool, or the PipeWire daemon it asks, is
absent, so CI (which has none of them) skips the file; on a dev machine a skip
here is a tool with no drift check, worth reading.

Deliberately absent: `systemctl --user restart pipewire` (`install._activate`),
which would restart the developer's audio — fakes only. `git describe` is
never gated and runs for real in `tests/test_version.py`. `lv2info` and
`spa-json-dump` inside `validate` and `checks.parse_conf` have their own
`live_machine` tests beside the code they serve.
"""

import os
import platform
import re
from pathlib import Path

import pytest

from lib import ee_paths, host, packages, tool_env
# Bound before the autouse `no_live_easyeffects_probe` fixture patches the
# module attribute, as tests/test_pw_doctor.py does.
from lib.ee_socket import easyeffects_running as unpatched_ee_probe
from lib.dax import discover
from lib.hardware import amps, codecs, sinks, speakers
from lib.pipewire import checks, session
from lib.report import doctor_run
from lib.report import speaker as report_speaker

pytestmark = pytest.mark.live_machine


def _need(*tools: str) -> None:
    missing = [t for t in tools if tool_env.which(t) is None]
    if missing:
        pytest.skip(f"not installed: {', '.join(missing)}")


def _need_pipewire() -> None:
    """Checked by its socket, not by a tool: every tool here is under test."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not (Path(runtime) / "pipewire-0").exists():
        pytest.skip("no PipeWire daemon running")


def _stdout(argv: list[str]) -> str:
    return tool_env.run(argv, capture_output=True, text=True,
                        timeout=15).stdout


def test_pw_dump_still_parses_into_audio_sinks():
    _need("pw-dump")
    _need_pipewire()
    dump = sinks._read_pw_dump()
    assert isinstance(dump, list) and dump, "pw-dump output didn't parse"
    assert all("id" in obj and "type" in obj for obj in dump)
    # The doctor's own pw-dump boundary, read the same way.
    assert checks._pw_dump(), "checks._pw_dump() read no graph"
    found = sinks._enumerate_audio_sinks()
    assert found, "a running PipeWire listed no Audio/Sink node"
    assert all(s["name"] for s in found)


def test_pw_metadata_still_gives_the_clock():
    _need("pw-metadata")
    _need_pipewire()
    clock = session.read_settings()
    assert clock.ok, clock.reason
    assert clock.rate.isdigit() and int(clock.rate) > 0, clock
    assert clock.quantum.isdigit() and int(clock.quantum) > 0, clock


def test_pw_top_still_parses_into_node_rows():
    _need("pw-top")
    _need_pipewire()
    snapshots = session.parse_pwtop(_stdout(["pw-top", "-b", "-n", "2"]))
    assert len(snapshots) >= 2, "pw-top's batch snapshots weren't told apart"
    rows = snapshots[-1]
    assert rows, "the last snapshot parsed to no node rows"
    assert all(r.state in "SIRC" for r in rows.values()), rows
    drivers = [r for r in rows.values() if r.driver and r.rate]
    assert drivers, f"no driver row carried a rate: {rows}"


def test_pw_cli_still_reports_the_running_daemons_version():
    _need("pw-cli")
    _need_pipewire()
    version = session.pipewire_version()
    assert version.ok and len(version.parts) >= 2, version


def test_wireplumber_still_reports_its_version():
    _need("wireplumber")
    version = session.wireplumber_version()
    assert version.ok and len(version.parts) >= 2, version


def test_pw_cli_node_listing_still_names_nodes_verbatim():
    """`install._verify_sinks` decides a chain loaded by finding each node's
    `node.name` as a substring of `pw-cli ls Node`; a listing that quoted or
    shortened the names would read every chain as missing."""
    _need("pw-cli", "pw-dump")
    _need_pipewire()
    names = [s["name"] for s in sinks._enumerate_audio_sinks()]
    assert names
    listing = _stdout(["pw-cli", "ls", "Node"])
    assert [n for n in names if n not in listing] == []


def test_pgrep_still_answers_for_a_running_process():
    _need("pgrep")
    _need_pipewire()
    age = session.process_age("pipewire")
    assert age is not None and age > 0
    # Three states, and "couldn't ask" is the one a format change produces.
    assert unpatched_ee_probe() in (True, False)


def test_amixer_still_prints_the_shapes_the_parsers_read():
    _need("amixer")
    if not Path("/proc/asound/card0").exists():
        pytest.skip("no ALSA card 0")
    scontrols = _stdout(["amixer", "-c0", "scontrols"]).splitlines()
    assert scontrols
    # `_detect_soundwire_speakers` reads the quoted name off these lines.
    assert all(re.fullmatch(r"Simple mixer control '[^']*',\d+", line)
               for line in scontrols), scontrols[:5]
    contents = _stdout(["amixer", "-c", "0", "contents"])
    heads = [b for b in re.split(r"(?=^numid=)", contents, flags=re.MULTILINE)
             if b.startswith("numid=")]
    assert heads, "amixer contents printed no numid= blocks"
    unparsed = [b.splitlines()[0] for b in heads
                if not speakers._AMIXER_CONTROL_HEAD_RE.match(b)]
    assert unparsed == []
    assert isinstance(speakers.detect_speaker_firmware_gates(), list)


def test_the_kernel_log_is_still_readable_as_plain_lines():
    """`amps` greps the log for amplifier drivers; `-o cat` is what keeps
    journald's own prefixes off the lines."""
    if tool_env.which("journalctl") is None and tool_env.which("dmesg") is None:
        pytest.skip("neither journalctl nor dmesg")
    log = amps._read_kernel_log()
    if log is None:
        pytest.skip("the kernel log isn't readable by this user")
    assert "Linux version" in log or len(log.splitlines()) > 10
    assert not re.match(r"-- (Journal|Logs) begin", log)


def test_lv2info_still_answers_the_plugin_presence_probe():
    _need("lv2info")
    probe = checks._probe_plugins()
    assert probe.has_lv2info and probe.entries
    assert any(present for _, _, present in probe.entries), probe.entries


def test_the_easyeffects_version_probe_still_reads_a_version():
    if (tool_env.which("easyeffects") is None
            and not ee_paths.flatpak_app_installed()):
        pytest.skip("no EasyEffects installed")
    probe = doctor_run._probe_ee_version()
    assert probe.version is not None, probe


def test_flatpak_info_still_carries_the_easyeffects_version():
    _need("flatpak")
    if not ee_paths.flatpak_app_installed():
        pytest.skip("no EasyEffects Flatpak deployed")
    out = _stdout(["flatpak", "info", ee_paths.FLATPAK_APP_ID])
    version = doctor_run.parse_ee_version(doctor_run._flatpak_version_text(out))
    assert version is not None, out


def test_the_package_manager_still_names_an_easyeffects_candidate():
    fam = packages.family()
    argv = packages.available_version_cmd(packages.EASYEFFECTS, fam)
    if not argv:
        pytest.skip(f"no version query for distro family {fam!r}")
    _need(argv[0])
    major = doctor_run._distro_easyeffects_major(fam)
    assert major is not None and major >= 6, (fam, argv, major)


# --- host files -----------------------------------------------------------


def test_the_codec_dump_still_parses_into_codecs_and_speaker_pins():
    dumps = sorted(Path("/proc/asound").glob("card*/codec#*"))
    if not dumps:
        pytest.skip("no HDA codec")
    ids = codecs.get_hda_codec_ids()
    assert ids and all(vendor and subsystem for vendor, subsystem, _ in ids), ids
    text = "".join(dump.read_text() for dump in dumps)
    if re.search(r"Pin Default .*\] Speaker", text):
        info = report_speaker._gather_speaker_pins()
        assert info.speakers, "the codec lists a speaker pin; none was parsed"


def test_sound_cards_still_resolve_to_a_pci_subsystem():
    if not list(Path("/sys/class/sound").glob("card*")):
        pytest.skip("no sound card in /sys/class/sound")
    pci = codecs.get_pci_audio_subsystem()
    assert pci and all(re.fullmatch(r"[0-9A-F]{4}", part) for part in pci), pci


def test_soundwire_devices_still_parse():
    bus = Path("/sys/bus/soundwire/devices")
    if not bus.is_dir() or not any(bus.iterdir()):
        pytest.skip("no SoundWire devices")
    assert codecs.get_soundwire_ids()


def test_the_proc_asound_card_list_still_reads():
    cards = Path("/proc/asound/cards")
    if not cards.exists() or not cards.read_text().strip():
        pytest.skip("no ALSA cards")
    assert report_speaker._gather_speaker_pins().sound_cards


def test_dmi_still_carries_the_fields_the_report_names():
    if not Path("/sys/class/dmi/id").is_dir():
        pytest.skip("no DMI")
    missing = [name for _, name in report_speaker._DMI_FIELDS
               if not (report_speaker._DMI_DIR / name).exists()]
    assert missing == []


def test_os_release_still_identifies_the_distro():
    if not Path("/etc/os-release").exists():
        pytest.skip("no /etc/os-release")
    assert "ID" in packages.read_os_release()
    assert report_speaker.get_distro_pretty_name()


def test_the_kernel_release_still_reads_from_proc():
    assert host.kernel_release() == platform.release()


def test_proc_mounts_still_lists_a_windows_partition_that_is_mounted():
    text = Path("/proc/mounts").read_text()
    mounted = re.search(r"^\S+ \S+ (ntfs3?|fuseblk) ", text, re.MULTILINE)
    found = discover._ntfs_family_mountpoints()
    assert isinstance(found, list)
    if mounted:
        assert found, mounted.group(0)


def test_module_and_firmware_trees_are_still_where_they_are_read():
    assert Path("/sys/module").is_dir()
    assert isinstance(amps._loaded_amp_drivers(), list)
    if not Path("/lib/firmware").is_dir():
        pytest.skip("no /lib/firmware")
    assert isinstance(amps._list_firmware_files(["*"]), list)


# A tool with no test above, and why. Everything else lib/ runs must be named
# in this file — the guard below reads the source for the quoted name.
_NO_LIVE_CHECK = {
    "systemctl": "restarting PipeWire would cut the developer's audio",
    "git": "never gated; tests/test_version.py runs it for real",
    "spa-json-dump": "tests/test_pw_doctor.py reads real confs back through it",
}


def _tools_lib_runs() -> dict[str, str]:
    """Every tool name lib/ and the entry scripts hand to `tool_env`, read
    statically: `tool_env.which("x")`, a `["x", …]` passed to any call named
    `run`/`_run`, and the `["x", …]` a `for` loop tries in turn. Package-manager
    queries are built per distro at run time, out of this reach — the
    candidate test above covers whichever one the machine has."""
    import ast
    root = Path(__file__).resolve().parent.parent
    found: dict[str, str] = {}

    def note(lst, where):
        if (isinstance(lst, ast.List) and lst.elts
                and isinstance(lst.elts[0], ast.Constant)
                and isinstance(lst.elts[0].value, str)):
            found.setdefault(lst.elts[0].value, where)

    for path in sorted((root / "lib").rglob("*.py")) + sorted(root.glob("*.py")):
        if path.name == "tool_env.py":
            continue
        where = str(path.relative_to(root))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and node.args:
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else getattr(func, "id", None))
                if (name == "which" and isinstance(func, ast.Attribute)
                        and getattr(func.value, "id", None) == "tool_env"
                        and isinstance(node.args[0], ast.Constant)):
                    found.setdefault(node.args[0].value, where)
                elif name in ("run", "_run"):
                    note(node.args[0], where)
            elif isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
                for element in node.iter.elts:
                    note(element, where)
    return found


def test_every_tool_lib_runs_has_a_live_check():
    """The drift check only exists for a tool someone remembered to add here.
    This makes forgetting fail: a new tool in lib/ needs a test above, or an
    entry in `_NO_LIVE_CHECK` saying why it can't have one."""
    tools = _tools_lib_runs()
    # A scan that stopped seeing calls would pass vacuously.
    assert {"pw-dump", "pw-top", "amixer", "journalctl", "flatpak"} <= set(tools)
    source = Path(__file__).read_text(encoding="utf-8").split(
        "_NO_LIVE_CHECK = {")[0]
    missing = sorted(f"{tool} ({where})" for tool, where in tools.items()
                     if tool not in _NO_LIVE_CHECK and f'"{tool}"' not in source)
    assert not missing, (
        "tools lib/ runs with no live_machine test in this file: "
        + ", ".join(missing))
    stale = sorted(set(_NO_LIVE_CHECK) - set(tools) - {"systemctl"})
    assert not stale, f"_NO_LIVE_CHECK names tools lib/ no longer runs: {stale}"


# Every host location lib/ reads, and what checks it — a test above, or why
# nothing needs to. Kept as an explicit map rather than a source search:
# these strings also turn up in comments and printed commands.
_HOST_LOCATIONS = {
    "/proc/asound": "test_the_codec_dump_still_parses_into_codecs_and_speaker_pins",
    "/proc/asound/cards": "test_the_proc_asound_card_list_still_reads",
    "/sys/class/sound": "test_sound_cards_still_resolve_to_a_pci_subsystem",
    "/sys/bus/soundwire/devices": "test_soundwire_devices_still_parse",
    "/sys/class/dmi/id": "test_dmi_still_carries_the_fields_the_report_names",
    "/etc/os-release": "test_os_release_still_identifies_the_distro",
    "/proc/sys/kernel/osrelease": "test_the_kernel_release_still_reads_from_proc",
    "/proc/mounts": "test_proc_mounts_still_lists_a_windows_partition_that_is_mounted",
    "/sys/module": "test_module_and_firmware_trees_are_still_where_they_are_read",
    "/lib/firmware": "test_module_and_firmware_trees_are_still_where_they_are_read",
    "/lib/firmware/updates": "test_module_and_firmware_trees_are_still_where_they_are_read",
    "/proc//stat": "test_pgrep_still_answers_for_a_running_process (process_age)",
    "/proc/uptime": "test_pgrep_still_answers_for_a_running_process (process_age)",
    "/var/lib/flatpak/app": "exempt: an existence check, nothing parsed",
    "/sys/class/firmware-attributes/thinklmi/attributes/MicrophoneAccess":
        "exempt: an existence check, nothing parsed",
    "/etc/modprobe.d/speaker-pin-fix.conf":
        "exempt: named in a command we print, never read",
}


def _host_locations_lib_names() -> set[str]:
    """Every string in lib/ and the entry scripts that starts like a host
    location (`host.HOST_PREFIXES`), docstrings aside; an f-string's literal
    parts are joined, so `f"/proc/{pid}/stat"` reads as `/proc//stat`."""
    import ast
    root = Path(__file__).resolve().parent.parent
    found = set()
    for path in sorted((root / "lib").rglob("*.py")) + sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # lib/host.py's own prefix table names where locations start, not one.
        prefix_table = {id(element) for node in ast.walk(tree)
                        if isinstance(node, ast.Assign)
                        and any(getattr(target, "id", None) == "HOST_PREFIXES"
                                for target in node.targets)
                        for element in ast.walk(node.value)}
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef,
                                           ast.AsyncFunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)
                      and isinstance(node.body[0].value, ast.Constant)}
        joined_parts = {id(v) for node in ast.walk(tree)
                        if isinstance(node, ast.JoinedStr) for v in node.values}
        for node in ast.walk(tree):
            if id(node) in docstrings | joined_parts | prefix_table:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(v.value for v in node.values
                               if isinstance(v, ast.Constant))
            else:
                continue
            if text.startswith(host.HOST_PREFIXES) and " " not in text:
                found.add(text)
    return found


def test_every_host_location_lib_reads_has_a_live_check():
    """As for tools: a new location in lib/ needs a check above, or an entry
    in `_HOST_LOCATIONS` saying why it can't have one."""
    names = _host_locations_lib_names()
    assert {"/proc/asound", "/sys/class/sound", "/etc/os-release"} <= names
    assert sorted(names - set(_HOST_LOCATIONS)) == []
    assert sorted(set(_HOST_LOCATIONS) - names) == [], "stale entries"
