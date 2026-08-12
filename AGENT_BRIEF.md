# Feature Search — Agent Brief
### Chargeback fraud classification · challenger model `dnn_v50_rc3`

---

## 1. Mission

Search the reachable feature space for a chargeback-fraud model exhaustively, screen it down to a shippable set, and prove that every survivor is correct, servable, and durable.

You are **not** being asked to build a model. You are being asked to produce a defensible, audited feature set plus the evidence that it is free of leakage and computable at decision time.

**Deliverables, in order:**

1. `feature_catalog.xlsx` fully populated — every candidate has a terminal status, every drop has a reason.
2. Built feature implementations for all survivors, in the serving path's own code.
3. A leakage audit signed off per the workbook's Leakage Audit sheet.
4. A serving-parity report: offline-vs-shadow value diff for the shipped vector.
5. A data-request list for inputs we do not yet have, with owners.
6. An updated `feature_space.yaml` reflecting anything you added to the registries.

---

## 2. Context you need

**The model.** A challenger to production champion `gbm_v412`, scoring pre-authorization, deciding block vs pass. Champion currently blocks ~4.2% of dollar volume.

**The funnel.** A passed transaction reaches one of: `SEN` (settled), `DEN` (denied by downstream rules), `ERR` (errored/declined at the financial institution). Only `SEN` can charge back (`CB`).

**Label maturity.** CBs in this segment reach ~90% of ultimate volume by day 30. That means: any aggregate over a window ending within ~45 days of today is under-counting, and any *feature* built from recent labels needs the same emergence-adjustment discipline as the model evaluation does.

**The censoring problem, which shapes everything.** Champion-blocked transactions have no outcome. Entity outcome rates are therefore computable only over traffic the champion approved. An entity that the champion blocks heavily looks clean. This means:
- Every outcome-rate feature partially encodes *current policy*, not just risk.
- Features derived from entity history are the ones most distorted by the champion's blind spots.
- **Use the release-program labels** (champion-blocked transactions sampled and released, carrying `sample_weight`) to de-bias entity rates in the blocked region. Without this the challenger relearns the champion's blind spots and the feature set has a ceiling it cannot see.

**Latency budget.** p99 of **40 ms** for the entire feature vector, not per feature.

**Decision point.** Pre-authorization. Anything whose input arrives after the authorization decision (delivery confirmation, refund outcome, dispute flag) is a later-lifecycle feature and must be tagged as such, not silently included.

---

## 3. The generative grammar

Nearly all aggregate features are one point in a six-slot product:

```
AGGREGATION( MEASURE ) over ( ENTITY , WINDOW , FILTER ) -> TRANSFORM
```

The registries for all six slots live in `feature_space.yaml`. `expand_catalog.py` enumerates them under compatibility rules. **Enumerate the slots exhaustively, then prune** — do not brainstorm features one at a time. The point of writing it this way is that the search becomes a loop over slots rather than a memory exercise.

Current space, as generated:

| | count |
|---|---|
| Compatible slot combinations (all tiers) | 45,552 |
| Stage-A base grid, tier P1 — at L2 (full) | 3,918 |
| Stage-A base grid, tier P1 — at **L0 (default probe)** | **1,167** |
| Deferred to P2 / P3 | 24,068 / 17,566 |
| Non-grid families (hand-enumerated) | 143 |

Stage B (transform variants) and Stage C (filter variants) multiply the base grid roughly tenfold. **Do not expand them up front.** Screen the base grid first, then run `expand_catalog.py --expand --survivors survivors.txt` on what survives stage 4. Expanding before screening multiplies the screening cost with no coverage gain.

Three slot notes that are easy to get wrong:

- **Composite entities are where the ring signal lives.** Single-entity rates are heavily mined already. `device × BIN8`, `IP/24 × merchant`, `receiver_account × BIN8` catch coordinated activity that per-entity aggregates cannot see. Every composite in the registry carries a stated fraud hypothesis; if you add one, add its hypothesis too. Support collapses fast on composites — the min-support floor does most of the pruning for you.
- **`diff_*` filters are usually the signal, not `same_*`.** Transactions on this card at *different* devices, on this device with *different* names, from this IP to *different* receiver countries.
- **The transform slot is where "difference/ratio at different lags" generalizes.** Short/long window ratios (burst index), z-score against the entity's *own* history (turns any count into an anomaly detector), z-score against the *population at the same timestamp* (controls for attack waves and your own upstream rule changes), empirical-Bayes shrinkage, exponential recency-weighting.

