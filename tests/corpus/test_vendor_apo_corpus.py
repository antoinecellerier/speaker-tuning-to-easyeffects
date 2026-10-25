"""Vendor APO layers bound to real corpus XMLs.

Walks every discovered XML, not one per distinct file: a layer belongs to the
package beside the XML, so two byte-identical XMLs can differ in what binds
them. Each distinct (XML, config) pair runs once. Skips cleanly where no
vendor config sits beside any discovered XML, which is every corpus that
holds no Surface package.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np
import pytest

from lib.apo import stacked
from lib.apo import layer as apo_layer
from lib.apo import surface
from lib.dax.parse import DB_FIXED_POINT_SCALE, parse_xml
from lib.pipewire import validate
from lib.pipewire.conf import build_chain, emit_links, format_conf
from lib.preset import emit, fir
from lib.preset.build import make_preset
from lib.report import messages
from tests.corpus.test_corpus import DISCOVERED


def _beside_a_vendor_package(xmls: list[Path]) -> list[Path]:
    """The XMLs with a vendor APO `.inf` in a folder beside their package.

    One scan per package parent instead of a finder call per XML:
    collection runs in every xdist worker, over thousands of paths.
    """
    verdict: dict[Path, bool] = {}
    kept = []
    for xml in xmls:
        roots = (xml.parent, xml.parent.parent)
        for root in roots:
            if root not in verdict:
                verdict[root] = bool(surface.infs_below(root))
        if any(verdict[r] for r in roots):
            kept.append(xml)
    return kept


def _bound_pairs() -> list[tuple[Path, object]]:
    seen, pairs = set(), []
    for xml in _beside_a_vendor_package(DISCOVERED):
        try:
            layer = stacked.find_for_xml(xml)
        except apo_layer.UnsupportedApoConfig as exc:
            layer = exc
        if layer is None:
            continue
        cfg = getattr(layer, "config_path", None) or layer.path
        key = (hashlib.sha256(xml.read_bytes()).digest(),
               hashlib.sha256(Path(cfg).read_bytes()).digest()
               if Path(cfg).is_file() else str(cfg))
        if key not in seen:
            seen.add(key)
            pairs.append((xml, layer))
    return pairs


PAIRS = _bound_pairs()


@pytest.mark.skipif(not PAIRS, reason="no vendor APO config beside any "
                    "discovered XML")
@pytest.mark.parametrize("xml_path,layer", PAIRS,
                         ids=[p.name for p, _ in PAIRS])
def test_bound_layer_reads_folds_and_converts(xml_path, layer, tmp_path):
    assert not isinstance(layer, apo_layer.UnsupportedApoConfig), (
        f"a real config binds {xml_path.name} but can't be read: {layer}")
    tuning = parse_xml(xml_path)
    scale = tuning.ieq_amount / 100.0
    ao = np.array(tuning.ao_left) / DB_FIXED_POINT_SCALE
    fft_freqs = np.fft.rfftfreq(fir.FIR_LENGTH, d=1.0 / fir.SAMPLE_RATE)
    for key in messages.VOICING_CURVES.values():
        if key not in tuning.curves:
            continue
        combined = (np.array(tuning.curves[key]) / DB_FIXED_POINT_SCALE
                    * scale + ao)
        taps, _ = fir.make_fir(tuning.freqs, combined,
                               eq_sections=layer.eq_left)
        worst = emit._worst_shape_error(taps, combined, 0.0, tuning.freqs,
                                        fft_freqs, rows=False,
                                        eq_sections=layer.eq_left)
        assert worst <= emit.FIR_VERIFY_OK_DB, (
            f"{xml_path.name} {key}: fold misses its target by {worst:.2f} dB")
    _chain(tuning, layer, tmp_path)


def _chain(tuning, layer, tmp_path):
    """The layer's preset as a PipeWire chain; raises on a misplaced stage."""
    preset, emitted = make_preset(
        "Corpus", tuning.peq_filters, tuning.vol_leveler,
        tuning.dialog_enhancer, tuning.mb_comp, tuning.regulator,
        tuning.freqs, volmax_boost=tuning.volmax_boost,
        enabled={apo_layer.FLAG}, apo=layer)
    assert f"{apo_layer.FLAG}-active" in emitted
    (tmp_path / "Corpus.irs").write_bytes(b"")
    return build_chain(preset, tmp_path)


# What `lv2info` answered, shared by the cases below: a port schema belongs
# to the installed plugin, not to the config under test.
_LV2_SCHEMAS: dict = {}


@pytest.mark.slow
@pytest.mark.live_machine  # lv2info is a host tool
@pytest.mark.skipif(not (PAIRS and shutil.which("lv2info")
                         and shutil.which("spa-json-dump")),
                    reason="no bound vendor config, or no lv2info tooling")
@pytest.mark.parametrize("xml_path,layer", PAIRS,
                         ids=[p.name for p, _ in PAIRS])
def test_bound_layer_conf_passes_lv2info(xml_path, layer, tmp_path):
    """Every real config's stages stay inside LSP's port ranges: crossovers,
    sidechain filters and thresholds, at up to 8 bands per stage."""
    if isinstance(layer, apo_layer.UnsupportedApoConfig):
        pytest.skip("the test above reports the unreadable config")
    chain = _chain(parse_xml(xml_path), layer, tmp_path)
    conf = format_conf(chain.stages, emit_links(chain.stages),
                       node_name="Corpus", node_description="Corpus",
                       warnings=chain.warnings)
    report = validate.run(conf, schemas=_LV2_SCHEMAS)
    assert report.status == validate.CLEAN, (
        f"{xml_path.name}: {report.status} {report.reason}\n"
        + "\n".join(report.errors))
    assert not report.warnings, "\n".join(report.warnings)
