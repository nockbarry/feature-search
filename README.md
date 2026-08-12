# feature-search

Generative feature-space search for a **chargeback fraud classification model**.

Enumerates the reachable feature space from a slot grammar, screens it down to a shippable set, and enforces the correctness properties — point-in-time labels, serving parity, support floors — that decide whether a feature is real or an artifact of hindsight.

Built for a pre-authorization block/pass decision over a `SEN` / `DEN` / `ERR` / `CB` funnel, where chargebacks reach ~90% maturity at 30 days.

---

## Quickstart

```bash
make install          # runtime + dev deps
make workbook         # feature_catalog.xlsx at the L0 probe rung
make check            # tests + lint
```

Then open `AGENT_BRIEF.md`. Everything else is referenced from it.

```bash
make catalog LEVEL=L1     # a finer resolution rung
make expand  LEVEL=L1     # stage B/C, needs survivors.txt from the screen
```

---

## Repo map

| Path | Role |
|---|---|
| `AGENT_BRIEF.md` | **Start here.** Mission, grammar, correctness rules, pruning funnel, definition of done. |
| `feature_space.yaml` | Source of truth: six slot registries, resolution ladder, labels, typologies. **Edit this.** |
| `validate.py` | Schema and invariant checks on the YAML. Runs before every expansion. |
| `expand_catalog.py` | Expands the YAML into `candidates.csv`. Flags: `--level`, `--max-tier`, `--expand`, `--survivors`. |
| `nongrid_features.py` | 143 hand-enumerated features the grammar cannot express. Expected to carry most of the lift. |
| `build_workbook.py` | Generates `feature_catalog.xlsx` — the agent's work queue. |
| `pit_reference.py` | Executable spec of the two-clock rule. Mirrors the SQL; testable in CI. |
| `pit_aggregate_template.sql` | Warehouse implementation: PIT aggregation, shrinkage, censoring correction, parity harness. |
| `docs/split_protocol.md` | Embargo and purging under label maturity. |
| `docs/null_semantics.md` | Null policy and feature-outage behaviour. |
| `docs/label_definition.md` | Reason-code mapping and dispute-lifecycle edge cases. |
| `tests/` | Compatibility rules, naming contract, determinism, spec validation, and the leakage fixtures. |

**`candidates.csv` and `feature_catalog.xlsx` are build artifacts.** They are gitignored and CI fails if either is committed. Edit the YAML and regenerate.

---

## How the search works

Aggregate features are one point in a six-slot product:

```
AGGREGATION( MEASURE ) over ( ENTITY , WINDOW , FILTER ) -> TRANSFORM
```

Enumerate the slots exhaustively, then prune — the search is a loop, not a memory exercise. Compatibility rules kill the nonsensical combinations (a 5-minute chargeback rate, distinct-devices-per-device), leaving **45,552** viable combinations across all tiers.

Two ideas keep that tractable:

**Coverage vs resolution.** Entities, measures and the non-grid families are *coverage* — a missing quantity is a blind spot no resolution recovers, so they are never gated. Windows, transforms, filters and entity granularity are *resolution* — start coarse, refine on evidence. The `L0` probe rung is **59× cheaper** than `L2` through the full pipeline.

**Staged expansion.** Stage A (base grid) is screened first; transform and filter variants expand only onto survivors. Expanding first multiplies screening cost ~10× for zero coverage gain.

Why coarsening is safe: for nested count windows under a Poisson arrival process, `corr(N_short, N_long) = √(w_short/w_long)`, so windows closer than ~×4 are >0.5 correlated and redundancy screening deletes them anyway. Real streams are burstier, so treat that as a floor and measure your own.

---

## Non-negotiables

**The two-clock rule.** Aggregates filter on `label_arrival_ts` (when we learned it), never `event_ts` (when it happened). A chargeback on a 01-Jun transaction reported 03-Jul must not appear in a 15-Jun entity rate. This is the most common leak in fraud modeling; it inflates offline AUC and concentrates in the champion/challenger disagreement region, flattering challengers specifically. `tests/test_pit_leakage.py` asserts it against hand-computed fixtures — including a deliberately leaky implementation kept in the tree so the suite can prove the correct one differs from it.

**Serving parity.** Offline stores get mutated after the fact. Diff shadow-computed values against offline recomputation on the same transactions; that diff *is* the training/serving skew metric. Run it before reporting any backtest, not after.

**Censoring.** Entity outcome rates are computable only over approved traffic, so a heavily-blocked entity looks clean. Carry `block_rate` alongside every `cb_rate` and de-bias with release-program weights, or the model relearns the champion's blind spots.

**Never coalesce a rate to zero.** Zero means "we looked and found no fraud." Null means "we have never seen this entity." For a new entity those are opposite risk statements.

---

## Testing

```bash
make test
```

60 tests. The ones that matter:

- `test_pit_leakage.py` — every expected value hand-computed from the fixture and stated in the test docstring. Checks against ground truth, not against last week's output.
- `test_compatibility.py::test_coverage_is_never_gated_by_resolution` — enforces the contract that resolution rungs prune windows and sibling granularities but never remove a measure or an entity class.
- `test_validate.py` — every check is proven to reject a specific known-bad spec, so the validator cannot pass everything and be mistaken for coverage.
- `test_determinism.py` — regeneration must be byte-identical, or diffs become unreadable and the agent's fills can no longer be matched to their features.

---

## Note on visibility

A completed fraud feature catalog is an evasion checklist: it names every signal computed, at what window, with what support floor, plus the vendor stack. Keep methodology public and instantiation private — real thresholds, vendor names, importance rankings and populated artifacts do not belong in a public repo.
