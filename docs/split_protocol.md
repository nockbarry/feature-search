# Split Protocol — embargo and purging under label maturity

**Status:** required reading before any backtest number is reported.

CBs in this segment reach ~90% maturity at day 30 and ~95%+ by day 45. That single fact invalidates the default train/validation split, and it does so in a direction that is easy to miss because it makes results look *better*, not worse.

---

## 1. The failure

Split at time `T`: train on everything before, validate on everything after.

**In the training set.** Transactions in the final ~45 days before `T` have immature labels. Chargebacks that will arrive have not arrived yet, so those rows are recorded as negatives. The label noise is not random — it is perfectly correlated with recency. The model learns a real pattern in the data you gave it:

> recent ⇒ safe

Every feature that correlates with recency (entity age, first-seen flags, anything with a `life` window, anything with a trend or acceleration transform) picks this up. The model then generalizes it to genuinely recent transactions at scoring time, which are exactly the ones it must judge.

**In the validation set.** If validation runs to the analysis date, its most recent cohorts are also immature, so measured CB rates are understated and measured model performance is inflated. Both errors point the same way.

**In random K-fold.** Worse still. A fold boundary that splits an entity's transaction history lets the model see an account's later outcomes while predicting its earlier ones. Combined with entity aggregates, this is direct target leakage.

---

## 2. The protocol

### 2.1 Time-ordered splits only

No random K-fold anywhere in the pipeline, including hyperparameter search and feature screening. Fraud is non-stationary and adversarial; a random fold measures interpolation when the deployed problem is extrapolation.

### 2.2 Purge the immature tail of training

Training data ends at `T_train_end = T − H`, where `H` is the maturity horizon (**45 days**, the ~95% emergence point, not the 30-day 90% point).

Rows between `T_train_end` and `T` are **discarded, not relabeled**. They are not negatives; their labels are unknown.

### 2.3 Embargo between train and validation

Insert a gap of `H` between the end of training and the start of validation:

```
|<--------- train --------->|<-- purge H -->|<-- embargo H -->|<--- validation --->|
                       T_train_end                     T_val_start
```

The purge removes immature *training* labels. The embargo prevents entity aggregates computed near `T_train_end` from sharing a window with validation transactions — a 30-day device velocity feature computed on the last training day overlaps the first validation days unless you separate them by at least the longest window in the feature set.

**Embargo width = max(maturity horizon, longest feature window).** With `w90d` in the registry that is 90 days, not 45. If the 90-day windows do not survive screening, the embargo can shrink accordingly — record which it is.

### 2.4 Validation cohorts must be mature

Every validation cohort must be at least `H` old at analysis time, **or** its rate must be emergence-adjusted:

```
developed_rate(cohort) = observed_rate / emergence_frac(age_days)
```

using the same fitted emergence curve as the deployment readout. Do not mix matured and developed cohorts in one headline metric without saying so; report them separately.

### 2.5 Purged, embargoed CV if you need multiple folds

For k time-ordered folds, apply the purge and embargo at **both** edges of each validation block, and drop training rows whose feature windows overlap the validation block. Standard `TimeSeriesSplit` does not do this and is not sufficient here.

---

## 3. Cost

At `H = 45` and an embargo of 90 days, each fold boundary consumes ~135 days of data. With 24 months of history that is a real constraint, and it is a legitimate argument for **fewer folds with more data** rather than 5-fold anything.

It is not an argument for shrinking `H`. Shrinking `H` does not buy data; it buys mislabeled data.

---

## 4. Interaction with the feature pipeline

The purge protects the **label**. Point-in-time aggregation protects the **features**. They are separate defences against related failures and you need both:

| | protects | failure if missing |
|---|---|---|
| PIT aggregation (`pit_reference.py`) | feature values | entity rate contains a CB we had not been told about |
| Purge + embargo (this doc) | target labels | recent rows labeled negative because their CBs have not arrived |

A pipeline with correct PIT features and a naive split still learns "recent ⇒ safe."

---

## 5. Checks before reporting any number

- [ ] Training data ends at least `H` before the analysis date, with the tail **discarded**, not relabeled.
- [ ] Embargo ≥ max(`H`, longest feature window) between train and validation.
- [ ] Every validation cohort is either fully matured or emergence-adjusted, and which one is stated.
- [ ] No random K-fold anywhere, including hyperparameter search and feature screening.
- [ ] Recency-correlated features (entity age, `life` windows, trend/acceleration transforms) inspected specifically for a monotone relationship with time-to-analysis-date. That relationship is the fingerprint of this bug.
- [ ] Negative control: shift the split forward by `H` and re-run. Performance should be stable. A large drop means the original split was leaking.

---

## 6. Related

- `pit_aggregate_template.sql`, `pit_reference.py` — feature-side protection
- `tests/test_pit_leakage.py` — the fixtures that prove it
- `AGENT_BRIEF.md` §7 — correctness rules
- `docs/label_definition.md` — what counts as a positive in the first place
