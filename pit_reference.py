"""
pit_reference.py — executable specification of the two-clock rule.

`pit_aggregate_template.sql` is the warehouse implementation; it cannot run in
CI. This module is the same logic in dependency-free Python so that
`tests/test_pit_leakage.py` can assert it against hand-computed fixtures.

The two implementations must agree. If you change one, change both, and make
the fixture test prove it.

THE RULE
    Feature values filter on label_arrival_ts (when we LEARNED it),
    never on event_ts (when it HAPPENED).

Transactions are plain dicts:
    {txn_id, entity_id, event_ts, amount, disposition}   disposition in SEN|DEN|ERR
Labels are plain dicts:
    {txn_id, event_ts, label_arrival_ts, cb_amount, is_fraud_cb}

Timestamps are floats in days for readability; any monotonic numeric works.
"""
from __future__ import annotations

__all__ = [
    "eligible_history", "pit_aggregate", "shrink", "debias_censoring",
    "naive_aggregate_LEAKY",
]


def eligible_history(txns, labels, entity_id, as_of_ts, window_days=None,
                     scoring_txn_id=None):
    """
    History rows usable for a feature computed at `as_of_ts`.

    Three exclusions, all strict, all deliberate:
      1. event_ts < as_of_ts        — the future is not history
      2. scoring txn itself excluded — a transaction may not describe itself
      3. window floor is inclusive  — event_ts >= as_of_ts - window
    """
    lab = {rec["txn_id"]: rec for rec in labels}
    floor = None if window_days is None else as_of_ts - window_days
    out = []
    for t in txns:
        if t["entity_id"] != entity_id:
            continue
        if t["event_ts"] >= as_of_ts:                 # strict: no same-instant rows
            continue
        if scoring_txn_id is not None and t["txn_id"] == scoring_txn_id:
            continue
        if floor is not None and t["event_ts"] < floor:
            continue
        rec = lab.get(t["txn_id"])
        # THE KEY LINE. A chargeback counts only if we had already been told.
        known = bool(rec) and rec["label_arrival_ts"] < as_of_ts
        out.append({
            **t,
            "known_cb": int(known and rec["is_fraud_cb"]),
            "known_cb_amount": (rec["cb_amount"] if known and rec["is_fraud_cb"] else 0.0),
        })
    return out


def pit_aggregate(txns, labels, entity_id, as_of_ts, window_days=None,
                  scoring_txn_id=None):
    """Point-in-time entity aggregate. Carries the policy companions."""
    h = eligible_history(txns, labels, entity_id, as_of_ts, window_days, scoring_txn_id)
    n = len(h)
    sen = [r for r in h if r["disposition"] == "SEN"]
    n_sen = len(sen)
    sen_amount = sum(r["amount"] for r in sen)
    n_cb = sum(r["known_cb"] for r in h)
    cb_amount = sum(r["known_cb_amount"] for r in h)
    return {
        "n_txn": n,
        "n_sen": n_sen,
        "sen_amount": sen_amount,
        "n_cb": n_cb,
        "cb_amount": cb_amount,
        "cb_rate_raw": (n_cb / n_sen) if n_sen else None,
        "cb_rate_dollar": (cb_amount / sen_amount) if sen_amount else None,
        # policy companions: ship these with EVERY outcome rate or the feature
        # silently means "what the champion let through"
        "den_rate": (sum(r["disposition"] == "DEN" for r in h) / n) if n else None,
        "err_rate": (sum(r["disposition"] == "ERR" for r in h) / n) if n else None,
    }


def shrink(n_cb, n_sen, parent_rate, k):
    """
    Empirical-Bayes shrinkage toward a parent entity.
    n == k gives equal weight to the entity and the parent.
    Never ship a raw rate below the catalog's min_support_n; ship this instead.
    """
    return (n_cb + k * parent_rate) / (n_sen + k)


def debias_censoring(approved_cb, approved_n, released):
    """
    Correct for the champion's censoring of blocked traffic.

    Entity rates computed over approved traffic only make a heavily-blocked
    entity look clean. Released blocks carry sample_weight = 1 / P(release),
    so they stand in for the whole blocked population.

    `released` is an iterable of {is_fraud_cb, sample_weight}.
    """
    rel_cb = sum(r["is_fraud_cb"] * r["sample_weight"] for r in released)
    rel_n = sum(r["sample_weight"] for r in released)
    denom = approved_n + rel_n
    return (approved_cb + rel_cb) / denom if denom else None


def naive_aggregate_LEAKY(txns, labels, entity_id, as_of_ts, window_days=None,
                          scoring_txn_id=None):
    """
    THE BUG, implemented on purpose.

    Identical to pit_aggregate except it joins labels on event_ts alone, so a
    chargeback reported after as_of_ts still lands in the feature. Present only
    so the test suite can assert the correct implementation differs from it —
    if these two ever agree on the fixture, the PIT filter has been lost.

    Never import this outside tests.
    """
    lab = {rec["txn_id"]: rec for rec in labels}
    floor = None if window_days is None else as_of_ts - window_days
    h = [t for t in txns
         if t["entity_id"] == entity_id
         and t["event_ts"] < as_of_ts
         and (scoring_txn_id is None or t["txn_id"] != scoring_txn_id)
         and (floor is None or t["event_ts"] >= floor)]
    n_sen = sum(t["disposition"] == "SEN" for t in h)
    n_cb = sum(int(bool(lab.get(t["txn_id"])) and lab[t["txn_id"]]["is_fraud_cb"])
               for t in h)
    return {"n_sen": n_sen, "n_cb": n_cb,
            "cb_rate_raw": (n_cb / n_sen) if n_sen else None}
