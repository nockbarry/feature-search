"""
test_compatibility.py — the expander's rules are the search's semantics.

These assert the invariants that make the catalog trustworthy:
  * no nonsensical slot combinations survive
  * feature names are unique and decode back to their slots
  * the resolution ladder narrows RESOLUTION but never COVERAGE
"""
import os
import sys
from collections import Counter

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from expand_catalog import (  # noqa: E402
    Resolution,
    build_entities,
    compatible,
    expand,
    fname,
    tier,
)

SPEC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "feature_space.yaml")


@pytest.fixture(scope="module")
def spec():
    with open(SPEC_PATH) as f:
        return yaml.safe_load(f)


def by_id(seq, i):
    return next(x for x in seq if x["id"] == i)


# ── slot compatibility ──────────────────────────────────────────────────────

def test_label_dependent_measures_reject_short_windows(spec):
    """
    A 5-minute chargeback rate is meaningless: at ~30 days to 90% maturity there
    are no labels in the window. Anything under 7 days is rejected.
    """
    cb = by_id(spec["measures"], "cb_fraud_rate")
    ent = by_id(spec["entities"], "device")
    agg = by_id(spec["aggregations"], "rate")
    raw = by_id(spec["transforms"], "raw")
    nof = by_id(spec["filters"], "none")
    for w in ("w1m", "w5m", "w1h", "w6h", "w1d"):
        assert not compatible(ent, cb, agg, by_id(spec["windows"], w), raw, nof), w
    assert compatible(ent, cb, agg, by_id(spec["windows"], "w30d"), raw, nof)


def test_distinct_count_measures_require_distinct_count_agg(spec):
    """d_pan is a distinct count; summing or averaging it is meaningless."""
    ent = by_id(spec["entities"], "device")
    m = by_id(spec["measures"], "d_pan")
    w = by_id(spec["windows"], "w1d")
    raw = by_id(spec["transforms"], "raw")
    nof = by_id(spec["filters"], "none")
    assert compatible(ent, m, by_id(spec["aggregations"], "dcnt"), w, raw, nof)
    for a in ("sum", "mean", "std", "max"):
        assert not compatible(ent, m, by_id(spec["aggregations"], a), w, raw, nof), a


def test_entity_never_counts_itself(spec):
    """'distinct devices per device' is always 1. Never emit it."""
    dev = by_id(spec["entities"], "device")
    d_device = by_id(spec["measures"], "d_device")
    args = (by_id(spec["aggregations"], "dcnt"), by_id(spec["windows"], "w1d"),
            by_id(spec["transforms"], "raw"), by_id(spec["filters"], "none"))
    assert not compatible(dev, d_device, *args)
    # but distinct devices per CARD is exactly the signal we want
    assert compatible(by_id(spec["entities"], "pan"), d_device, *args)


def test_time_since_aggregations_are_windowless(spec):
    """'time since last' over a 7-day window is a contradiction."""
    ent = by_id(spec["entities"], "device")
    m = by_id(spec["measures"], "txn_cnt")
    tsl = by_id(spec["aggregations"], "tsl")
    raw = by_id(spec["transforms"], "raw")
    nof = by_id(spec["filters"], "none")
    assert compatible(ent, m, tsl, by_id(spec["windows"], "life"), raw, nof)
    assert not compatible(ent, m, tsl, by_id(spec["windows"], "w7d"), raw, nof)


def test_lag_transforms_require_a_time_window(spec):
    """A prior window only exists for time-type windows, not count or lifetime."""
    ent = by_id(spec["entities"], "device")
    m = by_id(spec["measures"], "txn_cnt")
    agg = by_id(spec["aggregations"], "cnt")
    ratio = by_id(spec["transforms"], "ratio_sl")
    nof = by_id(spec["filters"], "none")
    assert compatible(ent, m, agg, by_id(spec["windows"], "w7d"), ratio, nof)
    for w in ("n10", "life"):
        assert not compatible(ent, m, agg, by_id(spec["windows"], w), ratio, nof), w


def test_shrinkage_applies_only_to_rates(spec):
    """Empirical-Bayes shrinkage of a raw count is not a defined operation."""
    ent = by_id(spec["entities"], "device")
    shrunk = by_id(spec["transforms"], "shrunk")
    w = by_id(spec["windows"], "w30d")
    nof = by_id(spec["filters"], "none")
    assert compatible(ent, by_id(spec["measures"], "cb_fraud_rate"),
                      by_id(spec["aggregations"], "rate"), w, shrunk, nof)
    assert not compatible(ent, by_id(spec["measures"], "txn_cnt"),
                          by_id(spec["aggregations"], "cnt"), w, shrunk, nof)


def test_diff_filters_need_a_contrast_to_exist(spec):
    """'on a different device' is vacuous when the entity IS the device."""
    m = by_id(spec["measures"], "txn_cnt")
    agg = by_id(spec["aggregations"], "cnt")
    w = by_id(spec["windows"], "w7d")
    raw = by_id(spec["transforms"], "raw")
    dd = by_id(spec["filters"], "diff_device")
    assert not compatible(by_id(spec["entities"], "device"), m, agg, w, raw, dd)
    assert compatible(by_id(spec["entities"], "pan"), m, agg, w, raw, dd)


# ── naming contract ─────────────────────────────────────────────────────────

def test_feature_names_are_unique(spec):
    rows, _ = expand(spec, max_tier=2, stages_bc=True, level="L2")
    dupes = [n for n, c in Counter(r["feature_name"] for r in rows).items() if c > 1]
    assert not dupes, f"{len(dupes)} duplicate names, e.g. {dupes[:3]}"


