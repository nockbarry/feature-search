"""
shard.py — split the queue into self-contained work packets.

The pack is ~90k tokens. Handing that to each of N sub-agents is wasteful and,
worse, unreliable: a fast worker given 90k tokens skims, and what it skims is
whatever sits in the middle of a long document — which is exactly where the
correctness rules live.

So each shard gets a TASK.md that is *self-contained*. The test that matters,
and the one tests/test_shard.py enforces, is: a worker that reads ONLY its
TASK.md and its own queue.csv can do the job correctly. No "see AGENT_BRIEF
section 7." The two-clock rule, the status vocabulary and the never-invent-a-
number contract are restated verbatim in every packet, because a rule referenced
is a rule skipped.

WHY SHARD BY ENTITY

At L0 the queue is 1,167 rows over 27 entities but only 54 distinct
(entity, window) panels — a 21.6:1 ratio of rows to underlying aggregate
computations. Every feature on `device_x_bin8` over 30 days reads the same
panel. Shard by row range and up to 21 workers each rebuild that panel
independently; shard by entity and it is built once, by the worker that needs
it. Entity shards also come out naturally even here (35-44 rows each), so no
balancing logic is required.

The corollary matters for the planner: shards are independent for stages 1-4,
which is what makes them parallelisable. Stages 6, 7 and 9 — redundancy
clustering, multivariate selection, operating-point re-ranking — are
cross-shard by definition and CANNOT be done inside a shard. Each task card
says so explicitly, because a worker asked to "screen these features" will
otherwise happily invent a SHAP rank for 44 rows in isolation.

Run:
    python shard.py                     # shards/ keyed by entity
    python shard.py --by entity_class   # coarser: 10 shards
    python shard.py --max-rows 60       # split oversized shards
"""
import argparse
import csv
import os
import shutil
import sys
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))

# Restated verbatim in every packet. A reference is a rule that gets skipped.
NON_NEGOTIABLES = """\
## Non-negotiables — read these, they are the whole job

**1. The two-clock rule.** Every aggregate that touches an outcome must be
computed from what was KNOWN at scoring time, not what is true in hindsight.
Filter on `label_arrival_ts` (when we learned it), never `event_ts` (when it
happened). A chargeback on a 01-Jun transaction reported 03-Jul must not appear
in a 15-Jun entity rate.

This is the most common leak in fraud modelling. It inflates offline AUC, and
it concentrates in exactly the region where champion and challenger disagree,
so it flatters the new model specifically. If your number looks too good, look
here first.

`test_pit_leakage.py` in the pack is the acceptance test. Your implementation
must reproduce its hand-computed fixtures — including the chargeback that
happened on day 1, was reported on day 40, and must be invisible when scoring
at day 20.

**2. Never write a number you did not compute.** `iv`, `psi_12m`,
`coverage_pct` and `shap_rank` are empty because they are measurements, not
estimates. If you cannot compute one, set `status: blocked` and name the missing
input in `drop_reason`. A blocked row is a data request with an owner. An
invented number is a feature selected on fiction, and nothing downstream catches
it. `validate_queue.py` rejects rows whose status claims work that left no
evidence.

**3. Censoring.** Entity outcome rates are computable only over traffic the
champion approved, so a heavily-blocked entity looks clean. Carry `block_rate`
alongside every `cb_rate`. Never coalesce a rate to zero: zero means "we looked
and found no fraud", null means "we have never seen this entity". For a new
entity those are opposite risk statements.

**4. Support floors.** Never expose a raw rate on n below the row's
`min_support_n`. Use empirical-Bayes shrinkage toward the parent entity and
expose n itself as a companion feature.
"""

