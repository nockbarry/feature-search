# Null Semantics and Feature-Outage Policy

**Status:** required before any feature with an external or optional input ships.

Nulls in a fraud model are not a data-quality nuisance to be imputed away. They are (a) a live production incident vector and (b) frequently a signal in their own right. Both need explicit policy, per feature, written down.

---

## 1. Why this causes incidents

A GBM sends nulls down a default branch learned at training time. When a vendor enrichment starts timing out at 09:00, every affected transaction's score shifts in the same direction at the same moment. The model does not error. Nothing alerts. The block rate moves, the swap sets change composition, and the first symptom is a guardrail tripping hours later with no obvious cause.

The failure is worse during an attack, because attack traffic and enrichment failures correlate: bot traffic is more likely to be missing device telemetry, and a traffic spike is more likely to exhaust vendor rate limits.

---

## 2. Nulls are informative — do not impute them away

Missingness is often the signal:

| Null | What it usually means |
|---|---|
| Device fingerprint absent | JavaScript blocked or stripped — automation |
| Client telemetry absent | Headless browser, or the SDK was removed |
| Email-age enrichment absent | Domain does not resolve, or is too new to have a record |
| AVS/CVV response absent | Merchant did not request it — different risk regime |
| Billing address absent | Guest checkout path |
| Phone-intelligence absent | Number is not in any carrier registry |

Median-imputing a missing device fingerprint tells the model "typical device" when the truth is "this client refused to identify itself." That is not neutral, it is backwards.

**Default policy: do not impute. Pass nulls to the model natively and add an explicit missingness indicator.**

The exception is a feature where missingness is purely operational (a batch job was late) and carries no behavioural meaning. Those are rare. State the reason when you claim one.

---

## 3. Two kinds of null, kept apart

The catalog must distinguish these, because they mean opposite things:

**Structural null** — the value cannot exist. A brand-new device has no 30-day history. Correct value is *unknown*, and the model should learn what unknown implies (usually: elevated risk, which is why `is_first_txn` features exist).

**Operational null** — the value exists but we could not fetch it. Vendor timeout, cache miss, upstream outage. Correct handling is a *fallback*, and the rate of these should be near zero and alerted on.

The reference implementation returns `None` rather than `0.0` for an empty denominator (`tests/test_pit_leakage.py::test_no_history_returns_none_not_zero`) precisely so that structural nulls stay distinguishable. **Never coalesce a rate to zero.** Zero means "we looked and found no fraud"; null means "we have never seen this entity." For a new entity those are opposite risk statements.

---

## 4. Required per-feature declarations

Every feature in the catalog carries:

| Field | Values | Meaning |
|---|---|---|
| `null_semantics` | `structural` / `operational` / `both` | which kind can occur |
| `null_encoding` | `native` / `sentinel:<v>` / `impute:<method>` | how it reaches the model; `native` is the default |
| `missingness_indicator` | yes / no | emit a companion `__is_null` feature |
| `expected_null_rate` | float | steady-state baseline |
| `null_alert_threshold` | float | page when the rolling rate exceeds this |
| `outage_policy` | `fail_open` / `fail_closed` / `degrade` | see below |

---

## 5. Outage policy

When an input is unavailable at scoring time:

**`fail_open`** — score without it, accept degraded accuracy. Correct for features that are nice-to-have and where blocking on unavailability would reject large volumes of good traffic. Most enrichment-tier features.

**`fail_closed`** — treat unavailability as elevated risk (route to review, or apply a risk premium). Correct where absence is itself suspicious and cheaply faked — client telemetry, device fingerprint. Note the adversarial property: `fail_open` on a device fingerprint tells attackers to simply block the SDK.

**`degrade`** — fall back to a coarser sibling. The entity-granularity hierarchy already gives you this: `ip` → `ip24` → `asn`, `pan` → `bin8` → `issuer`, `device` → `device_model`. A stale cached value with an explicit staleness feature is usually better than a null.

**Decide `fail_open` vs `fail_closed` per feature, and check the aggregate.** If every feature independently fails open, a total enrichment outage silently converts your model into a much weaker one that keeps blocking at the same threshold. Add a **vector-level** rule: if more than *k* features are null, route to review rather than scoring.

---

## 6. Monitoring

Null rate is a first-class monitored metric, alongside PSI and score drift:

- Per feature, rolling hourly null rate against its `expected_null_rate`, with control limits from its own pre-period distribution (same method as the guardrail board — not folk thresholds).
- Per vendor, aggregate null rate — catches one integration failing before individual features breach.
- **Score distribution conditioned on null pattern.** The real question is not "how many nulls" but "did the null pattern change the score distribution," which is what actually moves the block rate.
- Null rate by segment. A vendor that only covers one geography produces a segment-specific null cliff that global monitoring misses entirely.

---

## 7. Training-time obligations

**Train on realistic missingness.** If training data was assembled from a warehouse where enrichment eventually succeeded via retries, the model never learns the null branch, and the first production outage is out-of-distribution. Either train on logged scoring-time values (preferred — this is another argument for shadow-mode feature logging) or inject missingness matched to production rates.

**Missingness must be point-in-time too.** A field backfilled after the fact was null at decision time. Filling it in training is the same class of bug as the two-clock violation, and it is easy to miss because nothing about it looks like a label.

**Check null-rate stability across the split.** A feature whose null rate changed between train and validation windows (vendor onboarded mid-window, SDK version rolled out) will produce misleading importance. Add it to the stage-3 stability screen alongside PSI.

---

## 8. Checklist

- [ ] Every catalog feature has all six null fields populated.
- [ ] No rate feature coalesces to zero; empty denominators are `None`.
- [ ] Missingness indicators emitted wherever missingness is behavioural.
- [ ] Vector-level rule defined for "too many nulls at once."
- [ ] Null rates monitored per feature, per vendor, per segment, with alerts.
- [ ] Training data reflects production missingness rates.
- [ ] Null-rate stability included in the stage-3 stability screen.
- [ ] Outage runbook names the fallback per vendor and who owns the call.

---

## 9. Related

- `AGENT_BRIEF.md` §7 — correctness rules
- `docs/split_protocol.md` — the label-side analogue of point-in-time discipline
- Workbook → Leakage Audit — availability and serving-parity checks