def test_feature_name_decodes_to_its_slots(spec):
    """{entity}__{measure}__{agg}__{window}[__f_filter][__t_transform]"""
    ent = by_id(spec["entities"], "device")
    m = by_id(spec["measures"], "txn_cnt")
    agg = by_id(spec["aggregations"], "cnt")
    w = by_id(spec["windows"], "w7d")
    assert fname(ent, m, agg, w, by_id(spec["filters"], "none"),
                 by_id(spec["transforms"], "raw")) == "device__txn_cnt__cnt__w7d"
    assert fname(ent, m, agg, w, by_id(spec["filters"], "diff_name"),
                 by_id(spec["transforms"], "ratio_sl")) == \
        "device__txn_cnt__cnt__w7d__f_diff_name__t_ratio_sl"


def test_names_are_filesystem_and_sql_safe(spec):
    rows, _ = expand(spec, max_tier=2, stages_bc=True, level="L2")
    for r in rows[:2000]:
        n = r["feature_name"]
        assert n.replace("_", "").isalnum(), n
        assert len(n) < 128, n


# ── resolution ladder invariants ────────────────────────────────────────────

def test_ladder_narrows_resolution_monotonically(spec):
    """L0 windows must be a subset of L1's, and L1's of L2's. No rung invents a scale."""
    lad = spec["resolution_ladder"]
    for fam in ("outcome", "velocity", "behavior", "score"):
        l0, l1, l2 = (set(lad[lv]["windows"][fam]) for lv in ("L0", "L1", "L2"))
        assert l0 <= l1, (fam, l0 - l1)
        assert l1 <= l2, (fam, l1 - l2)


def test_coverage_is_never_gated_by_resolution(spec):
    """
    THE CONTRACT. Resolution rungs prune windows, transforms, filters and
    sibling granularities. They must never remove a MEASURE or an entity CLASS —
    a missing quantity is a blind spot no resolution recovers.
    """
    l0, _ = expand(spec, max_tier=3, level="L0")
    l2, _ = expand(spec, max_tier=3, level="L2")
    assert {r["measure"] for r in l0} == {r["measure"] for r in l2}
    assert {r["entity_class"] for r in l0} == {r["entity_class"] for r in l2}


def test_l0_is_materially_cheaper_than_l2(spec):
    """If the probe rung is not much cheaper it is not doing its job."""
    l0, _ = expand(spec, max_tier=1, stages_bc=True, level="L0")
    l2, _ = expand(spec, max_tier=1, stages_bc=True, level="L2")
    assert len(l2) > 10 * len(l0)


def test_l0_windows_are_widely_spaced(spec):
    """
    corr(N_s, N_l) = sqrt(w_s/w_l) for nested Poisson counts, so the probe rung
    keeps adjacent windows at >= x10 spacing -> correlation floor <= ~0.32.
    """
    secs = {w["id"]: w["seconds"] for w in spec["windows"]}
    for fam, ids in spec["resolution_ladder"]["L0"]["windows"].items():
        timed = sorted((secs[i] for i in ids if secs.get(i)), key=float)
        for a, b in zip(timed, timed[1:], strict=False):
            assert b / a >= 10, f"{fam}: spacing x{b/a:.1f} too tight for a probe rung"


def test_resolution_rejects_unknown_level(spec):
    with pytest.raises(SystemExit):
        Resolution(spec, "L9")


# ── tiering ─────────────────────────────────────────────────────────────────

def test_tier_is_worst_slot(spec):
    assert tier([{"priority": 1}, {"priority": 1}]) == 1
    assert tier([{"priority": 1}, {"priority": 2}]) == 2
    assert tier([{"priority": 1}, {"priority": 3}]) == 3
    assert tier([{}]) == 3           # unstated priority is pessimistic


def test_composite_inherits_its_weakest_link(spec):
    """
    A composite is only as adversarially durable as its cheapest-to-rotate part.
    device (cheap) x bin8 (expensive) -> cheap.
    """
    ents = {e["id"]: e for e in build_entities(spec)}
    assert ents["device_x_bin8"]["adversarial_cost"] == "cheap"


def test_every_composite_states_a_hypothesis(spec):
    """No composite without a stated fraud hypothesis — support will kill it anyway."""
    for c in spec["composites"]:
        assert c.get("hypothesis", "").strip(), c["id"]


# ── catalog integrity ───────────────────────────────────────────────────────

def test_label_dependent_rows_are_flagged_high_leakage(spec):
    rows, _ = expand(spec, max_tier=2, level="L2")
    labelled = {m["id"] for m in spec["measures"] if m["label_dependent"]}
    names = {m["name"] for m in spec["measures"] if m["id"] in labelled}
    for r in rows:
        if r["measure"] in names:
            assert r["leakage_risk"].startswith("HIGH"), r["feature_name"]


def test_label_dependent_rows_carry_a_higher_support_floor(spec):
    rows, _ = expand(spec, max_tier=2, level="L2")
    for r in rows:
        if r["leakage_risk"].startswith("HIGH"):
            assert r["min_support_n"] >= 200, r["feature_name"]


def test_every_row_has_the_four_gating_attributes(spec):
    rows, _ = expand(spec, max_tier=2, stages_bc=True, level="L1")
    for r in rows:
        for k in ("availability", "leakage_risk", "min_support_n", "adversarial_halflife"):
            assert r[k] not in (None, ""), (r["feature_name"], k)
