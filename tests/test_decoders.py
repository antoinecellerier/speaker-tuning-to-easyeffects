"""Q15-coefficient decoders + parse-time XML warning hooks.

The Dolby tuning ships exponential-smoothing time constants and gain
coefficients as Q15 fixed-point. The decode formulas are closed-form,
but the tests pin numeric reference values rather than re-deriving the
formula — that way a refactor changing the formula can't silently stay
green by replicating the same change in both production and test.

The warning-hook tests at the bottom guard the "watching-only" XML
fields where the corpus is constant today (peak-level≈0,
ieq-bands-set=ieq_balanced); we want a clear user-facing prompt if a
future device breaks the assumption.
"""

import math
import xml.etree.ElementTree as ET

import pytest

import dolby_to_easyeffects
from dolby_to_easyeffects import (
    decode_mbc_time_constant,
    make_multiband_compressor,
    parse_xml,
    resolve_channel_or_direct,
    collect_unmodeled_features,
)
from tests.conftest import SYNTHETIC_FREQS_20, synthetic_mb_comp


# Reference values computed offline from the documented spec
#   tau_ms = -1000 / ((fs/block_size) * ln(coeff/32768))
# Pin concrete numbers so a refactor that *changes* the formula (rather
# than re-expressing it equivalently) cannot quietly stay green by
# replicating the change in both production and test.
@pytest.mark.parametrize("coeff,block_size,expected_ms", [
    (16384, 256, 7.694374),    # ~half — moderate smoothing
    (32000, 256, 224.878348),  # near-unity — slow smoothing (long tau)
    (1000, 256, 1.528416),     # tiny — fast smoothing (short tau)
    (16384, 128, 3.847187),    # different block size halves the time
])
def test_decode_mbc_time_constant_reference_values(coeff, block_size, expected_ms):
    got_ms = decode_mbc_time_constant(coeff, block_size=block_size)
    assert got_ms == pytest.approx(expected_ms, abs=1e-5)


def test_decode_mbc_time_constant_clamps_invalid():
    """coeff at the bounds where one_minus_alpha is 0 or >=1 should
    fall back rather than blow up — this matches the production guard.
    """
    assert decode_mbc_time_constant(0) == 100.0
    assert decode_mbc_time_constant(32768) == 100.0
    assert decode_mbc_time_constant(40000) == 100.0


def test_decode_mbc_time_constant_monotone_in_coeff():
    """Larger coeff (closer to 32768) → slower smoothing → longer tau."""
    taus = [decode_mbc_time_constant(c) for c in (1000, 5000, 16384, 30000, 32500)]
    assert taus == sorted(taus)


def test_decode_mbc_time_constant_returns_finite_for_realistic_range():
    """All coefficients in the corpus span ~10 to ~32700; results
    must be finite, positive, and within ~0.1 ms to 10 s.
    """
    for coeff in range(100, 32700, 500):
        ms = decode_mbc_time_constant(coeff)
        assert math.isfinite(ms)
        assert 0.05 < ms < 10000.0


@pytest.mark.parametrize("gain_raw,expected_ratio", [
    (32767, 1.0),       # ~unity Q15 → 1:1 (no compression)
    (32000, 1.024),     # 32768/32000
    (16384, 2.0),       # exactly half Q15 → 2:1
    (8192, 4.0),        # quarter Q15 → 4:1
])
def test_make_multiband_compressor_decodes_ratio_from_q15(gain_raw, expected_ratio):
    """The Q15 gain coefficient inside an mb-comp band group must
    produce the expected compression ratio in the emitted preset.

    Tests through make_multiband_compressor (the only production caller)
    rather than re-implementing the formula in the test.
    """
    # Single-band MBC: only band0 is active, so band0.ratio reflects gain_raw.
    mb = synthetic_mb_comp(group_count=1, bands=[
        # (xover_idx, threshold_q4, gain_raw, attack_q15, release_q15, makeup_q4)
        (20, -160, gain_raw, 30000, 32500, 0),
    ])
    out = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    assert out["band0"]["ratio"] == pytest.approx(expected_ratio, abs=0.01)


