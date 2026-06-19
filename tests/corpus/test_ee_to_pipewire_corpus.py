"""Corpus-driven invariants for ``ee_to_pipewire.py``.

Mirrors ``tests/corpus/test_corpus.py`` but for the PW filter-chain
converter: per discovered XML, run the full pipeline (parse → FIR →
preset → PW conf), assert it doesn't raise, and check the structural
invariants ``test_ee_to_pipewire.py`` exercises only on the synthetic
fixture (every link endpoint resolves; the conf has at least one
stage).

If ``lv2info`` and ``spa-json-dump`` are on PATH, also runs
``tools/measure_pw/validate_conf.py`` against the rendered conf to
catch unknown-port / out-of-range / xm-MUTE-inversion regressions.
Skips that step cleanly if either tool is missing.

Catches "converter crashes on a non-X1-Yoga XML shape" before it
reaches a tester. Reuses the discovery machinery in
``test_corpus.py`` so both corpus tiers run against exactly the same
XML pool.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dolby_to_easyeffects import (
    make_fir,
    make_preset,
    parse_xml,
    save_wav_stereo,
)
from ee_to_pipewire import (
    build_chain,
    emit_links,
    format_conf,
)
from tests.corpus.test_corpus import CORPUS, _skip_if_no_corpus

# This tier runs the full XML→conf pipeline plus an lv2info subprocess for
# every discovered XML (thousands on a populated corpus) — minutes of
# wall-clock. Gated behind `slow`: `pytest --run-slow` or ATMOS_RUN_SLOW=1.
# The fast structural invariants still run by default via
# `tests/test_ee_to_pipewire.py` on the synthetic fixture.
pytestmark = pytest.mark.slow

VALIDATE_CONF_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools" / "measure_pw" / "validate_conf.py"
)


@pytest.mark.parametrize("xml_path", CORPUS, ids=lambda p: p.name)
def test_corpus_xml_runs_through_pw_pipeline(tmp_path, xml_path):
    """Full pipeline: XML → preset → PW conf, plus structural checks
    and (when tooling is available) deterministic schema validation.
    """
    _skip_if_no_corpus()

    try:
        result = parse_xml(xml_path)
    except ValueError as e:
        pytest.skip(f"{xml_path.name}: parser rejected by design: {e}")

    freqs = result.freqs
    curves = result.curves
    ao_left = result.ao_left
    ao_right = result.ao_right
    peq_filters = result.peq_filters
    vol_leveler = result.vol_leveler
    dialog_enhancer = result.dialog_enhancer
    mb_comp = result.mb_comp
    regulator = result.regulator

    if not curves:
        pytest.skip(f"{xml_path.name}: no IEQ curves")

    ieq = next(iter(curves.values()))
    target_l = [(ieq[i] + ao_left[i]) / 16.0 for i in range(20)]
    target_r = [(ieq[i] + ao_right[i]) / 16.0 for i in range(20)]
    fir_l, _ = make_fir(freqs, target_l)
    fir_r, _ = make_fir(freqs, target_r)
    irs = tmp_path / f"{xml_path.stem}.irs"
    save_wav_stereo(irs, fir_l, fir_r)

    preset, _ = make_preset(
        kernel_name=xml_path.stem,
        peq_filters=peq_filters,
        vol_leveler=vol_leveler,
        dialog_enhancer=dialog_enhancer,
        mb_comp=mb_comp,
        regulator=regulator,
        freqs=freqs,
    )

    chain = build_chain(preset, tmp_path, must_exist=True)
    assert chain.stages, (
        f"{xml_path.name}: build_chain emitted no stages "
        f"(warnings: {chain.warnings})"
    )

    links = emit_links(chain.stages)

    # Lifted from test_every_link_endpoint_resolves: every node:port
    # reference in `links` must name an emitted node, otherwise PW
    # silently drops the link.
    node_names = {n["name"] for s in chain.stages for n in s.nodes}
    for link in links:
        out_node = link["output"].split(":", 1)[0]
        in_node = link["input"].split(":", 1)[0]
        assert out_node in node_names, (
            f"{xml_path.name}: link source {out_node!r} not in chain"
        )
        assert in_node in node_names, (
            f"{xml_path.name}: link sink {in_node!r} not in chain"
        )

    conf = format_conf(
        chain.stages, links,
        node_name=f"Corpus_{xml_path.stem}",
        node_description=f"Corpus test: {xml_path.name}",
        warnings=chain.warnings,
    )
    # Sanity checks the synthetic test also makes — these are
    # invariants, not implementation details.
    assert "context.modules = [" in conf
    assert "libpipewire-module-filter-chain" in conf

    if shutil.which("lv2info") and shutil.which("spa-json-dump") \
            and VALIDATE_CONF_SCRIPT.is_file():
        rc = subprocess.run(
            [sys.executable, str(VALIDATE_CONF_SCRIPT), "-", "-q"],
            input=conf, capture_output=True, text=True, timeout=60,
        )
        # 0 = clean, 1 = errors, 2 = setup error (treated as a skip
        # for that XML — e.g. an LV2 URI not installed locally).
        if rc.returncode == 2:
            pytest.skip(
                f"{xml_path.name}: validate_conf setup error "
                f"({(rc.stderr or rc.stdout).strip()})"
            )
        assert rc.returncode == 0, (
            f"{xml_path.name}: validate_conf reported errors:\n"
            f"{(rc.stderr or rc.stdout).strip()}"
        )
