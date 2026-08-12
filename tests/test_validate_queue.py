"""
test_validate_queue.py — the output guard must catch fabrication AND pass honest work.

Both halves matter equally. A validator that rejects everything gets switched
off within a day, and then the fabrication it was built to catch ships anyway.
So there are two anchor tests here: a queue with planted defects must be
rejected item by item, and a large honest queue must pass under --strict with
zero warnings.

The threat model is specific. This package is handed to a planner that dispatches
fast sub-agents over 1,310 candidates. Such a worker, told to fill `iv`, fills
`iv`. Nothing about the number looks wrong. So the checks are structural rather
than statistical: they do not ask whether a value is plausible, they ask whether
the evidence a row claims actually exists. That question is decidable.
"""
import copy
import csv
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from validate_queue import check, load_statuses  # noqa: E402

COLS = ["feature_name", "status", "drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank"]


@pytest.fixture(scope="module")
def statuses():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return load_statuses(yaml.safe_load(f))


def row(name, status="candidate", **kw):
    r = dict.fromkeys(COLS, "")
    r["feature_name"] = name
    r["status"] = status
    r.update(kw)
    return r


def errs(rows, statuses, catalog=None):
    return check(copy.deepcopy(rows), statuses, catalog)[0]


# --------------------------------------------------------------- honest work
def test_honest_queue_passes_clean(statuses):
    """
    The half that keeps the validator usable. Every terminal status, each with
    exactly the evidence it claims and nothing more.
    """
    rows = [
        row("a__cb_rate__rate__w30d", "shipped",
            iv="0.31", psi_12m="0.04", coverage_pct="98.2", shap_rank="1"),
        row("b__cb_rate__rate__w30d", "shipped",
            iv="0.28", psi_12m="0.06", coverage_pct="97.1", shap_rank="2"),
        row("c__cb_rate__rate__w30d", "dropped", drop_reason="stage 4 low IV",
            iv="0.004", psi_12m="0.03", coverage_pct="95.0"),
        row("d__cb_rate__rate__w30d", "dropped",
            drop_reason="stage 3 coverage cliff", psi_12m="0.02", coverage_pct="41.0"),
        row("e__cb_rate__rate__w30d", "dropped",
            drop_reason="stage 6 redundant with sibling window",
            iv="0.19", psi_12m="0.02", coverage_pct="94.0"),
        row("f__cb_rate__rate__w30d", "blocked",
            drop_reason="input unbound: device_fp"),
    ]
    e, w = check(rows, statuses)
    assert e == [], "\n".join(e)
    assert w == [], "\n".join(w)


def test_blocked_needs_no_numbers(statuses):
    """
    `blocked` is the escape hatch that makes honesty cheaper than invention. If
    it demanded metrics, a stuck sub-agent's cheapest path would be to make some
    up — which is the exact behaviour this whole file exists to prevent.
    """
    e, _ = check([row("x__cb_rate__rate__w30d", "blocked",
                      drop_reason="input unbound: label_arrival_ts")], statuses)
    assert e == []


# ------------------------------------------------------------- fabrication
def test_status_claiming_absent_evidence_is_rejected(statuses):
    """`shipped` asserts a stage-7 selection. No shap_rank, no selection."""
    e = errs([row("x__cb_rate__rate__w30d", "shipped",
                  iv="0.2", psi_12m="0.03", coverage_pct="92.0")], statuses)
    assert any("requires shap_rank" in x for x in e)


def test_funnel_cannot_be_entered_in_the_middle(statuses):
    """A stage-4 IV with no stage-3 stability means stage 3 never ran."""
    e = errs([row("x__cb_rate__rate__w30d", "screened",
                  iv="0.22", coverage_pct="93.0")], statuses)
    assert any("cannot be entered in the middle" in x for x in e)


def test_drop_reason_must_have_its_measurement(statuses):
    """Dropping for 'low IV' with an empty iv column asserts a measurement
    nobody recorded — the single most likely fabrication in this workflow."""
    e = errs([row("x__cb_rate__rate__w30d", "dropped",
                  drop_reason="stage 4 low IV", psi_12m="0.03", coverage_pct="95.0")],
             statuses)
    assert any("asserts a measurement nobody recorded" in x for x in e)