---

## 4. Resolution ladder — start coarse

Slots divide into two kinds, and they get opposite treatment:

- **Coverage slots** — entities, measures, and the non-grid families. A missing quantity is a blind spot no resolution recovers. **Never gated. Search exhaustively at every rung.**
- **Resolution slots** — windows, transforms, filters, aggregations, entity granularity. Steeply diminishing returns. **Start at L0, refine on evidence.**

Think of the space as **8,764 distinct (entity, measure, aggregation) triples — 1,026 at tier 1**. Call these *physical quantities*: "distinct cards per device," "CB rate on this BIN×receiver-country." Everything else is resolution on a quantity you already decided to measure.

### Why this is safe on windows

For nested count windows under a homogeneous Poisson arrival process, Cov(N_s, N_l) = Var(N_s), so:

> **corr(N_short, N_long) = √(w_short / w_long)**

| spacing | corr floor |
|---|---|
| ×2 | 0.71 |
| ×4 | 0.50 |
| ×8 | 0.35 |
| ×16 | 0.25 |

Real streams are burstier and autocorrelated, so measured correlation runs *above* this floor — treat it as a lower bound and measure your own. Windows spaced under ~×4 are >0.5 correlated and stage 6 will collapse them anyway. Fine resolution up front pays compute, storage and screening budget to build features you then delete.

Two corollaries worth designing around: **the ratio transform decorrelates** — `{raw_long, ratio_short/long}` carries far more independent signal than two nested raws; and **`w30x7` (30d ending 7d ago) is the disjoint complement of `w7d`**, uncorrelated by construction, so one disjoint pair beats three nested ones.

### The rungs and what they cost

| level | A base | +B transforms | +C filters | total |
|---|---|---|---|---|
| **L0** probe | 1,167 | 242 | 0 | **1,409** |
| L1 standard | 3,016 | 7,128 | 8,734 | 18,878 |
| L2 full | 3,918 | 36,470 | 42,689 | 83,077 |

**L2 is 59× the screening cost of L0.** Most of that spread is the transform and filter expansion, not the windows — which is why B and C are deferred to post-screen regardless of rung.

```bash
python expand_catalog.py --level L0                          # default: the probe
python expand_catalog.py --level L1 --expand --survivors s.txt
```

### Refinement triggers

All must hold before a quantity earns finer resolution. **Check `edge_of_grid` first.**

1. **edge_of_grid** — if the best window for a quantity is the shortest or longest you tested, the optimum is likely *outside* the range. Add an anchor beyond the edge before subdividing inside it. This is where genuinely new signal hides: sub-minute velocity for card testing, multi-year age for synthetic identity.
2. **survived_screen** — the quantity reached stage 7 with non-trivial SHAP. No refinement around something that didn't clear the screen.
3. **scale_gradient** — importance varies materially between adjacent tested scales. If 1h and 30d score alike, the model is reading the entity, not the timescale, and interpolating adds nothing. Refine *between the two that differ*.
4. **marginal_gain** — add it, retrain, require ΔAUC or Δnet-$ above a **pre-registered** threshold on holdout. Record the result either way in the Refinement Log; a logged negative is what stops the next person re-running it.

**Resolution is a per-quantity attribute.** Card-testing velocity wants sub-minute; synthetic-identity age wants multi-year. Never bump a whole family because one quantity needed it.

### Sequencing

Run **L0 across the full coverage space** — all 48 entities, all 37 measures, all 143 non-grid features — rather than L2 on a subset. Breadth on coverage, coarseness on resolution. The failure mode you're avoiding is a beautifully resolved velocity family sitting next to an empty friendly-fraud column in the typology matrix.

That's ~1,167 base features plus 143 non-grid in the first pass: about a week of screening rather than a quarter, and it tells you which of the ~1,000 tier-1 quantities are alive. Everything after is refinement on a known-good shortlist with a logged test per step.

