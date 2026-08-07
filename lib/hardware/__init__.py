"""What audio hardware this machine actually has, read from the running system.

Four probes and no policy beyond picking between candidates: ``codecs`` reads
the ids that identify the controller, ``speakers`` fills the ``SpeakerInfo``
record the hardware reports are rendered from, ``amps`` gathers the evidence
that says whether a smart amplifier came up, and ``sinks`` asks PipeWire which
node the internal speakers sit behind. Nothing here parses a Dolby XML,
designs a filter, or builds a preset.

Deliberately empty of code, like ``lib/__init__.py`` and
``lib/data/__init__.py``: a re-export here would drag every sibling in behind
any single import and make cycles reachable (``tests/test_layout.py``).
Callers import the submodule they want by name (``from lib.hardware import
codecs``) — which is also what keeps a ``monkeypatch.setattr`` on it visible
to every caller.
"""