PROCEDURE = """\
## Procedure, per row

Work the funnel cheapest-first and STOP at the first screen a row fails. Do not
run stage 4 on a row that failed stage 1 — that is the entire point of the
ordering.

**Stage 1 — Availability.** Is this computable at decision time, from bound
inputs, inside the latency budget? Check the row's `availability` column against
your binding.
  - not computable, input unbound  -> `status: blocked`, `drop_reason: input unbound: <requirement id>`
  - not computable at decision time -> `status: dropped`, `drop_reason: stage 1 availability - <why>`
  - computable                      -> continue

**Stage 2 — Support.** Median n per entity-window cell against `min_support_n`.
  - below floor -> `status: dropped`, `drop_reason: stage 2 support - median n = <value>`
  - at or above -> continue

**Stage 3 — Stability.** Compute 12-month PSI and coverage. Fill `psi_12m` and
`coverage_pct` — both, always, before touching stage 4.
  - seasonal collapse or coverage cliff -> `status: dropped`, `drop_reason: stage 3 <psi|coverage> - <value>`
  - stable -> continue

**Stage 4 — Univariate.** Information value or mutual information against the
label. Run it per label, not only the primary. Fill `iv`.
  - no signal -> `status: dropped`, `drop_reason: stage 4 low IV - <value>`
  - signal    -> `status: screened`

**Stop there.** Stages 6, 7 and 9 — redundancy clustering, multivariate
selection, operating-point re-ranking — compare features against each other and
are impossible from inside one shard. Leave `shap_rank` empty and return
`screened`. The coordinator runs those across all shards. A `shipped` status or
a `shap_rank` written by a shard worker is wrong by construction.

**If you get stuck**, the answer is `blocked` with a reason, never a guess. A
shard that returns 44 blocked rows with precise reasons is a good outcome; it
tells the coordinator exactly what to unblock.
"""

WORKED = """\
## Worked examples — copy these shapes

```csv
feature_name,status,drop_reason,iv,psi_12m,coverage_pct,shap_rank
ip24__d_pan__dcnt__w1h,screened,,0.184,0.031,97.4,
ip24__cb_rate__rate__w30d,dropped,stage 4 low IV - 0.0021,0.0021,0.044,96.1,
ip24__cb_amt__mean__w30d,dropped,stage 3 coverage - 11.2% cliff at m9,,0.402,11.2,
ip24__champ_score__mean__w30d,blocked,input unbound: champion_score,,,,
ip24__d_receiver__dcnt__w30d,dropped,stage 2 support - median n = 4,,,,
```

Read those five rows carefully, because each one demonstrates a rule:

- **screened** carries stage-3 AND stage-4 evidence, and an empty `shap_rank`.
  That is what a completed shard row looks like.
- **dropped at stage 4** still carries the `iv` it was dropped for. The reason
  names a measurement, so the measurement must be present.
- **dropped at stage 3** has coverage and PSI but NO `iv` — the row never
  reached stage 4, so inventing one would be a lie about work you did not do.
- **blocked** carries no numbers at all. Nothing was measurable, so nothing is
  claimed.
- **dropped at stage 2** likewise carries no stage-3 metrics. Support failed
  first.

The pattern: the columns you fill are exactly the stages you actually ran, and
`drop_reason` names the stage that stopped you.
"""


def status_table(spec):
    rows = ["| status | terminal | requires | meaning |", "|---|---|---|---|"]
    for s in spec["queue_statuses"]:
        req = ", ".join(f"`{c}`" for c in s["requires"]) or "—"
        meaning = " ".join(s["meaning"].split())
        rows.append(f"| `{s['id']}` | {'yes' if s['terminal'] else 'no'} | {req} | {meaning} |")
    return "\n".join(rows)


def task_card(key, by, rows, spec, shard_no, shard_total):
    comps = {c["id"]: c for c in spec["composites"]}
    # The catalog already carries the rendered display name (composites render as
    # "A x B"); prefer it over re-deriving one from the registry.
    label = rows[0].get("entity") or key
    hypothesis = comps.get(key, {}).get("hypothesis")

    windows = sorted({r["window"] for r in rows})
    measures = sorted({r["measure"] for r in rows})

    out = [
        f"# Shard {shard_no}/{shard_total} — `{key}`",
        "",
        f"**Scope:** {len(rows)} candidate features on **{label}**"
        + (f", grouped by `{by}`." if by != "entity_id" else "."),
        "",
    ]
    if hypothesis:
        out += [f"**Why this entity exists:** {hypothesis}", ""]
    out += [
        f"**Windows in this shard:** {', '.join(f'`{w}`' for w in windows)} — "
        f"so you are building {len(windows)} aggregate panel(s), and every row reads "
        f"from one of them. Build each panel once.",
        "",
        # Measure names contain commas ("CB rate, fraud reason group"), so they are
        # backticked rather than comma-joined into an unreadable run-on.
        "**Measures:** " + " · ".join(f"`{m}`" for m in measures[:12])
        + (f" *(+{len(measures) - 12} more in queue.csv)*" if len(measures) > 12 else ""),
        "",
        "Your rows are in `queue.csv` beside this file. Fill it in and return it.",
        "Do not add rows, do not delete rows, do not reorder them.",
        "",
        "---",
        "",
        NON_NEGOTIABLES,
        "---",
        "",
        PROCEDURE,
        "---",
        "",
        "## Status vocabulary",
        "",
        status_table(spec),
        "",
        "---",
        "",
        WORKED,
        "---",
        "",
        "## Before you return",
        "",
        "```bash",
        "python validate_queue.py queue.csv --strict",
        "```",
        "",
        "It checks that the evidence each row claims actually exists: a status "
        "requiring `iv` with an empty `iv`, a stage-4 metric with no stage-3 metric, "
        "a drop naming a stage whose measurement is missing, duplicate ranks, or "
        "identical metric triples repeated across distinct features. Fix what it "
        "reports rather than working around it — every check corresponds to a claim "
        "that contradicts itself.",
        "",
        "Return `queue.csv` plus a short note listing anything you marked `blocked` "
        "and what would unblock it.",
        "",
    ]
    return "\n".join(out)


