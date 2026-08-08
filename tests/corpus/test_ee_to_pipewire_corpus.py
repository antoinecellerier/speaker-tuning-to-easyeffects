"""Corpus-driven invariants for ``ee_to_pipewire.py``.

Mirrors ``tests/corpus/test_corpus.py`` but for the PW filter-chain
converter: per discovered XML, run the full pipeline (parse → FIR →
preset → PW conf), assert it doesn't raise, and check the structural
invariants ``test_ee_to_pipewire.py`` exercises only on the synthetic
fixture (every link endpoint resolves; the conf has at least one
stage).

If ``lv2info`` and ``spa-json-dump`` are on PATH, also schema-checks
the rendered conf through ``lib.pipewire.validate.run`` — in process,
with each URI's ``lv2info`` output memoized for the session — to catch
unknown-port / out-of-range / xm-MUTE-inversion regressions. Skips that
step cleanly if either tool is missing.

The memo took the subprocess out of the per-XML path, so one test at
the end runs the command-line front end,
``tools/measure_pw/validate_conf.py``, over a single corpus conf —
otherwise nothing runs that wrapper on a real conf at all.

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

from lib.preset.build import make_preset
from lib.preset.emit import save_wav_stereo
from lib.dax.parse import parse_xml
from lib.preset.fir import make_fir
from lib.pipewire import validate
from lib.pipewire.conf import (
    build_chain,
    emit_links,
    format_conf,
)
from tests.corpus.test_corpus import CORPUS, _skip_if_no_corpus

# This tier runs the full XML→conf pipeline for every discovered XML —
# thousands of FIR builds on a populated corpus. Gated behind `slow`: `pytest
# --run-slow` or ATMOS_RUN_SLOW=1. The fast structural invariants still run by
# default via `tests/test_ee_to_pipewire.py` on the synthetic fixture.
pytestmark = pytest.mark.slow

VALIDATE_CONF_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "tools" / "measure_pw" / "validate_conf.py"
)

# Hoisted out of the test body: whether a CLI is installed cannot change
# between parametrised cases, and asking per case is one PATH walk per XML.
_HAS_LV2_TOOLING = bool(shutil.which("lv2info")
                        and shutil.which("spa-json-dump"))


@pytest.fixture(scope="session")
def lv2_schemas() -> dict:
    """One memo of what `lv2info` answered, for the whole session.

    A port schema is a property of the installed plugin, not of the XML under
    test, so reading it per XML re-execs `lv2info` thousands of times for an
    answer that cannot have changed. `validate.run` takes the dict as an
    argument and fills it, so the converter and the CLI keep validating with a
    fresh one and no process-global cache exists to invalidate.

    Declared here rather than in a `tests/corpus/conftest.py`: `test_corpus.py`
    shares that directory and has no conf to validate. Session scope is per
    xdist worker, so the cost is bounded by (workers × distinct URIs) — a real
    conf references three — rather than by the number of XMLs. Measured over
    the full `--run-slow` walk when this landed: 21 execs, against 8,082 when
    each XML shelled out to the CLI front end.
    """
    return {}


def _render_conf(xml_path: Path, tmp_path: Path) -> tuple[str | None, str]:
    """Full pipeline: XML → preset → PW conf, with its structural invariants
    asserted along the way.

    Returns ``(conf, "")``, or ``(None, reason)`` for an XML this tier does not
    cover — which the parametrised test turns into a skip and the CLI check
    below turns into "try the next one".
    """
    try:
        result = parse_xml(xml_path)
    except ValueError as e:
        return None, f"{xml_path.name}: parser rejected by design: {e}"

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
        return None, f"{xml_path.name}: no IEQ curves"

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
    # A generated preset must translate cleanly: any converter warning here
    # (unknown plugin key, unknown enum label, dropped bands, orphaned
    # output key) means the generator learned to emit something the
    # converter can't express — the exact drift the fast-tier coverage
    # guard pins on synthetic presets, re-checked against real XMLs.
    # Exempted: the autogain validated-history advisory (stable prefix in
    # emit_autogain) — it flags an unvalidated *mapping range* on a fully
    # translated preset (SoundWire leveler amount > 5), not a translation
    # gap. If another legitimate warning class ever fires corpus-wide,
    # extend the exemption pattern-scoped like this instead of deleting
    # the assertion.
    gap_warnings = [w for w in chain.warnings
                    if not w.startswith("autogain: maximum-history")]
    assert not gap_warnings, (
        f"{xml_path.name}: converter warned on a generated preset: "
        f"{gap_warnings}"
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
    return conf, ""


@pytest.mark.parametrize("xml_path", CORPUS, ids=lambda p: p.name)
def test_corpus_xml_runs_through_pw_pipeline(tmp_path, xml_path, lv2_schemas):
    """Full pipeline: XML → preset → PW conf, plus structural checks
    and (when tooling is available) deterministic schema validation.
    """
    _skip_if_no_corpus()

    conf, reason = _render_conf(xml_path, tmp_path)
    if conf is None:
        pytest.skip(reason)

    if not _HAS_LV2_TOOLING:
        return

    report = validate.run(conf, schemas=lv2_schemas)
    # `UNCHECKED` is a skip, not a verdict — the check could not run at all
    # (spa-json-dump missing, or its output not JSON). Nothing else may be
    # routed here: an unexpected exception must fail the test, because
    # turning one into a skip would pass this tier green with thousands of
    # confs never validated.
    if report.status == validate.UNCHECKED:
        pytest.skip(f"{xml_path.name}: conf validation could not run "
                    f"({report.reason})")
    assert report.status == validate.CLEAN, (
        f"{xml_path.name}: conf validation returned {report.status}"
        f"{' — ' + report.reason if report.reason else ''}\n"
        + "\n".join(report.errors)
    )


def test_the_validator_cli_still_validates_a_real_conf(tmp_path):
    """`tools/measure_pw/validate_conf.py`, end to end, on one real conf.

    Every other caller of this check now runs `lib.pipewire.validate` in
    process — the converter, and the walk above once the session memo replaced
    its per-XML subprocess. The wrapper still owns its stdin handling, its
    `sys.path` bootstrap and its 0/1/2 exit-code contract, and none of that is
    reachable from an import. `tests/test_layout.py` proves `--help` starts;
    this proves the whole path works on a conf the converter really produces.

    One XML, not the corpus: what scales with the corpus is the *conf*, and
    the walk above already validates every one of them.
    """
    _skip_if_no_corpus()
    if not _HAS_LV2_TOOLING:
        pytest.skip("lv2info or spa-json-dump not in PATH")

    for xml_path in CORPUS:
        conf, _ = _render_conf(xml_path, tmp_path)
        if conf is not None:
            break
    else:
        pytest.skip("no corpus XML rendered a conf")

    # No `is_file()` guard on the script: a wrapper that moved away should
    # fail this loudly, not turn into "nothing was checked".
    rc = subprocess.run(
        [sys.executable, str(VALIDATE_CONF_SCRIPT), "-", "-q"],
        input=conf, capture_output=True, text=True, timeout=60,
    )
    assert rc.returncode == 0, (
        f"{xml_path.name}: {VALIDATE_CONF_SCRIPT.name} exited "
        f"{rc.returncode} (0 = clean, 1 = errors, 2 = setup error):\n"
        f"{(rc.stderr or rc.stdout).strip()}"
    )
