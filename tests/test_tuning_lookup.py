"""Which tuning a place holding several kinds is read for, and what the run
says when none binds this machine (`lib/tuning_lookup.py`)."""

from __future__ import annotations

import pytest

import dolby_to_easyeffects
from lib import console, tuning_lookup, tuning_source
from lib.apo import dax2, sonitude
from lib.apo.layer import BoundButUnreadable
from lib.dax import discover


def _args(*argv: str):
    return dolby_to_easyeffects.build_parser(list(argv)).parse_args(
        list(argv))


class _Fake(tuning_lookup._Finder):
    """A finder whose download lookup returns or raises *outcome*."""

    def __init__(self, name: str, outcome):
        self.name, self.outcome = name, outcome

    def in_download(self, tree, download, args):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _first_bound(monkeypatch, *outcomes):
    """The finder name and result `_first_bound` gives for one outcome per
    kind, in `FINDERS` order; "nothing" when *otherwise* answers."""
    monkeypatch.setattr(tuning_lookup, "FINDERS", tuple(
        _Fake(name, outcome) for name, outcome
        in zip(("dax3", "sonitude", "dax2"), outcomes)))
    finder, found = tuning_lookup._first_bound(
        lambda f: f.in_download(None, None, None), lambda: "nothing")
    return found if found == "nothing" else (finder.name, found)


def _miss(text: str) -> tuning_lookup._Miss:
    return tuning_lookup._Miss(RuntimeError(text))


def test_the_most_recent_devices_format_is_read_first():
    assert [type(f) for f in tuning_lookup.FINDERS] == [
        tuning_lookup._DolbyXml, tuning_lookup._Sonitude, tuning_lookup._Dax2]


@pytest.mark.parametrize("outcomes,winner", [
    (("xml", "archive", "inf"), ("dax3", "xml")),
    ((None, "archive", "inf"), ("sonitude", "archive")),
    ((_miss("no xml"), None, "inf"), ("dax2", "inf")),
    ((_miss("no xml"), BoundButUnreadable("unreadable"), "inf"),
     ("dax2", "inf")),
    ((None, None, None), "nothing"),
])
def test_the_first_kind_that_binds_is_read(monkeypatch, outcomes, winner):
    assert _first_bound(monkeypatch, *outcomes) == winner


@pytest.mark.parametrize("outcomes,said", [
    # A tuning that binds this device but can't be read names this
    # device's tuning, so it is said over any kind that binds nothing.
    ((_miss("no xml"), _miss("no archive"), BoundButUnreadable("inf")),
     "inf"),
    ((BoundButUnreadable("xml"), None, BoundButUnreadable("inf")), "xml"),
    # Of two that bind nothing, the first read.
    ((_miss("no xml"), _miss("no archive"), None), "no xml"),
    ((None, _miss("no archive"), _miss("no inf")), "no archive"),
])
def test_what_is_said_when_none_binds(monkeypatch, outcomes, said):
    with pytest.raises(RuntimeError, match=f"^{said}$"):
        _first_bound(monkeypatch, *outcomes)


def test_a_choice_left_to_the_reader_is_said_at_once(monkeypatch):
    """Only "none is this device's" goes on to the next kind."""
    with pytest.raises(ValueError, match="two folders"):
        _first_bound(monkeypatch, ValueError("two folders"), "archive", None)


def test_a_flag_a_named_tuning_cant_take_is_refused_before_it_is_read(
        tmp_path, monkeypatch):
    def unread(*a, **k):
        raise AssertionError("read before its flags were checked")
    monkeypatch.setattr(sonitude, "resolve", unread)
    archive = tmp_path / "SEAPODAT.INTELAUDIO.FUNC_01-VEN_10EC-DEV_0285.zip"
    archive.write_bytes(b"")
    with pytest.raises(tuning_lookup._FlagRefused,
                       match="^--disable mbc acts on a Dolby stage"):
        tuning_lookup._path_source(archive,
                                   _args(str(archive), "--disable", "mbc"))


def test_flags_are_checked_against_the_tuning_used_not_one_passed_over(
        tmp_path, monkeypatch, silence_console):
    """A Sonitude package for other hardware, read before the DAX2 tuning
    this machine binds, has no say in the flags."""
    silence_console(console)
    preset = dax2.Dax2Preset(tmp_path / "x.inf", "HDAUDIO\\X", "P_SPK", {})
    _store(monkeypatch, tmp_path, dax2_outcome=preset)
    monkeypatch.setattr(discover, "autoprobe_dolby_source",
                        lambda also: tmp_path)
    source = tuning_lookup.source_for(_args("--enable", "autogain"), None)
    assert isinstance(source, tuning_source.Dax2)