def build(by="entity_id", out="shards", candidates="candidates.csv", max_rows=None):
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        spec = yaml.safe_load(f)

    cpath = os.path.join(ROOT, candidates)
    if not os.path.exists(cpath):
        print(f"{cpath} not found. Generate it first:\n    make catalog", file=sys.stderr)
        return 1
    with open(cpath, newline="") as f:
        rows = list(csv.DictReader(f))
    if by not in rows[0]:
        print(f"--by {by!r} is not a column in {candidates}. "
              f"Available: {', '.join(rows[0])}", file=sys.stderr)
        return 1

    groups = defaultdict(list)
    for r in rows:
        groups[r[by]].append(r)

    # Split oversized groups rather than silently shipping a shard too big to work.
    if max_rows:
        split = {}
        for k, v in groups.items():
            if len(v) <= max_rows:
                split[k] = v
            else:
                for i in range(0, len(v), max_rows):
                    split[f"{k}__part{i // max_rows + 1}"] = v[i:i + max_rows]
        groups = split

    outdir = os.path.join(ROOT, out)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)

    keep = ["feature_name", "availability", "leakage_risk", "min_support_n",
            "adversarial_halflife", "hypothesis",
            "status", "drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank"]

    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    index = ["# Shard index", "",
             f"{len(rows)} candidates split into **{len(ordered)} shards** by `{by}`.",
             "",
             "Shards are independent for stages 1-4 only. Redundancy clustering (6),",
             "multivariate selection (7) and operating-point re-ranking (9) compare",
             "features across shards and must be run by the coordinator after all",
             "shards return.",
             "",
             "| # | shard | rows | panels |", "|---|---|---|---|"]

    for i, (key, grp) in enumerate(ordered, 1):
        d = os.path.join(outdir, key)
        os.makedirs(d)
        with open(os.path.join(d, "queue.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keep, extrasaction="ignore")
            w.writeheader()
            w.writerows(grp)
        with open(os.path.join(d, "TASK.md"), "w") as f:
            f.write(task_card(key, by, grp, spec, i, len(ordered)))
        panels = len({r["window"] for r in grp})
        index.append(f"| {i} | `{key}` | {len(grp)} | {panels} |")

    with open(os.path.join(outdir, "INDEX.md"), "w") as f:
        f.write("\n".join(index) + "\n")

    sizes = [len(g) for _, g in ordered]
    card = os.path.getsize(os.path.join(outdir, ordered[0][0], "TASK.md"))
    qcsv = os.path.getsize(os.path.join(outdir, ordered[0][0], "queue.csv"))
    print(f"wrote {outdir}/ — {len(ordered)} shards by {by}, "
          f"{min(sizes)}-{max(sizes)} rows each")
    print(f"  a worker loads ~{(card + qcsv) / 4000:.1f}k tokens instead of the full pack")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Split the queue into self-contained packets.")
    ap.add_argument("--by", default="entity_id",
                    help="column to shard on (default entity_id — shares aggregate panels)")
    ap.add_argument("--out", default="shards")
    ap.add_argument("--candidates", default="candidates.csv")
    ap.add_argument("--max-rows", type=int, default=None,
                    help="split any shard larger than this")
    a = ap.parse_args(argv)
    return build(by=a.by, out=a.out, candidates=a.candidates, max_rows=a.max_rows)


if __name__ == "__main__":
    sys.exit(main())