def test_make_multiband_compressor_clamps_extreme_ratio():
    """gain_raw very near zero would explode the inverse; production
    clamps to 100:1 as a practical maximum.
    """
    mb = synthetic_mb_comp(group_count=1, bands=[
        (20, -160, 100, 30000, 32500, 0),  # gain_frac < 0.01 → clamp branch
    ])
    out = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    assert out["band0"]["ratio"] == 100.0


def test_make_multiband_compressor_warns_on_out_of_range_coeffs(capsys):
    """A silent value-replacing fallback (clamped ratio, fallback time
    constant) must surface in the log naming the band and raw coeff —
    the fallback *value* is kept, only a warning is added. The warning
    fires in the builder/emit path (make_multiband_compressor), once per
    band, not in main()'s diagnostics re-decode.

    band0: out-of-range gain → ratio clamp warning.
    band1: out-of-range attack + release coeffs → two time-constant warnings.
    band2: all coeffs in range → no warning.
    """
    mb = synthetic_mb_comp(group_count=3, bands=[
        # gain_frac < 0.01 → ratio clamp; attack/release in range (no time warn)
        (7, -160, 100, 30000, 32500, 0),
        # attack=0 and release=32768 are both out of range → two time warnings;
        # gain in range so no ratio warning here
        (14, -160, 16384, 0, 32768, 0),
        # everything in range → silent
        (20, -160, 16384, 30000, 32500, 0),
    ])
    out = make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    err = capsys.readouterr().out

    # Fallback values are unchanged.
    assert out["band0"]["ratio"] == 100.0
    assert out["band1"]["attack-time"] == 100.0
    assert out["band1"]["release-time"] == 100.0

    # Warnings name the affected band and the raw coeff.
    assert "MBC band 0 gain coeff 100" in err
    assert "MBC band 1 attack coeff 0" in err
    assert "MBC band 1 release coeff 32768" in err

    # The in-range band is silent, and each warning fires exactly once.
    assert "MBC band 2" not in err
    assert err.count("out of range") == 3


def test_make_multiband_compressor_no_warn_on_normal_coeffs(capsys):
    """In-range coefficients (the common corpus case) print no fallback
    warning — the warning is reserved for genuine out-of-range guards.
    """
    mb = synthetic_mb_comp(group_count=1, bands=[
        (20, -160, 16384, 30000, 32500, 0),
    ])
    make_multiband_compressor(mb, SYNTHETIC_FREQS_20)
    assert "out of range" not in capsys.readouterr().out


# --- warn_unmodeled_features: watching-only XML fields ---

def _capture_warnings(profile_xml: str) -> list[str]:
    """Messages collect_unmodeled_features returns for a synthetic profile.

    These used to be captured by monkeypatching cprint, back when the
    collector printed as it went. It returns them now — main() prints them
    once at the end of the run instead of burying them mid-parse — so the
    tests just read the return value.
    """
    return collect_unmodeled_features(ET.fromstring(profile_xml))


def test_warn_silent_on_default_values():
    """The 17AA22E6 dynamic profile shape: peak-level=0, preset=ieq_balanced,
    no DSO, no advanced-virt — should print nothing.
    """
    out = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="0"/>
            <ieq-bands-set preset="ieq_balanced"/>
          </tuning-cp>
        </profile>
    """)
    assert out == []


def test_warn_peak_level_nonzero_fires_with_db_conversion():
    """value=-3 → −3/16 ≈ −0.19 dB at the standard convention; the
    warning should surface both the raw value, the dB conversion, and
    the report URL.
    """
    out = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <peak-level value="-3"/>
          </tuning-cp>
        </profile>
    """)
    assert len(out) == 1
    msg = out[0]
    assert "peak-level=-3" in msg
    assert "-0.19 dB" in msg
    assert "github.com/antoinecellerier" in msg


def test_warn_ieq_bands_set_balanced_does_not_fire():
    """Default (or absent) preset='ieq_balanced' is the corpus-wide
    constant — no warning expected.
    """
    out = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set preset="ieq_balanced"/>
          </tuning-cp>
        </profile>
    """)
    assert out == []
    out_no_attr = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set/>
          </tuning-cp>
        </profile>
    """)
    assert out_no_attr == []


