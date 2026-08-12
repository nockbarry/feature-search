"""
validate_queue.py — check what the AGENT wrote back, not what we handed it.

Every other guard in this repo validates inputs: the registries, the ladder, the
pack's own claims. None of them look at the direction the work actually flows.
With one careful reviewer that is survivable. With a planner dispatching fast
sub-agents over 1,310 candidates it is not, because the dominant failure mode is
not a wrong answer — it is a *plausible* answer with nothing behind it.

`iv`, `psi_12m`, `coverage_pct` and `shap_rank` are empty numeric columns. A
model instructed to fill a column will fill it. Nothing in the handover
distinguishes a computed 0.42 from an invented one, and unlike a leakage bug
this failure is invisible downstream: you ship a feature set selected on
fiction, with a clean-looking audit trail behind it.

So the checks here are mostly STRUCTURAL rather than statistical. They do not
ask "is this number plausible" — that is unfalsifiable. They ask "does the
evidence this row claims actually exist", which is decidable:

  * The pruning funnel is ORDERED. A stage-4 IV without stage-3 coverage means
    stage 4 was reported without stage 3 running.
  * A status implies evidence. `shipped` without `shap_rank` claims a
    multivariate selection that left no trace.
  * A drop must name the stage that killed it, and that stage's metric must be
    present. "low IV" with an empty iv column asserts a measurement nobody made.
  * A rank is a permutation. Duplicate `shap_rank` values are not a ranking.
  * Identical metric triples repeated across many distinct features do not
    happen by computation. They happen by copy-paste.

`blocked` exists so a sub-agent that cannot compute something has somewhere
honest to put it. If this validator is noisy, the fix is more `blocked` rows,
not more confident numbers.

Run:
    python validate_queue.py filled_queue.csv
    python validate_queue.py filled_queue.csv --catalog candidates.csv --strict

Exit 0 clean, 1 on any error. `--strict` promotes warnings to errors.
"""
import argparse
import csv
import os
import sys
from collections import Counter, defaultdict

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))

# Which funnel stage produces each metric. Order matters: a later metric
# present with an earlier one missing means the funnel was not run in order.
STAGE_OF = [
    ("coverage_pct", 3, "stability / coverage"),
    ("psi_12m", 3, "stability / PSI"),
    ("iv", 4, "univariate"),
    ("shap_rank", 7, "multivariate"),
]

RANGES = {
    "iv": (0.0, None, "information value is non-negative"),
    "psi_12m": (0.0, None, "PSI is non-negative"),
    "coverage_pct": (0.0, 100.0, "coverage is a percentage"),
    "shap_rank": (1.0, None, "a rank starts at 1"),
}

# drop_reason must name the stage that killed the row. These are the tokens we
# recognise; an unrecognised reason is a warning, not an error, because a real
# drop can have a reason the funnel does not enumerate.
REASON_STAGE = {
    "availability": 1, "latency": 1, "not computable": 1, "unavailable": 1,
    "support": 2, "min_support": 2, "thin": 2, "sparse": 2,
    "stability": 3, "psi": 3, "coverage": 3, "seasonal": 3,
    "iv": 4, "univariate": 4, "mutual information": 4, "no signal": 4,
    "redundan": 6, "correlated": 6, "duplicate": 6,
    "shap": 7, "importance": 7, "multivariate": 7,
    "leak": 8, "leakage": 8, "point-in-time": 8,
    "operating point": 9, "threshold": 9,
}
STAGE_METRIC = {3: "coverage_pct", 4: "iv", 7: "shap_rank"}


def num(v):
    if v is None or str(v).strip() == "":
        return None
    try:
        return float(str(v).strip())
    except ValueError:
        return "BAD"


def load_statuses(spec):
    st = spec.get("queue_statuses")
    if not st:
        raise SystemExit("feature_space.yaml has no queue_statuses registry — "
                         "the legal values are undefined, so nothing can be checked.")
    return {s["id"]: s for s in st}