def _store(monkeypatch, tmp_path, *, xml=None, sonitude_binds=False,
           dax2_outcome=None):
    """A DriverStore holding a Dolby XML for this machine when *xml*, a
    Sonitude package, and *dax2_outcome* as its DAX2 lookup's."""
    monkeypatch.setattr(discover, "holds_dolby_xmls",
                        lambda store: xml is not None)

    def find_xml(*a, **k):
        if xml is None:
            raise FileNotFoundError("no Dolby XML here")
        return xml
    monkeypatch.setattr(discover, "find_tuning_xml", find_xml)
    folder = tmp_path / "snapo64ext.inf_amd64_1"
    monkeypatch.setattr(sonitude, "driver_store_packages",
                        lambda store: [folder])

    def resolve(origin, warn):
        if not sonitude_binds:
            raise RuntimeError("holds Sonitude tunings, but none for this "
                               "device's audio hardware")
        return {"music": object()}
    monkeypatch.setattr(sonitude, "resolve", resolve)

    def dax2_lookup(*a, **k):
        if isinstance(dax2_outcome, Exception):
            raise dax2_outcome
        return dax2_outcome
    monkeypatch.setattr(dax2, "for_this_machine", dax2_lookup)
    return folder



def test_a_dax2_tuning_that_cant_be_read_gives_way_to_a_sonitude_one(
        tmp_path, monkeypatch, silence_console):
    silence_console(console)
    _store(monkeypatch, tmp_path, sonitude_binds=True,
           dax2_outcome=BoundButUnreadable("can't read it"))
    source = tuning_lookup.windows_source(tmp_path, _args())
    assert isinstance(source, tuning_source.Sonitude)


def test_a_dax2_tuning_that_cant_be_read_is_said_over_a_sonitude_miss(
        tmp_path, monkeypatch, silence_console):
    silence_console(console)
    _store(monkeypatch, tmp_path, dax2_outcome=BoundButUnreadable("can't read it"))
    with pytest.raises(BoundButUnreadable, match="can't read it"):
        tuning_lookup.windows_source(tmp_path, _args())


def test_a_dolby_xml_is_read_over_a_sonitude_one_and_names_it(
        tmp_path, monkeypatch, silence_console, capsys):
    silence_console(console)
    xml = tmp_path / "DEV_0285_SUBSYS_103C860F.xml"
    folder = _store(monkeypatch, tmp_path, xml=xml, sonitude_binds=True)
    source = tuning_lookup.windows_source(tmp_path, _args())
    assert isinstance(source, tuning_source.DolbyXml)
    out = capsys.readouterr().out
    assert f"Auto-detected: {xml}" in out
    assert f"A Sonitude tuning is there too ({folder}); using the Dolby " \
        "file." in out


def test_a_sonitude_tuning_is_read_over_a_dax2_one(
        tmp_path, monkeypatch, silence_console):
    silence_console(console)
    preset = dax2.Dax2Preset(tmp_path / "x.inf", "HDAUDIO\\X", "P_SPK", {})
    _store(monkeypatch, tmp_path, sonitude_binds=True, dax2_outcome=preset)
    source = tuning_lookup.windows_source(tmp_path, _args())
    assert isinstance(source, tuning_source.Sonitude)


def test_dolby_files_the_reader_must_pick_among_are_said_at_once(
        tmp_path, monkeypatch, silence_console):
    """A Sonitude tuning that binds doesn't stand in for the choice."""
    silence_console(console)
    _store(monkeypatch, tmp_path, xml=tmp_path / "unused.xml",
           sonitude_binds=True)

    def choice(*a, **k):
        raise discover.TuningChoice("2 internal-speaker tunings match")
    monkeypatch.setattr(discover, "find_tuning_xml", choice)
    with pytest.raises(discover.TuningChoice, match="2 internal-speaker"):
        tuning_lookup.windows_source(tmp_path, _args())


def test_a_missing_inf_is_said_as_missing(tmp_path):
    path = tmp_path / "HDXSSTM2.inf"
    with pytest.raises(FileNotFoundError) as exc:
        tuning_lookup._path_source(path, _args(str(path)))
    assert exc.value.filename == str(path)


def test_a_store_with_no_tuning_says_so_as_the_dolby_lookup_does(
        tmp_path, monkeypatch, silence_console):
    silence_console(console)
    _store(monkeypatch, tmp_path)
    monkeypatch.setattr(sonitude, "driver_store_packages", lambda store: [])
    with pytest.raises(FileNotFoundError, match="no Dolby XML here"):
        tuning_lookup.windows_source(tmp_path, _args())


def test_auto_detection_keeps_a_mount_any_kind_is_in(monkeypatch):
    """`autoprobe_dolby_source`'s *also* hook asks every finder."""
    seen = []

    def probe(also):
        seen.append(also)
        raise FileNotFoundError("stop here")
    monkeypatch.setattr(discover, "autoprobe_dolby_source", probe)
    for name, finder in (("dax3", tuning_lookup._DolbyXml),
                         ("sonitude", tuning_lookup._Sonitude),
                         ("dax2", tuning_lookup._Dax2)):
        monkeypatch.setattr(finder, "present_in_store",
                            lambda self, store, name=name: store == name)
    args = _args()
    with pytest.raises(FileNotFoundError):
        tuning_lookup.source_for(args, None)
    also, = seen
    assert all(also(name) for name in ("dax3", "sonitude", "dax2"))
    assert not also("elsewhere")
