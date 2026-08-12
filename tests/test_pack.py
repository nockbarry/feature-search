"""
test_pack.py — the pack must be smaller, not lossier.

pack.py drops 13 columns from the work queue on the claim that every one is
recoverable from `feature_name` plus `feature_space.yaml`. That claim is the
whole justification for the trim, so it is verified here against every row of
the real catalog rather than asserted in a docstring.

The second thing these tests guard is portability. The acceptance test is the
most valuable file in the pack, and it is worthless if it cannot run where the
pack is unzipped.
"""
import csv
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from expand_catalog import build_entities  # noqa: E402
from pack import (  # noqa: E402
    CORRECTNESS_FILES,
    DISCOVERY_FILES,
    DROPPED,
    QUEUE_KEEP,
    SPEC_FILES,
    build,
    decode,
)

CANDIDATES = os.path.join(ROOT, "candidates.csv")


@pytest.fixture(scope="module")
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def catalog():
    if not os.path.exists(CANDIDATES):
        subprocess.run([sys.executable, "expand_catalog.py", "--level", "L0"],
                       cwd=ROOT, check=True, capture_output=True)
    with open(CANDIDATES, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def packed(tmp_path_factory, catalog):
    out = tmp_path_factory.mktemp("pack")
    rc = build(out=str(out))
    assert rc == 0
    return out


def test_every_dropped_column_round_trips(spec, catalog):
    """
    The load-bearing test. For all 1167 rows, decoding the feature name must
    reproduce each dropped slot column exactly. A single mismatch means the pack
    is lossy and the columns have to go back in.
    """
    ents = {e["id"]: e for e in build_entities(spec)}
    slot_cols = [c for c in DROPPED if c not in ("level", "stage", "tier")]

    mismatches = []
    for row in catalog:
        got = decode(row["feature_name"], spec, ents)
        for col in slot_cols:
            if got[col] != row[col]:
                mismatches.append(f"{row['feature_name']}.{col}: "
                                  f"decoded {got[col]!r} != catalog {row[col]!r}")
    assert not mismatches, "\n".join(mismatches[:20])


def test_constant_columns_really_are_constant(catalog):
    """level/stage/tier are stated once in the manifest; that is only honest if
    they never vary within a rung."""
    for col in ("level", "stage", "tier", "window_type"):
        assert len({r[col] for r in catalog}) == 1, f"{col} varies — it cannot be hoisted"


def test_decode_rejects_malformed_names(spec, catalog):
    ents = {e["id"]: e for e in build_entities(spec)}
    with pytest.raises(ValueError):
        decode("too__few", spec, ents)
    with pytest.raises(ValueError):
        decode("pan__cb_rate__rate__w30d__zzz_bogus", spec, ents)


def test_pack_contains_every_declared_file(packed):
    for name, _ in DISCOVERY_FILES + SPEC_FILES + CORRECTNESS_FILES:
        rel = name if name.startswith("spec/") else os.path.basename(name)
        assert (packed / rel).exists(), f"{name} missing from pack"
    assert (packed / "queue.csv").exists()
    assert (packed / "MANIFEST.md").exists()


def test_discovery_runs_standalone_from_the_pack(packed):
    """
    The agent's first job is binding inputs, so the discovery tooling has to work
    where the pack is unzipped. discover.py resolves both spec/data_requirements.yaml
    and feature_space.yaml relative to itself; the pack flattens one and preserves
    the other, which is exactly the kind of thing that breaks silently.
    """
    r = subprocess.run([sys.executable, "discover.py", "--binding",
                        "spec/binding.example.yaml"],
                       cwd=packed, capture_output=True, text=True)
    assert "DISCOVERY COVERAGE" in r.stdout, r.stdout + r.stderr
    assert r.returncode == 1, "the example binding is meant to report as blocking"


def test_manifest_puts_discovery_before_the_queue(packed):
    """Screening the queue before binding inputs is wasted work; read order
    must say so."""
    text = (packed / "MANIFEST.md").read_text()
    assert text.index("DISCOVERY_CHECKLIST.md") < text.index("queue.csv")
    assert "Do not guess column names" in text


def test_pack_excludes_the_pipeline_and_the_workbook(packed):
    """The pack is what a model reads. The machinery that generates it is not an
    input to the search, and .xlsx is the human surface."""
    for excluded in ("expand_catalog.py", "validate.py", "build_workbook.py",
                     "feature_catalog.xlsx", "candidates.csv", "Makefile"):
        assert not (packed / excluded).exists(), f"{excluded} should not be in the pack"


def test_queue_keeps_exactly_the_declared_columns(packed):
    with open(packed / "queue.csv", newline="") as f:
        reader = csv.DictReader(f)
        assert list(reader.fieldnames) == QUEUE_KEEP
        rows = list(reader)
    assert rows


def test_queue_fill_in_columns_are_empty(packed):
    """The agent's own columns must arrive blank, or it will read stale values as
    findings."""
    with open(packed / "queue.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    for col in ("drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank"):
        assert all(not r[col] for r in rows), f"{col} arrived non-empty"


def test_queue_is_smaller_than_the_catalog(packed):
    assert os.path.getsize(packed / "queue.csv") < os.path.getsize(CANDIDATES) * 0.6


def test_manifest_states_the_hoisted_constants(packed, catalog):
    text = (packed / "MANIFEST.md").read_text()
    for col in ("level", "stage", "tier", "window_type"):
        value = catalog[0][col]
        assert f"`{col}` = `{value}`" in text, f"manifest omits {col}={value}"


def test_manifest_declares_the_schema_gap(packed):
    """A model that guesses warehouse column names produces confident nonsense.
    The pack must say so."""
    text = (packed / "MANIFEST.md").read_text()
    assert "Do not guess column names" in text
    assert "v_label_arrival" in text


def test_acceptance_test_runs_standalone(packed):
    """
    Unzipped anywhere, `pytest test_pit_leakage.py` inside the pack must pass.
    It imports pit_reference, which lives beside it only because the pack
    flattens both into one directory — worth pinning, since the repo layout has
    them a directory apart.
    """
    r = subprocess.run([sys.executable, "-m", "pytest", "test_pit_leakage.py", "-q"],
                       cwd=packed, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
