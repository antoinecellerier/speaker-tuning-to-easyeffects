"""The environment every shell-out in ``lib/`` runs under.

Nothing this project runs is asked to speak to the user in their language:
each command's output is *parsed* — a ``Version:`` label, a ``Candidate:``
line, a decimal point in an ``lv2info`` bound — and gettext translates those
labels along with everything else. Issue #93 was the first sighting: a
``zh_CN.UTF-8`` shell made ``flatpak info`` print ``版本： 8.2.9``, which no
``startswith("version:")`` finds, and an ``apt-cache policy`` under French
prints ``Candidat :`` the same way. Pinning the locale at the subprocess
boundary fixes every parser at once, where a per-parser tolerance would have
to guess each tool's catalogue; ``tests/test_layout.py`` holds every call in
``lib/`` to it.
"""

from __future__ import annotations

import os


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
