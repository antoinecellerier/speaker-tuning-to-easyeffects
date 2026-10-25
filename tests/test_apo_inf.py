"""Driver `.inf` reading shared by the vendor formats (`lib/apo/inf.py`).

Every `.inf` here is synthetic.
"""

from lib.apo import inf


def _write(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "x.inf"
    if encoding == "utf-16":
        path.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        path.write_text(text, encoding=encoding)
    return path


def test_a_trailing_backslash_continues_the_line(tmp_path):
    path = _write(tmp_path, "[A]\nHKR,Key,Name,,one,\\\n  two ; comment\nnext\n")
    assert inf.read(path)["a"] == ["HKR,Key,Name,,one,two", "next"]


def test_comments_strip_outside_quotes_only(tmp_path):
    path = _write(tmp_path, '[A]\nKey = "a;b" ; gone\n')
    assert inf.read(path)["a"] == ['Key = "a;b"']


def test_utf16_with_a_byte_order_mark_reads(tmp_path):
    path = _write(tmp_path, "[Version]\nDriverVer = 10/08/2021, 6.0.9249.2\n",
                  encoding="utf-16")
    assert inf.driver_version(path) == (6, 0, 9249, 2)


def test_strings_substitute_and_keep_a_comma_value_whole(tmp_path):
    path = _write(tmp_path, '[A]\nHKR,K,%PK%,1,00\n'
                            '[Strings]\nPK = "{abc},3"\n')
    line = inf.read(path)["a"][0]
    assert inf.fields(line) == ["HKR", "K", "{abc},3", "1", "00"]


def test_a_hardware_id_binds_through_a_decorated_model_section(tmp_path):
    path = _write(tmp_path, (
        "[Manufacturer]\n%M% = Models, NTamd64\n"
        "[Models.NTamd64]\n"
        "Other = Install_A, HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC1111\n"
        "Mine = Install_B, HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC11B2\n"
        "[Strings]\nM = \"Maker\"\n"))
    hits = list(inf.bound_installs(inf.read(path),
                                   {"dev_0274", "subsys_10ec11b2"}))
    assert hits == [("HDAUDIO\\FUNC_01&VEN_10EC&DEV_0274&SUBSYS_10EC11B2",
                     "Install_B")]


def test_an_edited_file_is_read_again(tmp_path):
    path = _write(tmp_path, "[A]\none\n")
    assert inf.read(path)["a"] == ["one"]
    path.write_text("[A]\none\ntwo\n")
    assert inf.read(path)["a"] == ["one", "two"]