---

## 5. The non-grid families — expect most of the lift here

The grammar cannot express these, and they are the reason a mechanical grid search alone underperforms. `nongrid_features.py` seeds 143 features across ten families. **That list is your starting point, not the finished set.** Minimum counts per family are in the YAML; exceeding them is expected.

| Family | What it catches that the grid cannot |
|---|---|
| **Graph** | Ring fraud. Component size, component point-in-time CB rate, confirmed-fraud nodes within 1–2 hops, component growth in 24h, bridge formation. Invisible to per-entity aggregates, glaring in the graph. |
| **Novelty / age** | First-seen timestamps per entity, and the *interactions*: new device + old card (ATO), aged email + brand-new everything else (synthetic). |
| **Deviation from own baseline** | Separates "risky transaction" from "risky customer." The single best ATO detector family. |
| **Cross-field consistency** | Every pair of location-bearing fields, plus implied travel velocity between consecutive transactions on a card, plus name-similarity scores across cardholder/account/document/email. |
| **Identifier strings** | Bot-generated email batches cluster tightly in edit distance. Canonicalization collisions catch account cycling. |
| **Session / sequence** | Credential-change recency is the strongest single ATO feature most teams lack. Acquisition channel is the most underused feature in fraud generally — fraud concentrates by affiliate. |
| **Client telemetry** | Paste-vs-type on the PAN field is worth the SDK integration on its own. |
| **Instrument / auth** | AVS, CVV, 3DS status and liability shift, funding type, prepaid flag, expiry proximity. |
| **Basket / fulfillment** | Freight-forwarder and reshipper matches, resale liquidity, expedited shipping. |
| **Population context** | Portfolio rates at the current hour. Makes the model robust to attack waves and to *your own* threshold changes. |

---

## 6. Labels — model more than one

Chargebacks are a **mixed label**. Third-party fraud and first-party ("friendly") fraud share almost no predictors. Check the Typology Coverage sheet: as seeded, friendly fraud and refund abuse are the two thinnest rows, and that is a real finding, not an artifact of the seed list.

If first-party fraud is a material share of CB volume, **splitting the label by reason-code group and modeling the two populations separately (or as a multi-task head) will likely yield more lift than any additional entity aggregate.** Treat that as a hypothesis to test early, not a refinement to defer.

Also build against the fast proxy labels — TC40/SAFE (≈7d), lost/stolen decline codes (≈1d), review dispositions (≈3d). They shorten your iteration loop from 45 days to days, and they are useful entity features in their own right.

---

## 7. Correctness rules — non-negotiable

Full checklist on the workbook's Leakage Audit sheet; `pit_aggregate_template.sql` is the reference implementation. The two that matter most:

**The two-clock rule.** Every aggregate must be computed as of *what was known at scoring time*, not what is true about that period in hindsight. A chargeback on a 01-Jun transaction reported 03-Jul must not appear in a 15-Jun entity rate. Store both `event_ts` and `label_arrival_ts`; filter on the latter, strictly less than the scoring timestamp.

This is the single most common leak in fraud modeling. It inflates offline AUC substantially, and — critically for this program — it concentrates in exactly the region where champion and challenger disagree, because leaked fraud signal is what *creates* disagreement. **If an OOT backtest result does not reproduce in shadow mode, look here first.**

**Serving parity.** Offline feature stores get mutated after the fact: labels arrive, records backfill, entities merge. A feature can *exist* offline while its value contains hindsight. The only reliable test is to diff shadow-computed values against offline recomputation on the same transactions. That diff distribution **is** the training/serving skew metric. Run it before reporting any backtest, not after.

The rest, in brief: out-of-fold target encoding (validate by shuffling labels — encoded IV must collapse to ~0); support floors with empirical-Bayes shrinkage toward a named parent entity, with `n` exposed as a companion feature; censoring correction via release weights; latency benchmarked in the serving path; post-transaction inputs tagged; policy-encoding features re-baselined after every threshold change; adversarial half-life tagged on everything; and legal/compliance review on identity- and geo-derived features before they enter a production model.

