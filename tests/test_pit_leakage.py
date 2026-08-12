"""
test_pit_leakage.py — the most important test in this repo.

Every expected value below was computed BY HAND from the fixture and is stated
in the docstring of its test. That is the point: the suite checks the pipeline
against ground truth, not against last week's output. A refactor that changes
behaviour fails here instead of silently shipping a leaky feature.

FIXTURE — device D1, plus D2 to prove entity isolation.

  txn  entity  event_ts  amount  disposition   chargeback
  ---  ------  --------  ------  -----------   ---------------------------------
  T1   D1      day  1      100   SEN           CB $100, REPORTED day 40  <-- late
  T2   D1      day  5      200   SEN           CB $200, reported day 10
  T3   D1      day  8       50   DEN           none (never settled)
  T4   D1      day 12      300   SEN           none
  T5   D1      day 20      150   SEN           <-- the SCORING row, as_of day 20
  T6   D2      day  6      999   SEN           CB $999, reported day  7
  T7   D1      day 15       80   SEN           CB  $80, reported day 20  <-- exactly

Scoring T5 at as_of = day 20.

T1's chargeback is the trap. It HAPPENED on day 1, well inside any window, but
we were not told until day 40. At day 20 it must contribute nothing.

T7's chargeback is the boundary trap: reported at exactly as_of. The rule is
strict (<), so it is also excluded — we model "known strictly before the
decision", which is the only version that is safe under equal timestamps.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pit_reference import (  # noqa: E402
    debias_censoring,
    eligible_history,
    naive_aggregate_LEAKY,
    pit_aggregate,
    shrink,
)

TXNS = [
    {"txn_id": "T1", "entity_id": "D1", "event_ts": 1.0,  "amount": 100.0, "disposition": "SEN"},
    {"txn_id": "T2", "entity_id": "D1", "event_ts": 5.0,  "amount": 200.0, "disposition": "SEN"},
    {"txn_id": "T3", "entity_id": "D1", "event_ts": 8.0,  "amount":  50.0, "disposition": "DEN"},
    {"txn_id": "T4", "entity_id": "D1", "event_ts": 12.0, "amount": 300.0, "disposition": "SEN"},
    {"txn_id": "T5", "entity_id": "D1", "event_ts": 20.0, "amount": 150.0, "disposition": "SEN"},
    {"txn_id": "T6", "entity_id": "D2", "event_ts": 6.0,  "amount": 999.0, "disposition": "SEN"},
    {"txn_id": "T7", "entity_id": "D1", "event_ts": 15.0, "amount":  80.0, "disposition": "SEN"},
]
LABELS = [
    {"txn_id": "T1", "event_ts": 1.0, "label_arrival_ts": 40.0, "cb_amount": 100.0, "is_fraud_cb": 1},
    {"txn_id": "T2", "event_ts": 5.0, "label_arrival_ts": 10.0, "cb_amount": 200.0, "is_fraud_cb": 1},
    {"txn_id": "T6", "event_ts": 6.0, "label_arrival_ts":  7.0, "cb_amount": 999.0, "is_fraud_cb": 1},
    {"txn_id": "T7", "event_ts": 15.0, "label_arrival_ts": 20.0, "cb_amount":  80.0, "is_fraud_cb": 1},
]
AS_OF = 20.0
ARGS = dict(entity_id="D1", as_of_ts=AS_OF, window_days=30.0, scoring_txn_id="T5")


def test_late_reported_chargeback_is_invisible():
    """
    T1's CB arrives day 40. At day 20 it is unknown.
    Eligible D1 history = T1, T2, T3, T4, T7 (T5 is the scoring row).
    known_cb: T2 only (day 10 < 20). T1 excluded (day 40), T7 excluded (day 20 not < 20).
    Hand-computed: n_cb == 1.
    """
    h = eligible_history(TXNS, LABELS, **ARGS)
    assert {r["txn_id"] for r in h} == {"T1", "T2", "T3", "T4", "T7"}
    assert {r["txn_id"] for r in h if r["known_cb"]} == {"T2"}


def test_label_arrival_at_as_of_is_excluded_strictly():
    """T7 reported at exactly day 20. Strict < means excluded. Included at 20.001."""
    h = eligible_history(TXNS, LABELS, **ARGS)
    assert next(r for r in h if r["txn_id"] == "T7")["known_cb"] == 0

    later = dict(ARGS, as_of_ts=20.001)
    h2 = eligible_history(TXNS, LABELS, **later)
    assert next(r for r in h2 if r["txn_id"] == "T7")["known_cb"] == 1


def test_scoring_transaction_excludes_itself():
    """T5 must not appear in its own history — a txn cannot describe itself."""
    h = eligible_history(TXNS, LABELS, **ARGS)
    assert "T5" not in {r["txn_id"] for r in h}


def test_entity_isolation():
    """T6 belongs to D2 and must never leak into D1's aggregate."""
    h = eligible_history(TXNS, LABELS, **ARGS)
    assert "T6" not in {r["txn_id"] for r in h}


