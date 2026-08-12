# Integration — what you provide, and in what order

This repo is a **portable method plus a checklist**. It ships zero facts about
your environment. Everything below is the work of turning it into a search
against your actual data.

If you read one thing first, read this. It is the map.

---

## What you must provide

Three things, in this order. Nothing downstream is meaningful until they exist.

| # | You provide | Into | Gate |
|---|---|---|---|
| 1 | **Your environment** — latency budget, label maturity, funnel states, base rates, warehouse coordinates, governance limits | `spec/instance.yaml` | `python validate_instance.py` fails while any `TODO` remains |
| 2 | **Your schema** — which real column satisfies each of the 42 data requirements | a binding YAML (`python discover.py --template`) | `python discover.py --binding b.yaml` exits non-zero while a `core` requirement is unmet |
| 3 | **Your measurements** — the screening results per candidate feature | `queue.csv` | `python validate_queue.py q.csv --strict` rejects rows whose status claims work that left no evidence |

Everything else in the repo is generated, portable, or a guard on one of those three.

---

## Order of operations

```
  0. read            AGENT_BRIEF.md          the method
                     ↓
  1. instance        spec/instance.yaml      your environment      ← YOU FILL
                     validate_instance.py    refuses TODOs
                     ↓
  2. survey          make survey             generates BigQuery from your instance
                     sql/survey/01..06       run these against your warehouse
                     ↓
  3. bind            DISCOVERY_CHECKLIST.md  42 requirements, ~324 aliases
                     discover.py --binding   what your findings unlock      ← YOU FILL
                     ↓
  4. catalog         make catalog            the candidate features that are now reachable
                     ↓
  5. shard           make shards             27 self-contained worker packets
                     ↓
  6. screen          (the workers)           stages 1–4 per shard            ← YOU FILL
                     validate_queue.py       rejects unevidenced claims
                     ↓
  7. coordinate      (you)                   stages 6, 7, 9 across all shards
```

Steps 2 and 3 are the ones people skip, and skipping them is why a feature
search produces a beautiful backtest and an unshippable model.

---

## Step 2 in detail — surveying BigQuery

`make survey` reads `spec/instance.yaml` and writes six queries into
`sql/survey/`. They are all `SELECT`s; nothing mutates.

| Query | Question it answers | Cost |
|---|---|---|
| `01_inventory.sql` | What tables exist, how big, how fresh? | metadata only |
| `02_column_search.sql` | Which columns might satisfy which requirement? | metadata only |
| `03_profile.sql` | Is this column usable — nulls, cardinality, when it started being populated? | scans, use a lookback |
| `04_stability.sql` | Month by month: coverage cliffs, cardinality collapse, schema changes | scans |
| `05_entity_key.sql` | Is this a stable entity key or a per-session id? | scans |
| `06_join_rate.sql` | Does this enrichment actually cover live traffic? | scans |

**`02_column_search.sql` is the one to run first.** It matches all ~324 aliases
from `spec/data_requirements.yaml` against `INFORMATION_SCHEMA.COLUMNS` across
your datasets, and tags each hit with the requirement it might satisfy and how
much of the catalog that requirement unlocks. It is a **recall** tool — expect
false positives and prefer them. A wrong hit costs one profile query; a miss
costs a whole feature family.

The **requirements with zero hits are the finding.** A `core` requirement with
no match blocks the search outright.

### Then judge each candidate, do not just bind it

A column that exists and joins can still be unusable. Queries 03–06 measure
against the `fitness` thresholds in `spec/instance.yaml`:

- **Null rate** above `max_null_rate_pct` — it is a partial field, and the gap
  is rarely random.
- **First populated** much later than first event — the field was added
  mid-history. Your usable history starts there, not at table creation.
- **Coverage cliff** in `04_stability` — a schema or vendor change. History
  either side of it is not comparable, which breaks both windows and PSI.
- **Cardinality collapse** — an upstream dedupe or id-scheme change.
- **Entity-to-session ratio** above `max_entity_to_session_ratio` in
  `05_entity_key` — **this is a session cookie, not an entity key.** Binding it
  as a device fingerprint is the most damaging mistake available here: it looks
  correct in every schema browser, and every device-keyed feature silently
  becomes worthless with no error anywhere.
- **Join rate** below `min_join_rate_pct` — a stale enrichment table whose
  misses correlate with newness, which correlates with risk. Worse than not
  having it.

Record each requirement as `found` (with `verified: true` only after the checks
above), `absent`, or `unknown`. Those three are not interchangeable: `absent` is
a design constraint, `unknown` is work outstanding, and an unverified `found`
does not count toward coverage.

---

## What the repo gives you back

| Artifact | Command | For |
|---|---|---|
| `DISCOVERY_CHECKLIST.md` | `make checklist` | what to look for, what it is called, how to verify |
| `sql/survey/` | `make survey` | runnable BigQuery for your project |
| coverage report | `discover.py --binding` | which entities, measures and families your findings unlock |
| `candidates.csv` | `make catalog` | the candidate features |
| `pack/` | `make pack` | ~90k-token handover for a planning agent |
| `shards/` | `make shards` | 27 packets, ~3k tokens each, for worker agents |
| `feature_catalog.xlsx` | `make workbook` | the human fill-in surface |

---

## Integrating with an agent system

The repo assumes a **planner plus workers** split, and the two handovers are
different artifacts on purpose:

- **`pack/` goes to the planner.** Full method, discovery layer, correctness
  files, whole queue. It owns instance, binding, and the cross-shard stages.
- **`shards/` go to the workers.** Each packet is self-contained: it restates
  the two-clock rule, the anti-fabrication contract, the status vocabulary, the
  per-row procedure and five worked examples verbatim, alongside its own ~44
  rows. No packet refers to a document the worker does not have.

**What a worker cannot do, and must be told it cannot:** stages 6, 7 and 9
(redundancy clustering, multivariate selection, operating-point re-ranking)
compare features against each other and are meaningless within one shard. Every
task card says to leave `shap_rank` empty and return `screened`. A `shipped`
status written by a shard worker is wrong by construction.

**The failure mode to design against** is not a wrong answer, it is a plausible
one. `iv`, `psi_12m`, `coverage_pct` and `shap_rank` are empty numeric columns,
and a fast model instructed to fill a column will fill it. `validate_queue.py`
is the guard: it does not ask whether a number is plausible, it asks whether the
evidence a row claims exists. Run it on everything that comes back.

Whatever prompt each worker receives should restate the two-clock rule and the
never-invent-a-number contract inline. Do not rely on a worker having read a
section of a long document — that is exactly what the shard packets exist to
avoid.

---

## What this repo does not decide for you

Named here so nobody assumes they are handled: reject-inference method,
cost-sensitive objective and class weighting, calibration protocol, the minimum
release rate that keeps the policy feedback loop broken, per-segment performance
floors, and the governance set (model card, fairness protocol, adverse-action
codes, retraining triggers, red-team plan, runbooks).

Each is a decision with materially different variants. Picking one silently
would be worse than the gap. See the "Not yet written" section of
`AGENT_BRIEF.md`.