def test_warn_ieq_bands_set_unusual_preset_fires():
    """If the XML names anything other than ieq_balanced, surface it
    so the user can pick the matching variant and self-report.
    """
    out = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <ieq-bands-set preset="ieq_warm"/>
          </tuning-cp>
        </profile>
    """)
    assert len(out) == 1
    msg = out[0]
    assert "ieq_warm" in msg
    assert "github.com/antoinecellerier" in msg


def test_warn_existing_unmodeled_features_still_fire():
    """Regression guard for the original two warnings — the lambda-based
    refactor of _UNMODELED_FEATURES must not have silenced them.
    """
    out = _capture_warnings("""
        <profile type="dynamic">
          <tuning-cp>
            <dynamic_speaker_optimization_enable value="1"/>
            <advanced-speaker-virtualizer-rendering-config/>
          </tuning-cp>
        </profile>
    """)
    assert any("Dynamic Speaker Optimization" in m for m in out)
    assert any("advanced speaker virtualizer" in m for m in out)


class TestIntAttr:
    """_int_attr() — safe int read of a ``value=`` attribute.

    Centralises the ``int(el.get("value"))`` idiom so a missing element or a
    present-but-blank/absent attribute degrades to a default instead of raising
    AttributeError/TypeError (neither caught by the CLI handler), while
    genuinely non-integer data still raises a clean ValueError (which is).
    """

    def test_missing_element_returns_default(self):
        assert dolby_to_easyeffects._int_attr(None, default=7) == 7

    def test_missing_element_defaults_to_none(self):
        assert dolby_to_easyeffects._int_attr(None) is None

    def test_absent_attribute_returns_default(self):
        el = ET.fromstring("<foo/>")
        assert dolby_to_easyeffects._int_attr(el, default=-5) == -5

    def test_empty_attribute_returns_default(self):
        el = ET.fromstring('<foo value=""/>')
        assert dolby_to_easyeffects._int_attr(el, default=3) == 3

    def test_normal_value_parsed(self):
        el = ET.fromstring('<foo value="42"/>')
        assert dolby_to_easyeffects._int_attr(el, default=0) == 42

    def test_negative_value_parsed(self):
        # The raw 1/16-dB integers the schema uses are frequently negative.
        el = ET.fromstring('<foo value="-320"/>')
        assert dolby_to_easyeffects._int_attr(el) == -320

    def test_garbage_value_raises_valueerror(self):
        el = ET.fromstring('<foo value="not-an-int"/>')
        with pytest.raises(ValueError):
            dolby_to_easyeffects._int_attr(el)

    def test_custom_attribute_name(self):
        el = ET.fromstring('<foo count="9"/>')
        assert dolby_to_easyeffects._int_attr(el, default=0, name="count") == 9


class TestParseXmlGuards:
    """parse_xml() — required-element guards (finding R1).

    A schema variant or truncated XML that is missing ``band_20_freq``,
    ``tuning-vlldp``, or ``audio-optimizer-bands`` must fail with a clean,
    actionable ``ValueError`` (which the CLI handler catches and prints),
    not a bare ``AttributeError`` from an unguarded ``.find(...).get(...)``
    chain (which escapes as a traceback). ``pytest.raises(ValueError)``
    also fails if the old AttributeError behavior regresses.
    """

    def _write(self, tmp_path, body):
        p = tmp_path / "variant.xml"
        p.write_text(body)
        return p

    def test_missing_band_20_freq_raises_valueerror(self, tmp_path):
        # <constant> present but no <band_20_freq> child.
        p = self._write(tmp_path, "<root><constant/></root>")
        with pytest.raises(ValueError, match="band_20_freq"):
            parse_xml(p)

    def test_missing_tuning_vlldp_raises_valueerror(self, tmp_path):
        # Reaches the profile, but the profile has no <tuning-vlldp>.
        p = self._write(tmp_path, """
            <root>
              <constant><band_20_freq fs_48000="1,2,3,4,5"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="default"/>
              </endpoint>
            </root>
        """)
        with pytest.raises(ValueError, match="tuning-vlldp"):
            parse_xml(p)

    def test_missing_audio_optimizer_bands_raises_valueerror(self, tmp_path):
        # <tuning-vlldp> present but no <audio-optimizer-bands> child.
        p = self._write(tmp_path, """
            <root>
              <constant><band_20_freq fs_48000="1,2,3,4,5"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="default"><tuning-vlldp/></profile>
              </endpoint>
            </root>
        """)
        with pytest.raises(ValueError, match="audio-optimizer-bands"):
            parse_xml(p)

    def test_band_group_wrong_length_raises_valueerror(self, tmp_path):
        # MBC enabled with a band_group_0 carrying 5 ints instead of the
        # 6 the per-band decode unpacks (xover, threshold, ratio, attack,
        # release, makeup). Validated at parse time so the failure names
        # the offending band instead of a bare "not enough values to
        # unpack" from decode_band.
        p = self._write(tmp_path, """
            <root>
              <constant><band_20_freq fs_48000="1,2,3,4,5"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="default">
                  <tuning-vlldp>
                    <audio-optimizer-bands>
                      <ch_00 value="0,0,0,0,0"/>
                      <ch_01 value="0,0,0,0,0"/>
                    </audio-optimizer-bands>
                    <mb-compressor-enable value="1"/>
                    <mb-compressor-tuning>
                      <band_group_0 value="1,2,3,4,5"/>
                    </mb-compressor-tuning>
                  </tuning-vlldp>
                </profile>
              </endpoint>
            </root>
        """)
        with pytest.raises(ValueError, match="band_group_0 has 5 values"):
            parse_xml(p)

    def test_regulator_threshold_length_mismatch_raises_valueerror(self, tmp_path):
        # Regulator enabled with a threshold_high whose length (3) differs
        # from the band grid (5 freqs). make_regulator indexes freqs[] at
        # positions derived from threshold_high, so a mismatch would
        # IndexError deep in the zone loop. Guarded at parse time (finding
        # R6) so the failure names both lengths instead of escaping as an
        # opaque IndexError.
        p = self._write(tmp_path, """
            <root>
              <constant><band_20_freq fs_48000="1,2,3,4,5"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="default">
                  <tuning-vlldp>
                    <audio-optimizer-bands>
                      <ch_00 value="0,0,0,0,0"/>
                      <ch_01 value="0,0,0,0,0"/>
                    </audio-optimizer-bands>
                    <regulator-speaker-dist-enable value="1"/>
                    <regulator-tuning>
                      <threshold_high value="-96,-96,-96"/>
                    </regulator-tuning>
                  </tuning-vlldp>
                </profile>
              </endpoint>
            </root>
        """)
        with pytest.raises(ValueError, match=r"threshold_high has 3 values but the band grid has 5"):
            parse_xml(p)

    def test_malformed_peq_filter_row_warns_and_skips(self, tmp_path, capsys):
        # One valid <filter> row and one malformed row (missing f0). The
        # malformed row must warn-and-skip — matching the loop's existing
        # unknown-type skip (finding R9) — instead of crashing the whole
        # run with an uncaught TypeError, so the valid row still survives.
        p = self._write(tmp_path, """
            <root>
              <constant><band_20_freq fs_48000="1,2,3,4,5"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="default">
                  <tuning-vlldp>
                    <audio-optimizer-bands>
                      <ch_00 value="0,0,0,0,0"/>
                      <ch_01 value="0,0,0,0,0"/>
                    </audio-optimizer-bands>
                    <speaker-peq-filters>
                      <filter speaker="0" type="1" f0="1000" gain="3" q="1.0"/>
                      <filter speaker="0" type="1" gain="3" q="1.0"/>
                    </speaker-peq-filters>
                  </tuning-vlldp>
                </profile>
              </endpoint>
            </root>
        """)
        result = parse_xml(p)
        # Valid row survives, malformed row dropped.
        assert len(result.peq_filters) == 1
        assert result.peq_filters[0]["f0"] == 1000.0
        # Warn-and-skip emitted a message naming the problem.
        assert "skipping" in capsys.readouterr().out


class TestSimplifiedSchema:
    """parse_xml() — simplified-schema DAX3 support (issue #22).

    Older Lenovo drivers (xml_version ~3.2.x; e.g. ThinkPad X1 Carbon Gen 8)
    ship a simplified DAX3 schema: the per-channel audio-optimizer correction
    lives under <gain_l>/<gain_r> (a 10-channel surround layout) instead of
    <ch_00>..<ch_07>, and the MBC and speaker-PEQ blocks are omitted entirely.
    The regulator block is unchanged (still one threshold per band). parse_xml
    maps gain_l→left / gain_r→right onto the same audio-optimizer slots as
    ch_00/ch_01 — they share the identical value=/preset= encoding — so these
    XMLs produce a convolver (+ regulator) preset instead of being rejected.
    """

    def _write(self, tmp_path, body):
        p = tmp_path / "simplified.xml"
        p.write_text(body)
        return p

    # A 5-band simplified profile: gain_l/gain_r in place of ch_00/ch_01, the
    # surround channels alongside (ignored for a 2-channel speaker), a matching
    # 5-value regulator, and no MBC or speaker-PEQ blocks.
    _SIMPLIFIED = """
        <root>
          <constant><band_20_freq fs_48000="100,200,400,800,1600"/></constant>
          <endpoint type="internal_speaker" operating_mode="normal">
            <profile type="dynamic">
              <tuning-vlldp>
                <audio-optimizer-bands>
                  <gain_l value="10,20,-30,0,5"/>
                  <gain_r value="-5,0,30,-20,-10"/>
                  <gain_c value="0,0,0,0,0"/>
                </audio-optimizer-bands>
                <regulator-speaker-dist-enable value="1"/>
                <regulator-tuning>
                  <threshold_high value="-96,-80,-64,-48,0"/>
                </regulator-tuning>
              </tuning-vlldp>
            </profile>
          </endpoint>
        </root>
    """

    def test_gain_l_r_accepted_as_audio_optimizer(self, tmp_path, capsys):
        result = parse_xml(self._write(tmp_path, self._SIMPLIFIED))
        # gain_l → left, gain_r → right, decoded to the same raw-int form
        # ch_00/ch_01 produce (resolve_xml_value + parse_csv_ints).
        assert result.ao_left == [10, 20, -30, 0, 5]
        assert result.ao_right == [-5, 0, 30, -20, -10]
        # The simplified variant has no MBC and no speaker PEQ.
        assert result.mb_comp is None
        assert result.peq_filters == []
        # A one-line notice tells the user which schema path fired.
        assert "simplified" in capsys.readouterr().out

    def test_regulator_maps_without_adaptation(self, tmp_path):
        # The simplified regulator is still one threshold per band, so the
        # existing make_regulator path applies unchanged — no special-casing.
        result = parse_xml(self._write(tmp_path, self._SIMPLIFIED))
        assert result.regulator is not None
        assert len(result.regulator["threshold_high"]) == 5

    def test_audio_optimizer_without_ch_or_gain_still_raises(self, tmp_path):
        # audio-optimizer-bands present but carrying neither ch_00/ch_01 nor
        # gain_l/gain_r (only a surround channel) is genuinely unsupported and
        # must still fail with a clean, named ValueError.
        body = """
            <root>
              <constant><band_20_freq fs_48000="100,200,400,800,1600"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="dynamic">
                  <tuning-vlldp>
                    <audio-optimizer-bands>
                      <gain_c value="0,0,0,0,0"/>
                    </audio-optimizer-bands>
                  </tuning-vlldp>
                </profile>
              </endpoint>
            </root>
        """
        with pytest.raises(ValueError, match="neither ch_00/ch_01 nor"):
            parse_xml(self._write(tmp_path, body))


class TestResolveChannelOrDirect:
    """resolve_channel_or_direct() — direct value/preset vs per-channel ch_00.

    Older/flat regulator tunings carry the CSV array directly on
    threshold_high/low; the newer SoundWire schema nests it under
    <ch_00>..<ch_07>. The helper reads the direct form when present, else
    ch_00 (through the same value=/preset= mechanism), else "".
    """

    _CONST = ET.fromstring('<constant><arr target="9,9,9"/></constant>')

    def test_direct_value(self):
        el = ET.fromstring('<threshold_high value="1,2,3"/>')
        assert resolve_channel_or_direct(el, None) == "1,2,3"

    def test_direct_preset(self):
        el = ET.fromstring('<threshold_high preset="arr"/>')
        assert resolve_channel_or_direct(el, self._CONST) == "9,9,9"

    def test_ch_00_value(self):
        el = ET.fromstring(
            '<threshold_high><ch_00 value="4,5,6"/><ch_01 value="4,5,6"/></threshold_high>')
        assert resolve_channel_or_direct(el, None) == "4,5,6"

    def test_ch_00_preset(self):
        el = ET.fromstring('<threshold_high><ch_00 preset="arr"/></threshold_high>')
        assert resolve_channel_or_direct(el, self._CONST) == "9,9,9"

    def test_direct_value_wins_over_children(self):
        # A direct value= takes precedence over any ch_00 child.
        el = ET.fromstring(
            '<threshold_high value="1,2,3"><ch_00 value="4,5,6"/></threshold_high>')
        assert resolve_channel_or_direct(el, None) == "1,2,3"

    def test_empty_returns_blank(self):
        assert resolve_channel_or_direct(ET.fromstring("<threshold_high/>"), None) == ""

    def test_none_returns_blank(self):
        assert resolve_channel_or_direct(None, None) == ""


class TestRegulatorPerChannelSchema:
    """parse_xml() — newer SoundWire per-channel regulator thresholds.

    The newer SoundWire schema (e.g. SUBSYS_37A317AA) nests threshold_high /
    threshold_low under per-channel <ch_00>..<ch_07> elements instead of a
    direct value=/preset=. Before the fix resolve_xml_value returned "" and the
    regulator silently fell back to [0.0]*N (no per-band limiting). parse_xml
    now reads ch_00 and warns on L/R divergence or a genuinely empty tuning.
    """

    def _write(self, tmp_path, body):
        p = tmp_path / "perchan.xml"
        p.write_text(body)
        return p

    def _xml(self, threshold_high_inner):
        return f"""
            <root>
              <constant><band_20_freq fs_48000="100,200,400,800,1600"/></constant>
              <endpoint type="internal_speaker" operating_mode="normal">
                <profile type="dynamic">
                  <tuning-vlldp>
                    <audio-optimizer-bands>
                      <ch_00 value="0,0,0,0,0"/>
                      <ch_01 value="0,0,0,0,0"/>
                    </audio-optimizer-bands>
                    <regulator-speaker-dist-enable value="1"/>
                    <regulator-tuning>
                      <threshold_high>{threshold_high_inner}</threshold_high>
                    </regulator-tuning>
                  </tuning-vlldp>
                </profile>
              </endpoint>
            </root>
        """

    def test_reads_ch_00_not_zero_fallback(self, tmp_path):
        body = self._xml(
            '<ch_00 value="-96,-80,-64,-48,0"/>'
            '<ch_01 value="-96,-80,-64,-48,0"/>'
            '<ch_02 value="0,0,0,0,0"/>')
        result = parse_xml(self._write(tmp_path, body))
        # -96,-80,-64,-48,0 in 1/16-dB units → -6,-5,-4,-3,0 dB — real
        # limiting, not the [0.0]*5 no-op fallback.
        assert result.regulator["threshold_high"] == [-6.0, -5.0, -4.0, -3.0, 0.0]

    def test_ch_01_divergence_warns_and_uses_ch_00(self, tmp_path, capsys):
        body = self._xml(
            '<ch_00 value="-96,-80,-64,-48,0"/>'
            '<ch_01 value="-96,-80,-64,-48,16"/>')  # last band differs
        result = parse_xml(self._write(tmp_path, body))
        out = capsys.readouterr().out
        assert "asymmetric" in out
        # ch_00 is the stereo-limiter reference despite the divergence.
        assert result.regulator["threshold_high"] == [-6.0, -5.0, -4.0, -3.0, 0.0]

    def test_empty_threshold_high_warns_and_falls_back(self, tmp_path, capsys):
        # threshold_high present but carrying no value/preset/ch_00 → no
        # limiting, but the run now says so instead of silently disabling.
        result = parse_xml(self._write(tmp_path, self._xml("")))
        out = capsys.readouterr().out
        assert "no per-band limiting" in out
        assert result.regulator["threshold_high"] == [0.0] * 5
