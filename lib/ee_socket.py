"""EasyEffects' local socket: the transport, shared by every caller.

Stdlib-only on purpose, like ``ee_paths.py``: the socket is reached from the
``--doctor`` report and from the end of a generator run, and neither should
pull DSP in to ask a running EasyEffects a question.

EasyEffects' daemon listens on a QLocalServer named ``EasyEffectsServer``
under ``$XDG_RUNTIME_DIR`` (native EE ≥ 8.0.9; ``/tmp`` before, and inside
the sandbox — out of reach — on Flatpak) and answers newline-terminated
ASCII requests —
its documented "Local Server"
(https://wwmm.github.io/easyeffects/user_interface/local_server.html, since
EE 8.0.7; the tags are upstream's src/tags_local_server.hpp). Callers get typed
functions, never a raw request string: ``--doctor`` sends only the two reads,
and the end of a generator run sends one load and reads its receipt. Why the socket and not the ``easyeffects``
CLI, and the version history: docs/design-notes.md, "Rejected approaches".
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

SERVER_NAME = "EasyEffectsServer"   # upstream tags::local_server::server_name
# The two read-only requests. get_last_loaded_preset is on the documented
# page (since 8.0.7); get_global_bypass is source-only (tags_local_server.hpp)
# — what `easyeffects -b 3` itself sends — answers 1/2 with no newline, and
# exists only since 8.1.3: an 8.0.9–8.1.2 daemon answers nothing, which a
# caller must read as unknown, never as "off".
PRESET_REQUEST = "get_last_loaded_preset:output\n"
BYPASS_REQUEST = "get_global_bypass\n"


def _name_accepted(name: str) -> bool:
    """Upstream's load_preset regex, ^load_preset:(input|output):([^\n]{1,100})\n$,
    runs as std::regex over the raw bytes, so its 100 counts UTF-8 bytes —
    a 60-character accented name is over it. A name outside it is dropped by
    the daemon without a word, so refusing it here is what keeps that from
    surfacing as unexplained drift."""
    return "\n" not in name and 1 <= len(name.encode("utf-8")) <= 100


@dataclass
class EEReply:
    """What the daemon said, and how far we got asking.

    Three states, because two of them must look different in the report.
    ``reached`` False is no socket — EasyEffects isn't running, which is
    ordinary, and falling back to its config file quietly is right.
    ``reached`` with ``answered`` False means the daemon is listening but did
    not reply to this request: its protocol moved under us. That must be
    visible, because the alternative is serving a stale config value as
    though it were current for as long as nobody notices.

    ``answered`` with an empty ``value`` is a real answer — over the socket EE
    sends the raw preset name, so "" means no preset is loaded. (Its CLI
    substitutes the string "None" there; the socket does not.)
    """
    value: str = ""
    reached: bool = False
    answered: bool = False


def _socket_path() -> Path | None:
    """``$XDG_RUNTIME_DIR/EasyEffectsServer`` — where every native EE ≥ 8.0.9
    listens — or None in a session without a runtime dir. THE seam: the test
    suite pins it to None so no run in it can reach a real EasyEffects."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime_dir) / SERVER_NAME if runtime_dir else None


def _exchange(request: str, lines: int) -> tuple[list[str], bool, bool]:
    """One connection, one write, then read ``lines`` newline-terminated
    replies (``0``: a single unframed read). Returns (replies, reached,
    answered): not reached is no socket; reached-but-unanswered is a
    listening daemon that fell silent — its protocol moved under us.

    The daemon writes a reply only from the branch matching a request's tag;
    an unrecognised one falls through and writes nothing, so a read that
    times out is the drift signal. Waiting out the timeout is the point —
    not a slow path to avoid.
    """
    path = _socket_path()
    if path is None:
        return [], False, False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(str(path))
            sock.sendall(request.encode())
            if lines == 0:
                data = sock.recv(4096)
                return [data.decode("utf-8", errors="replace").strip()], True, True
            buf = b""
            while buf.count(b"\n") < lines:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except (socket.timeout, TimeoutError):
        return [], True, False
    except OSError:
        return [], False, False
    # Count newlines, not split() elements: a reply cut off right after a
    # newline splits into a trailing "" that would pass as the missing line.
    got = buf.decode("utf-8", errors="replace").split("\n")[:lines]
    return got, True, buf.count(b"\n") >= lines


def last_loaded_output_preset() -> EEReply:
    """The output preset EasyEffects reports as loaded ("" = none)."""
    replies, reached, answered = _exchange(PRESET_REQUEST, lines=1)
    return EEReply(value=replies[0].strip() if answered else "",
                   reached=reached, answered=answered)


def global_bypass() -> EEReply:
    """"1" (bypassed) or "2" (active) — the daemon's own encoding, unframed."""
    replies, reached, answered = _exchange(BYPASS_REQUEST, lines=0)
    return EEReply(value=replies[0] if answered else "",
                   reached=reached, answered=answered)


@dataclass
class LoadResult:
    """How far a load got. ``loaded`` is the preset EasyEffects reports after
    the load, ``kernel`` the convolver's kernel name when one was asked for.

    ``unreachable``: no socket. ``silent``: the daemon took the request and
    answered nothing — drift. ``mismatch``: it answered, but not with our
    preset (``loaded`` unchanged: it could not find the file where it looks;
    ``loaded`` empty: the JSON failed to parse) or not with our kernel.
    """
    outcome: str = "unreachable"   # "unreachable" | "silent" | "loaded" | "mismatch"
    loaded: str = ""
    kernel: str = ""


def load_output_preset(name: str, expect_kernel: str | None = None) -> LoadResult:
    """Ask a running EasyEffects to load one of our output presets, and check
    it took.

    The requests go in ONE write: the daemon drains every complete line of a
    write in a single synchronous pass (upstream local_server.cpp
    onReadyRead) and sets its last-loaded key before parsing, so the reply
    to the second line is the post-load state. ``load_preset`` itself answers
    nothing, on a missing file included — the two reads are the only receipt.
    ``expect_kernel`` adds the convolver's kernel name to the receipt: equal
    to the hashed name we wrote, it proves the new JSON was applied and,
    since the name changed, that the convolver re-read the impulse.
    """
    if not _name_accepted(name):
        raise ValueError(f"preset name the daemon would drop: {name!r}")
    request = f"load_preset:output:{name}\n" + PRESET_REQUEST
    if expect_kernel is not None:
        request += "get_property:output:convolver:0:kernelName\n"
    replies, reached, answered = _exchange(request, lines=2 if expect_kernel is not None else 1)
    if not reached:
        return LoadResult("unreachable")
    if not answered:
        return LoadResult("silent")
    loaded = replies[0].strip()
    kernel = replies[1].strip() if expect_kernel is not None else ""
    ok = loaded == name and (expect_kernel is None or kernel == expect_kernel)
    return LoadResult("loaded" if ok else "mismatch", loaded=loaded, kernel=kernel)
