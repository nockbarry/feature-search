# Label Definition — chargeback edge cases

**Status:** required before training. Every unresolved question here is a silent inconsistency between the feature pipeline and the target.

`feature_space.yaml` defines each label in one line. Production needs the full decision for every branch of the dispute lifecycle. The failure mode is subtle: the feature pipeline and the training target each make their own implicit choice, the two disagree, and the resulting noise looks like a modeling problem rather than a definitional one.

---

## 1. Reason-code mapping

The primary split is fraud vs non-fraud, and it drives the two-population strategy in `AGENT_BRIEF.md` §6 — these groups have near-disjoint predictors, so getting the mapping wrong contaminates both models.

| Group | Contains | Predicted by |
|---|---|---|
| `cb_fraud` | Card-absent fraud, counterfeit, lost/stolen, no-cardholder-authorization | Device, geo, velocity, graph, consistency |
| `cb_nonfraud` | Not received, not as described, duplicate processing, credit not processed, cancelled recurring | Tenure, delivery, refund history, prior disputes, contact history |
| `cb_excluded` | Processing errors, authorization errors, currency mismatch | Neither — these are operational, not behavioural |

**Decisions to record:**

- **Where does "credit not processed" go?** Genuinely ambiguous: sometimes merchant error, sometimes first-party abuse. Pick one, or make it a third class.
- **Networks re-map codes.** Visa CE3.0 and equivalent changes have re-partitioned dispute categories before and will again. Store the **raw** reason code and version the mapping in this file; never overwrite historical labels with a new mapping without re-training and re-baselining.
- **Cross-network normalization.** Visa, Mastercard, Amex and Discover use different code systems. The mapping table must be per-network, and a code that exists in one and not another is a decision, not a null.
- **Fraud codes are under-reported.** Some issuers file recoverable disputes under service codes to avoid fraud-reporting overhead. If TC40/SAFE says fraud and the CB reason says "not received," trust TC40 — record this as an explicit override rule.

---

## 2. Lifecycle edge cases

Each needs an answer. "We'll handle it later" means the pipeline is already making a choice for you.

**Representment won.** Merchant disputes the chargeback and wins; funds are returned. Is the transaction still a positive?

> Recommended: **yes, still positive for `cb_fraud`**. The cardholder disputed it and the fraud signal was real; recovery is a financial outcome, not evidence of non-fraud. For `cb_nonfraud` the answer is arguably no, since a won representment often means the dispute was invalid. Whatever you choose, the label must not flip after the fact — see §3.

**Second presentment / pre-arbitration / arbitration.** Multi-round disputes can run 90+ days. The `label_arrival_ts` for point-in-time purposes is the **first** notification, not the final resolution. Otherwise features go blind for months on transactions you already knew were bad.

**Multiple chargebacks on one transaction.** Partial disputes can arrive separately. Deduplicate to one label per transaction; take the earliest `label_arrival_ts` and the **maximum severity** reason code, not the first one.

**Partial chargeback.** `cb_amount < amount`. Count-based rates treat it as a full positive; dollar-based rates use the disputed amount only. This is one reason count and dollar rates diverge — expected, but state it.

**Chargeback on a refunded transaction.** Usually double-dipping or a timing race. Frequently first-party abuse. Flag separately rather than folding into either group.

**Chargeback after a reversal or cancellation.** Should not exist. If it does, it is a data-quality issue — count and alert, do not silently include.

**Chargeback on a transaction that never settled (`DEN`/`ERR`).** Should be impossible; only `SEN` can charge back. Non-zero counts mean a join defect or a disposition-mapping error. Assert this in the pipeline — see §5.

**Fraud confirmed by other means, no chargeback filed.** Confirmed ATO, mule account closed, review-confirmed fraud. These are genuine positives that the CB label misses. Decide whether they enter the target; if they do, the maturity profile changes and the emergence curve no longer applies uniformly.

---

## 3. Labels must be immutable once stamped

The dispute lifecycle means a transaction's status can change months later. If you mutate historical labels in place:

- Point-in-time correctness breaks — the whole `label_arrival_ts` mechanism assumes each fact has one arrival time.
- Backtests stop being reproducible; the same code on the same date range returns different numbers.
- Model comparisons across time become invalid, since the champion was trained against a different truth than the challenger.

**Append, never update.** Each lifecycle event is a new row with its own `event_type` and arrival timestamp. The label at any point in time is a fold over events with `arrival_ts < as_of`. This makes representment reversals expressible without rewriting history.

---

## 4. Maturity differs by group

The 30-day / 90% figure is for `cb_fraud`. Non-fraud disputes mature substantially more slowly — "not received" cannot be filed until an expected delivery date passes, so the curve is shifted and flatter.

**Fit and store a separate emergence curve per reason group.** Applying the fraud curve to non-fraud cohorts systematically understates them. This also matters for the split protocol: the embargo `H` should use the **slowest** group in the target, not the fastest.

---

## 5. Pipeline assertions

Cheap, and each catches a class of silent corruption:

```sql
-- no chargeback on unsettled traffic
SELECT count(*) FROM chargeback cb JOIN txn t USING (txn_id)
WHERE t.disposition <> 'SEN';                                  -- expect 0

-- no label arriving before its event
SELECT count(*) FROM v_label_arrival WHERE label_arrival_ts < event_ts;  -- expect 0

-- one label row per transaction after dedup
SELECT count(*) FROM (SELECT txn_id FROM label GROUP BY 1 HAVING count(*) > 1) x;  -- expect 0

-- disputed amount never exceeds the transaction
SELECT count(*) FROM chargeback cb JOIN txn t USING (txn_id)
WHERE cb.cb_amount > t.amount * 1.0001;                        -- expect 0

-- every reason code is mapped
SELECT DISTINCT reason_code FROM chargeback
WHERE reason_code NOT IN (SELECT reason_code FROM reason_code_map);  -- expect empty
```

The last one is the one that breaks quietly: an unmapped new code defaults to whatever your `CASE` fallback does, and a whole fraud category silently becomes negatives.

---

## 6. Decision record

Fill this in and treat it as the authoritative answer. Blank rows are unresolved risk.

| Question | Decision | Owner | Date |
|---|---|---|---|
| "Credit not processed" → fraud / non-fraud / third class | | | |
| Won representment stays positive for `cb_fraud`? | | | |
| Won representment stays positive for `cb_nonfraud`? | | | |
| `label_arrival_ts` = first notification | | | |
| Multiple CBs → earliest ts, max severity | | | |
| Partial CB → full positive on count, partial on dollars | | | |
| CB-on-refund → separate flag | | | |
| Non-CB confirmed fraud in target? | | | |
| TC40 overrides CB reason code when they conflict | | | |
| Reason-code map version in force | | | |
| Emergence curve fitted per reason group | | | |

---

## 7. Related

- `docs/split_protocol.md` — maturity horizon `H` comes from the slowest group here
- `pit_aggregate_template.sql` — `<fraud_reason_codes>` placeholder resolves from §1
- `AGENT_BRIEF.md` §6 — the two-population modeling strategy this mapping enables