def test_identical_metric_triples_are_rejected(statuses):
    """Distinct features do not compute to identical (iv, psi, coverage).
    Three or more sharing a triple is copy-paste, not computation."""
    rows = [row(f"{c}__cb_rate__rate__w30d", "screened",
                iv="0.12", psi_12m="0.02", coverage_pct="88.0") for c in "abc"]
    assert any("identical (iv, psi_12m, coverage_pct)" in x for x in errs(rows, statuses))


def test_two_identical_triples_are_tolerated(statuses):
    """Two can legitimately collide on rounded values; the floor is three, so
    the check does not fire on ordinary rounding."""
    rows = [row(f"{c}__cb_rate__rate__w30d", "screened",
                iv="0.12", psi_12m="0.02", coverage_pct="88.0") for c in "ab"]
    assert not any("identical" in x for x in errs(rows, statuses))


def test_rank_must_be_a_permutation(statuses):
    rows = [row(f"{c}__cb_rate__rate__w30d", "shipped",
                iv="0.2", psi_12m="0.03", coverage_pct="92.0", shap_rank="2")
            for c in "ab"]
    assert any("a rank is a permutation" in x for x in errs(rows, statuses))


def test_illegal_status_is_rejected(statuses):
    e = errs([row("x__cb_rate__rate__w30d", "kept")], statuses)
    assert any("is not a declared queue status" in x for x in e)


def test_out_of_range_and_non_numeric_values(statuses):
    e = errs([row("a__cb_rate__rate__w30d", "screened",
                  iv="-0.1", psi_12m="0.02", coverage_pct="140"),
              row("b__cb_rate__rate__w30d", "screened",
                  iv="high", psi_12m="0.02", coverage_pct="90")], statuses)
    assert any("information value is non-negative" in x for x in e)
    assert any("coverage is a percentage" in x for x in e)
    assert any("is not a number" in x for x in e)


# ------------------------------------------------------------ row integrity
def test_rows_may_not_be_invented_or_deleted(statuses):
    catalog = {"a__cb_rate__rate__w30d", "b__cb_rate__rate__w30d"}
    e = errs([row("a__cb_rate__rate__w30d", "blocked", drop_reason="unbound"),
              row("z__invented__rate__w30d", "shipped", iv="0.9", psi_12m="0.01",
                  coverage_pct="99", shap_rank="1")], statuses, catalog)
    assert any("not in the catalog we handed over" in x for x in e)
    assert any("dropped out of the queue entirely" in x for x in e)


def test_duplicate_rows_are_rejected(statuses):
    r = row("a__cb_rate__rate__w30d", "blocked", drop_reason="unbound")
    assert any("duplicate row" in x for x in errs([r, dict(r)], statuses))


def test_non_terminal_rows_warn_not_error(statuses):
    """Definition of done #1. A partially worked queue is legal mid-flight and
    must not be rejected — but it must be visibly incomplete."""
    e, w = check([row("a__cb_rate__rate__w30d", "candidate")], statuses)
    assert e == []
    assert any("non-terminal" in x for x in w)


# ---------------------------------------------------------------- plumbing
def test_status_vocabulary_ships_in_the_spec():
    """The vocabulary used to exist only in build_workbook.py, which the context
    pack excludes — so an agent working from queue.csv could not know the legal
    values. It must live where the pack can see it."""
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        spec = yaml.safe_load(f)
    assert "queue_statuses" in spec
    ids = {s["id"] for s in spec["queue_statuses"]}
    assert {"candidate", "blocked", "dropped", "shipped"} <= ids
    assert any(s["terminal"] for s in spec["queue_statuses"])


def test_cli_rejects_a_fabricated_queue(tmp_path, statuses):
    p = tmp_path / "q.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerow(row("x__cb_rate__rate__w30d", "shipped", iv="0.5"))
    r = subprocess.run([sys.executable, "validate_queue.py", str(p)],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 1
    assert "queue rejected" in r.stdout
