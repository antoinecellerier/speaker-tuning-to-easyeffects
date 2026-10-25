"""Windows driver `.inf` reading, shared by the vendor formats a driver
package binds through one: sections, fields, the hardware-ID walk to an
install section, and DriverVer.

Stdlib-only, like `lib/apo/layer.py`.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterator
from pathlib import Path


def read(path: Path) -> dict[str, list[str]]:
    """An .inf as {lowercased section name: its lines}, comments stripped.

    Windows writes .inf files in UTF-16 as often as in ANSI, so the encoding
    follows the byte-order mark. A line ending in `\\` continues on the next.
    `[Strings]` tokens (`%NAME%`) are substituted throughout. Parsed once
    per file version: a Realtek driver's .inf runs to a megabyte, and a run
    asks several questions of it.
    """
    st = Path(path).stat()
    return _read(str(path), st.st_mtime_ns, st.st_size)


@functools.lru_cache(maxsize=32)
def _read(path: str, _mtime_ns: int, _size: int) -> dict[str, list[str]]:
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    sections: dict[str, list[str]] = {}
    current = None
    pending = ""
    for line in text.splitlines():
        line = pending + strip_comment(line).strip()
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        pending = ""
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), [])
        elif current is not None:
            current.append(line)
    strings = {}
    for line in sections.get("strings", []):
        key, sep, value = line.partition("=")
        if sep:
            strings[key.strip().lower()] = value.strip().strip('"')

    def subst(line: str) -> str:
        # A substituted value keeps its quotes when it holds a comma, as the
        # PKEY names do ("{guid},0"), so `fields` still splits it as one.
        def value(m):
            v = strings.get(m.group(1).lower())
            if v is None:
                return m.group(0)
            return f'"{v}"' if "," in v else v
        return re.sub(r"%([^%]+)%", value, line)
    return {name: [subst(l) for l in lines] for name, lines in sections.items()}


def strip_comment(line: str) -> str:
    """The line up to its first `;` outside double quotes."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def fields(value: str) -> list[str]:
    """Comma-separated .inf fields, unquoted; a quoted comma stays inside."""
    out, current, quoted = [], [], False
    for ch in value:
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    out.append("".join(current).strip())
    return out


def bound_installs(sections: dict[str, list[str]], parts: set[str]
                   ) -> Iterator[tuple[str, str]]:
    """(hardware ID, install section) for each model entry binding a device.

    *parts* are lowercased hardware-ID parts, such as `dev_0274` and
    `subsys_10ec11b2`; a hardware ID binds the device when it carries them
    all. Walks [Manufacturer] → its model sections, decorated ones included
    → each entry's install section, in file order.
    """
    for line in sections.get("manufacturer", []):
        _, _, value = line.partition("=")
        base, *decorations = fields(value)
        for model in [base] + [f"{base}.{d}" for d in decorations]:
            for entry in sections.get(model.lower(), []):
                _, _, rhs = entry.partition("=")
                install, *hwids = fields(rhs)
                hwid = next((h for h in hwids if parts <= set(
                    re.split(r"[\\&]", h.lower()))), None)
                if hwid:
                    yield hwid, install


def directive(sections: dict[str, list[str]], section: str, key: str
              ) -> list[str]:
    """Every value a section's *key* lines list, e.g. its AddReg sections."""
    values = []
    for line in sections.get(section, []):
        k, _, value = line.partition("=")
        if k.strip().lower() == key:
            values += [v for v in fields(value) if v]
    return values


def driver_version(inf: Path) -> tuple[int, ...]:
    """[Version] DriverVer's version as a sortable tuple, () when absent."""
    for line in read(inf).get("version", []):
        key, _, value = line.partition("=")
        if key.strip().lower() == "driverver":
            ver = fields(value)[-1]
            return tuple(int(p) for p in re.findall(r"\d+", ver))
    return ()
