"""
test_determinism.py — the catalog is a build artifact; it must be reproducible.

If regeneration reorders rows, every future diff becomes unreadable and the
status/drop_reason columns the agent filled in can no longer be matched back to
their features. Dict iteration is insertion-ordered in CPython, but nothing
enforces that a future edit does not introduce a set() mid-pipeline. This does.
"""
import csv
import io
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from expand_catalog import expand  # noqa: E402


@pytest.fixture(scope="module")
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


def test_same_spec_yields_identical_rows(spec):
    a, _ = expand(spec, max_tier=1, level="L0")
    b, _ = expand(spec, max_tier=1, level="L0")
    assert a == b


def test_row_order_is_stable(spec):
    a, _ = expand(spec, max_tier=2, stages_bc=True, level="L1")
    b, _ = expand(spec, max_tier=2, stages_bc=True, level="L1")
    assert [r["feature_name"] for r in a] == [r["feature_name"] for r in b]


def test_cli_output_is_byte_identical(tmp_path):
    """End-to-end: two CLI runs must produce identical files."""
    outs = []
    for i in (1, 2):
        p = tmp_path / f"c{i}.csv"
        subprocess.run([sys.executable, "expand_catalog.py", "--level", "L0",
                        "--out", str(p)], cwd=ROOT, check=True,
                       capture_output=True)
        outs.append(p.read_bytes())
    assert outs[0] == outs[1]


def test_column_set_is_frozen(tmp_path):
    """
    Downstream consumers (the workbook builder, the agent's fills) bind to these
    names. Adding a column is fine; renaming or dropping one is a breaking change
    that must be a deliberate edit to this list.
    """
    p = tmp_path / "c.csv"
    subprocess.run([sys.executable, "expand_catalog.py", "--level", "L0",
                    "--out", str(p)], cwd=ROOT, check=True, capture_output=True)
    cols = next(csv.reader(io.StringIO(p.read_text())))
    expected = {
        "feature_name", "tier", "stage", "level", "entity", "entity_id",
        "entity_class", "measure", "measure_family", "aggregation", "window",
        "window_type", "filter", "transform", "availability", "leakage_risk",
        "min_support_n", "adversarial_halflife", "hypothesis", "status",
        "drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank",
    }
    missing = expected - set(cols)
    assert not missing, f"breaking change: columns removed {missing}"


def test_agent_fill_columns_start_empty(spec):
    """
    status is the only pre-populated fill column, and it must start as
    'candidate' so the definition-of-done check ('no row still says candidate')
    is meaningful.
    """
    rows, _ = expand(spec, max_tier=1, level="L0")
    assert {r["status"] for r in rows} == {"candidate"}
    for r in rows:
        for k in ("drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank"):
            assert r[k] == ""


def test_survivor_filter_is_respected(spec):
    """
    Stage B/C must expand only the survivors handed to it. Passing a single
    survivor may yield zero rows (if no priority-1 transform is compatible with
    that particular slot combination), but it must never exceed the unfiltered
    expansion, and it must never emit a variant of a non-survivor.
    """
    base, _ = expand(spec, max_tier=1, level="L1")
    keep = {base[0]["feature_name"]}
    all_bc, _ = expand(spec, max_tier=1, stages_bc=True, level="L1")
    few, _ = expand(spec, max_tier=1, stages_bc=True, level="L1", survivors=keep)
    assert len(few) < len(all_bc)
    for r in few:
        if r["stage"] != "A_base":
            assert r["feature_name"].startswith(base[0]["feature_name"])