def check(rows, statuses, catalog_names=None):
    errors, warnings = [], []
    E, W = errors.append, warnings.append

    if not rows:
        return ["queue is empty"], []

    required_cols = {"feature_name", "status", "drop_reason",
                     "iv", "psi_12m", "coverage_pct", "shap_rank"}
    missing_cols = required_cols - set(rows[0])
    if missing_cols:
        return [f"queue is missing column(s): {', '.join(sorted(missing_cols))}"], []

    # ---- row-set integrity: no invented or vanished candidates -------------
    names = [r["feature_name"] for r in rows]
    dupes = [n for n, c in Counter(names).items() if c > 1]
    for n in sorted(dupes)[:10]:
        E(f"duplicate row: {n}")
    if catalog_names is not None:
        for n in sorted(set(names) - catalog_names)[:10]:
            E(f"row not in the catalog we handed over: {n}")
        missing = catalog_names - set(names)
        if missing:
            E(f"{len(missing)} candidate(s) dropped out of the queue entirely, e.g. "
              f"{', '.join(sorted(missing)[:5])} — rows may be filled, never deleted")

    seen_triples = defaultdict(list)
    ranks = []
    unresolved = 0

    for r in rows:
        fn = r["feature_name"]
        st = (r.get("status") or "").strip()

        if st not in statuses:
            E(f"{fn}: status {st!r} is not a declared queue status "
              f"({', '.join(sorted(statuses))})")
            continue
        if not statuses[st]["terminal"]:
            unresolved += 1

        vals = {}
        for col, (lo, hi, why) in RANGES.items():
            v = num(r.get(col))
            if v == "BAD":
                E(f"{fn}: {col}={r[col]!r} is not a number")
                continue
            vals[col] = v
            if v is None:
                continue
            if lo is not None and v < lo:
                E(f"{fn}: {col}={v} — {why}")
            if hi is not None and v > hi:
                E(f"{fn}: {col}={v} — {why}")

        # ---- the status claims evidence; does it exist? --------------------
        for need in statuses[st]["requires"]:
            got = r.get(need)
            if got is None or str(got).strip() == "":
                E(f"{fn}: status '{st}' requires {need}, which is empty "
                  f"— the status claims work that left no evidence")

        # ---- funnel ordering ----------------------------------------------
        present = [(c, s) for c, s, _ in STAGE_OF if vals.get(c) is not None]
        if present:
            deepest = max(s for _, s in present)
            for col, stage, label in STAGE_OF:
                if stage < deepest and vals.get(col) is None:
                    E(f"{fn}: {col} (stage {stage}, {label}) is empty but a "
                      f"stage-{deepest} metric is filled — the funnel runs "
                      f"cheapest-first and cannot be entered in the middle")

        # ---- a drop must name its stage, and that stage must have a number --
        reason = (r.get("drop_reason") or "").strip().lower()
        if st == "dropped":
            stage = next((s for tok, s in REASON_STAGE.items() if tok in reason), None)
            if stage is None:
                W(f"{fn}: drop_reason {reason[:48]!r} does not name a funnel stage")
            else:
                metric = STAGE_METRIC.get(stage)
                if metric and vals.get(metric) is None:
                    E(f"{fn}: dropped at stage {stage} but {metric} is empty "
                      f"— the reason asserts a measurement nobody recorded")
        elif reason and st != "blocked":
            W(f"{fn}: status '{st}' but drop_reason is filled — which is it?")

        if vals.get("shap_rank") is not None:
            ranks.append((vals["shap_rank"], fn))

        triple = tuple(vals.get(c) for c in ("iv", "psi_12m", "coverage_pct"))
        if all(v is not None for v in triple):
            seen_triples[triple].append(fn)

    # ---- a rank is a permutation ------------------------------------------
    rc = Counter(v for v, _ in ranks)
    for v, c in sorted(rc.items()):
        if c > 1:
            E(f"shap_rank {v:g} assigned to {c} features — a rank is a permutation, "
              f"not a score")

    # ---- computation does not collide -------------------------------------
    for triple, fns in sorted(seen_triples.items(), key=lambda kv: -len(kv[1])):
        if len(fns) >= 3:
            E(f"{len(fns)} features share the identical (iv, psi_12m, coverage_pct) "
              f"= {triple} — distinct features do not compute to identical triples; "
              f"e.g. {', '.join(fns[:3])}")

    # ---- definition of done ------------------------------------------------
    if unresolved:
        W(f"{unresolved} row(s) still non-terminal — definition of done #1 "
          f"requires every row to reach a terminal status "
          f"({', '.join(sorted(s for s, v in statuses.items() if v['terminal']))})")

    return errors, warnings


def summary(rows, statuses):
    c = Counter((r.get("status") or "").strip() for r in rows)
    out = ["", f"{len(rows)} rows"]
    for st in list(statuses) + sorted(set(c) - set(statuses)):
        if c.get(st):
            mark = "" if st in statuses and statuses[st]["terminal"] else "  (non-terminal)"
            out.append(f"  {st:12} {c[st]:5}{mark}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate a filled work queue.")
    ap.add_argument("queue", help="the filled queue.csv the agent wrote back")
    ap.add_argument("--spec", default=os.path.join(ROOT, "feature_space.yaml"))
    ap.add_argument("--catalog", help="the queue as handed over, to detect added/removed rows")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    a = ap.parse_args(argv)

    with open(a.spec) as f:
        statuses = load_statuses(yaml.safe_load(f))
    with open(a.queue, newline="") as f:
        rows = list(csv.DictReader(f))

    catalog_names = None
    if a.catalog:
        with open(a.catalog, newline="") as f:
            catalog_names = {r["feature_name"] for r in csv.DictReader(f)}

    errors, warnings = check(rows, statuses, catalog_names)

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")
    print(summary(rows, statuses))

    if errors or (a.strict and warnings):
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s) — queue rejected.")
        return 1
    print(f"\nqueue accepted ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
