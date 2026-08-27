"""EasyEffects' local socket: the transport, shared by every caller.

Stdlib-only on purpose, like ``ee_paths.py``: the socket is reached from the
``--doctor`` report and from the end of a generator run, and neither should
pull DSP in to ask a running EasyEffects a question.

EasyEffects' daemon listens on a QLocalServer named ``EasyEffectsServer``
under ``$XDG_RUNTIME_DIR`` and answers newline-terminated ASCII requests —
its documented "Local Server"
(https://wwmm.github.io/easyeffects/user_interface/local_server.html, since
EE 8.0.7; the tags are upstream's src/tags_local_server.hpp). Which requests
a caller may send is that caller's decision: ``lib/report/doctor_run.py``
allowlists its two read-only ones. Why the socket and not the ``easyeffects``
CLI, and the version history: docs/design-notes.md, "Rejected approaches".
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


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


def query(request: str) -> EEReply:
    """Send one request to the running EasyEffects daemon and read one reply.

    The seam tests monkeypatch, mirroring `lib/pipewire/checks._pw_dump`.
    When EasyEffects isn't running the socket isn't there — as for a Flatpak
    install, whose socket sits inside the sandbox — and the reply says so.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return EEReply()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(str(Path(runtime_dir) / "EasyEffectsServer"))
            sock.sendall(request.encode())
            # The daemon writes a reply only from the branch matching the
            # request tag; an unrecognised one falls through and writes
            # nothing, so a read that times out is the drift signal. Waiting
            # out the timeout is the point — not a slow path to avoid.
            reply = sock.recv(4096)
    except (socket.timeout, TimeoutError):
        return EEReply(reached=True)
    except OSError:
        return EEReply()
    return EEReply(value=reply.decode("utf-8", errors="replace").strip(),
                   reached=True, answered=True)