---

## 8. Adversarial durability

Tag every feature by how cheaply an attacker can change it:

| Tier | Examples | Half-life |
|---|---|---|
| **Free** | email, user-agent, IP via VPN, device model | days–weeks |
| **Cheap** | device fingerprint, phone, customer ID, address | weeks–months |
| **Expensive** | real card, aged account, verified document, payout account | months–quarters |

A model leaning heavily on free-tier features will backtest beautifully and degrade in weeks. Set the retraining cadence from the mix, and track per-feature lift decay and PSI as a standing monitor — not just at build time.

---

## 9. The pruning funnel

Cheapest filter first. Live counts on the workbook's Pruning Funnel sheet.

| # | Screen | Criterion |
|---|---|---|
| 1 | Availability | Computable at decision time within the latency budget |
| 2 | Support | Median n per entity-window cell ≥ `min_support_n` |
| 3 | Stability | 12-month PSI and coverage; no seasonal collapse or coverage cliff |
| 4 | Univariate | IV / mutual information — **run per label, not just the primary** |
| 5 | Expansion | Re-run the expander on stage-4 survivors (`--expand --survivors`); raise `--level` only where the Refinement Log justifies it |
| 6 | Redundancy | Cluster by rank correlation; keep one per cluster — **prefer the cheaper and more adversarially durable member** |
| 7 | Multivariate | Permutation importance / SHAP on holdout, iterative tail drop |
| 8 | Leakage audit | Every applicable check passes; anything suspiciously top-ranked gets manual timeline inspection |
| 9 | Operating-point relevance | Re-rank importance **restricted to the score band around the decision threshold** |

Step 9 is not optional and is routinely skipped. Global importance is dominated by the easy, obvious tail. The features that matter are the ones that discriminate *near the cut*, because that is where the decision actually happens.

---

## 10. Anti-patterns

- **Reporting an offline AUC before running the serving-parity diff.** The number is not meaningful until the diff is clean.
- **Materializing the full slot product.** ~10⁷ rows nobody reads. Stage it.
- **Expanding transforms before screening.** ~10× the screening cost, zero coverage gain.
- **Jumping to L2 because one quantity looked promising.** Refine that quantity, log the test, leave everything else at L0.
- **Adding another velocity aggregate when the Typology Coverage sheet shows an empty column.** Breadth across typologies beats depth within one.
- **Shipping a raw rate on thin support.** Trees memorize it and it looks great until it doesn't.
- **Treating a feature whose input we lack as "dropped."** It is a data request with an owner.
- **Building a composite without a fraud hypothesis.** If you can't say what ring it catches, support will kill it anyway.
- **Assuming a lift number transfers across operating points.** Re-check at the cut.

---

## 11. Definition of done

1. Every candidate row has `status != candidate`; every dropped row has a `drop_reason`.
2. Typology Coverage shows no `GAP` flag, or each gap carries a written waiver.
3. Every shipped feature has passed all applicable leakage checks and names its parent entity for shrinkage.
4. Shadow-vs-offline value diff measured and within tolerance for the full shipped vector.
5. Data Sources sheet has `have_it` and `owner` filled for every row.
6. Shipped set re-ranked at the decision threshold, with the ranking recorded.

---

## 12. Files

| File | Role |
|---|---|
| `AGENT_BRIEF.md` | This document. Start here. |
| `feature_space.yaml` | Source of truth for all six slot registries, labels, typologies. Edit this, never the generated workbook structure. |
| `expand_catalog.py` | `python expand_catalog.py` → `candidates.csv`. Flags: `--level`, `--max-tier`, `--expand`, `--survivors`. |
| `nongrid_features.py` | The 143 seeded non-grid features. Extend it. |
| `build_workbook.py` | Regenerates `feature_catalog.xlsx` from the above. |
| `pit_aggregate_template.sql` | Reference implementation of the two-clock rule, shrinkage, censoring correction, and the parity harness. |
| `feature_catalog.xlsx` | The work queue. Blue cells are yours to fill. |

Regenerate the workbook any time the registries change:

```bash
python expand_catalog.py --level L0 && python build_workbook.py
```