def test_rate_denominator_is_sen_only():
    """
    SEN rows in history: T1(100), T2(200), T4(300), T7(80) -> n_sen = 4, $680.
    T3 is DEN: it never settled, so it cannot charge back and is not in the denominator.
    Known CB: T2 only -> n_cb = 1, cb_amount = 200.
    Hand-computed: cb_rate_raw = 1/4 = 0.25 ; cb_rate_dollar = 200/680 = 0.294117...
    """
    a = pit_aggregate(TXNS, LABELS, **ARGS)
    assert a["n_txn"] == 5
    assert a["n_sen"] == 4
    assert a["sen_amount"] == pytest.approx(680.0)
    assert a["n_cb"] == 1
    assert a["cb_rate_raw"] == pytest.approx(0.25)
    assert a["cb_rate_dollar"] == pytest.approx(200.0 / 680.0)


def test_policy_companions_present():
    """
    DEN/ERR rates ship with every outcome rate, or the feature silently means
    'what the champion let through'. History has 5 rows, 1 DEN, 0 ERR.
    """
    a = pit_aggregate(TXNS, LABELS, **ARGS)
    assert a["den_rate"] == pytest.approx(1 / 5)
    assert a["err_rate"] == pytest.approx(0.0)


def test_window_floor_is_inclusive_and_excludes_older():
    """
    10-day window at day 20 -> floor day 10, inclusive.
    Eligible: T4 (day 12), T7 (day 15). T1/T2/T3 fall outside.
    Hand-computed: n_sen = 2, n_cb = 0 (T7's label arrives exactly at as_of).
    """
    a = pit_aggregate(TXNS, LABELS, entity_id="D1", as_of_ts=AS_OF,
                      window_days=10.0, scoring_txn_id="T5")
    assert a["n_txn"] == 2
    assert a["n_sen"] == 2
    assert a["n_cb"] == 0
    assert a["cb_rate_raw"] == pytest.approx(0.0)


def test_naive_join_disagrees_and_overstates():
    """
    Regression guard. The leaky version counts T1 and T7 because it joins on
    event_ts alone: n_cb = 3 of 4 SEN = 0.75, versus the correct 0.25.
    If these two ever agree on this fixture, the PIT filter has been lost.
    """
    correct = pit_aggregate(TXNS, LABELS, **ARGS)
    leaky = naive_aggregate_LEAKY(TXNS, LABELS, **ARGS)
    assert leaky["n_cb"] == 3
    assert leaky["cb_rate_raw"] == pytest.approx(0.75)
    assert leaky["cb_rate_raw"] > correct["cb_rate_raw"]


def test_no_history_returns_none_not_zero():
    """
    A rate with an empty denominator is UNKNOWN, not 0.0. Returning 0.0 tells
    the model 'this entity is clean' when the truth is 'we have never seen it',
    which is the opposite of the intended signal for a brand-new entity.
    """
    a = pit_aggregate(TXNS, LABELS, entity_id="D_UNSEEN", as_of_ts=AS_OF,
                      window_days=30.0)
    assert a["n_sen"] == 0
    assert a["cb_rate_raw"] is None


def test_shrinkage_pulls_thin_support_toward_parent():
    """
    n_cb=1, n_sen=4, parent_rate=0.10, k=20.
    Hand-computed: (1 + 20*0.10) / (4 + 20) = 3.0/24 = 0.125.
    Raw would be 0.25 — shrinkage halves it because n << k.
    """
    assert shrink(1, 4, 0.10, 20) == pytest.approx(0.125)


def test_shrinkage_converges_to_raw_as_support_grows():
    """With n_sen=10000 the parent prior is irrelevant: result -> 0.25."""
    assert shrink(2500, 10000, 0.10, 20) == pytest.approx(0.25, abs=5e-4)


def test_shrinkage_equal_weight_at_n_equals_k():
    """
    n_sen == k gives the entity and the parent equal weight.
    n_cb=10, n_sen=20, parent=0.10, k=20 -> (10+2)/40 = 0.30,
    exactly midway between raw 0.50 and parent 0.10.
    """
    assert shrink(10, 20, 0.10, 20) == pytest.approx(0.30)
    assert shrink(10, 20, 0.10, 20) == pytest.approx((0.50 + 0.10) / 2)


def test_censoring_correction_raises_a_heavily_blocked_entity():
    """
    Approved traffic: 2 CBs in 10 -> looks like 20%.
    Released blocks: 3 sampled at P(release)=1/50 so weight 50 each; 1 charged back.
        weighted cb = 50, weighted n = 150.
    Hand-computed: (2 + 50) / (10 + 150) = 52/160 = 0.325.
    The entity is far worse than approved traffic alone suggests — which is the
    whole reason the release programme exists.
    """
    released = [{"is_fraud_cb": 1, "sample_weight": 50.0},
                {"is_fraud_cb": 0, "sample_weight": 50.0},
                {"is_fraud_cb": 0, "sample_weight": 50.0}]
    assert debias_censoring(2, 10, released) == pytest.approx(0.325)
    assert debias_censoring(2, 10, released) > 2 / 10


def test_censoring_correction_is_identity_with_no_releases():
    """No release programme -> no correction available, and the rate is unchanged."""
    assert debias_censoring(2, 10, []) == pytest.approx(0.20)
