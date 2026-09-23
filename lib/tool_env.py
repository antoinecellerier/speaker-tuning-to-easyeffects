"""The one door every external tool in ``lib/`` goes through.

Two things are decided here and nowhere else.

**The locale.** Nothing this project runs is asked to speak to the user in
their language. Each command's output is *parsed*: a ``Version:`` label, a
``Candidate:`` line, a decimal point in an ``lv2info`` bound. gettext
translates those labels along with everything else. In issue #93 a
``zh_CN.UTF-8`` shell made ``flatpak info`` print ``版本： 8.2.9``, which no
``startswith("version:")`` finds, and an ``apt-cache policy`` under French
prints ``Candidat :`` the same way. Pinning the locale at the
subprocess boundary fixes every parser at once, where a per-parser tolerance
would have to guess each tool's catalogue.

**Whether the machine is asked at all.** With ``ATMOS_NO_LIVE_TOOLS`` set,
``which`` finds nothing and ``run`` raises ``FileNotFoundError``: the machine
looks like one with none of these tools installed. Every caller already
handles that state, and CI runs in it. The test suite sets it for every test
(``tests/conftest.py``), so a result never depends on the audio stack of the
machine running it, and the scripts a test starts as child processes inherit
it, which is the reach a monkeypatch lacks. Tests that need a tool's answer
replace ``run``/``which`` here in-process; a test whose child process needs
one puts an executable of the tool's name in ``ATMOS_FAKE_TOOLS_DIR``, the one
directory the gate still looks in. The few tests that exist to exercise the
real tool opt out with ``@pytest.mark.live_machine``.

``tests/test_layout.py`` holds ``lib/`` and the entry scripts to this module:
no ``subprocess`` call and no ``shutil.which`` of their own.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess

NO_LIVE_TOOLS = "ATMOS_NO_LIVE_TOOLS"
FAKE_TOOLS_DIR = "ATMOS_FAKE_TOOLS_DIR"

# `git describe` answers which version of this tool is running — a question
# about the checkout, not about the machine — so the gate lets it through.
_CHECKOUT_TOOLS = frozenset({"git"})


def _gated(tool: str) -> bool:
    return (bool(os.environ.get(NO_LIVE_TOOLS))
            and os.path.basename(tool) not in _CHECKOUT_TOOLS)


def _stand_in(tool: str) -> str | None:
    """The executable a test left in ``ATMOS_FAKE_TOOLS_DIR`` for ``tool``."""
    root = os.environ.get(FAKE_TOOLS_DIR)
    if not root:
        return None
    path = os.path.join(root, os.path.basename(tool))
    return path if os.access(path, os.X_OK) else None


def c_locale() -> dict[str, str]:
    """The caller's environment with the locale pinned for parsing.

    ``LC_ALL=C.UTF-8`` rather than ``C`` so UTF-8 in paths and sink
    descriptions survives; a system without that locale falls back to ``C``
    inside the child, which is still untranslated. ``LANGUAGE`` is dropped
    rather than overridden: it outranks ``LC_ALL`` for GLib's language list
    and glibc only ignores it under a C locale, so leaving it set is what
    lets a translated ``flatpak info`` back in.
    """
    env = {k: v for k, v in os.environ.items() if k != "LANGUAGE"}
    env["LC_ALL"] = "C.UTF-8"
    return env


def which(name: str) -> str | None:
    """``shutil.which``; while live tools are off, only a stand-in."""
    if _gated(name):
        return _stand_in(name)
    return shutil.which(name)


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` under ``c_locale()``. While live tools are off, a
    stand-in runs in the tool's place, or ``FileNotFoundError`` is raised as
    for a tool that isn't installed. Takes ``subprocess.run``'s keywords
    except ``env``, which is this module's to set."""
    if _gated(argv[0]):
        stand_in = _stand_in(argv[0])
        if stand_in is None:
            raise FileNotFoundError(errno.ENOENT,
                                    f"not run: {NO_LIVE_TOOLS} is set", argv[0])
        argv = [stand_in, *argv[1:]]
    return subprocess.run(argv, env=c_locale(), **kwargs)
