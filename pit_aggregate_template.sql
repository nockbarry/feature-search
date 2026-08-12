-- =============================================================================
-- pit_aggregate_template.sql
-- Reference implementation of the two-clock rule for entity outcome aggregates.
--
-- THE BUG THIS PREVENTS
--   A chargeback on a 01-Jun transaction, reported 03-Jul, must NOT appear in
--   the device's fraud rate as of 15-Jun. The naive join does exactly that,
--   inflates offline AUC, and — because leaked fraud signal is what creates
--   champion/challenger disagreement — concentrates the inflation in the swap
--   sets, flattering challengers specifically.
--
-- THE RULE
--   Feature values filter on label_arrival_ts (when we LEARNED it),
--   never on event_ts (when it HAPPENED).
--
-- Adapt dialect as needed; written for a Postgres/Snowflake-style engine.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 0. Label arrival: materialize both clocks explicitly. Never derive one.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_label_arrival AS
SELECT
    cb.txn_id,
    t.event_ts                              AS event_ts,          -- clock 1: when it happened
    cb.reported_ts                          AS label_arrival_ts,  -- clock 2: when we learned it
    cb.cb_amount,
    CASE WHEN cb.reason_code IN (<fraud_reason_codes>) THEN 1 ELSE 0 END AS is_fraud_cb,
    CASE WHEN cb.reason_code IN (<fraud_reason_codes>) THEN 0 ELSE 1 END AS is_nonfraud_cb
FROM chargeback cb
JOIN txn t USING (txn_id);
-- Sanity check that must pass before anything downstream is trusted:
--   SELECT count(*) FROM v_label_arrival WHERE label_arrival_ts < event_ts;  -- expect 0


-- -----------------------------------------------------------------------------
-- 1. Point-in-time entity aggregate.
--    Computed AS OF each scoring transaction, using only what was known then.
-- -----------------------------------------------------------------------------
WITH scoring AS (                      -- the rows we are building features for
    SELECT txn_id, device_id AS entity_id, event_ts AS as_of_ts
    FROM   txn
    WHERE  event_ts BETWEEN :train_start AND :train_end
),
hist AS (                              -- eligible history per scoring row
    SELECT
        s.txn_id                                    AS scoring_txn_id,
        s.entity_id,
        h.txn_id                                    AS hist_txn_id,
        h.amount,
        h.disposition,
        -- KEY LINE: a chargeback counts only if we had already been told about it
        CASE WHEN la.label_arrival_ts IS NOT NULL
              AND la.label_arrival_ts < s.as_of_ts        -- strict: not <=
             THEN la.is_fraud_cb ELSE 0 END          AS known_fraud_cb,
        CASE WHEN la.label_arrival_ts IS NOT NULL
              AND la.label_arrival_ts < s.as_of_ts
             THEN la.cb_amount ELSE 0 END            AS known_cb_amount
    FROM scoring s
    JOIN txn h
      ON  h.device_id = s.entity_id
      AND h.event_ts <  s.as_of_ts                   -- strict: exclude the scoring txn itself
      AND h.event_ts >= s.as_of_ts - INTERVAL '30 days'
    LEFT JOIN v_label_arrival la
      ON  la.txn_id = h.txn_id
),
agg AS (
    SELECT
        scoring_txn_id,
        entity_id,
        count(*)                                                  AS n_txn,
        count(*) FILTER (WHERE disposition = 'SEN')               AS n_sen,
        sum(amount) FILTER (WHERE disposition = 'SEN')            AS sen_amount,
        sum(known_fraud_cb)                                       AS n_cb,
        sum(known_cb_amount)                                      AS cb_amount,
        -- policy-encoding companions: carry these with EVERY outcome rate, or the
        -- feature silently means "what the champion let through" (see check 4)
        avg(CASE WHEN disposition = 'DEN' THEN 1 ELSE 0 END)      AS den_rate,
        avg(CASE WHEN disposition = 'ERR' THEN 1 ELSE 0 END)      AS err_rate
    FROM hist
    GROUP BY 1, 2
)
SELECT
    a.scoring_txn_id,
    a.entity_id,
    a.n_txn,
    a.n_sen,                                                  -- expose n: the model
                                                              -- learns how much to trust the rate
    a.den_rate,
    a.err_rate,
    -- raw rate: DO NOT ship this alone below min_support_n
    CASE WHEN a.n_sen > 0 THEN a.n_cb::float / a.n_sen END    AS cb_rate_raw,
    -- empirical-Bayes shrunk rate toward the parent entity (device -> device_model).
    -- k is the prior strength; tune so that n = k gives equal weight to the parent.
    ( a.n_cb + :k * p.parent_cb_rate ) / ( a.n_sen + :k )      AS cb_rate_shrunk,
    p.parent_cb_rate
FROM agg a
LEFT JOIN v_parent_rate_pit p                                 -- see section 2
       ON p.parent_id = <parent_of(a.entity_id)>
      AND p.as_of_date = date_trunc('day', <as_of_ts>);


-- -----------------------------------------------------------------------------
-- 2. Parent rates for shrinkage — also point-in-time, also snapshotted daily.
--    Hierarchy: pan -> bin8 -> issuer -> country
--               ip  -> ip24 -> asn
--               device -> device_model
--               email -> email_dom
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_parent_rate_pit AS
SELECT
    d.as_of_date,
    t.bin8                                              AS parent_id,
    sum(la.is_fraud_cb) FILTER (
        WHERE la.label_arrival_ts < d.as_of_date)::float
      / nullif(count(*) FILTER (
        WHERE t.disposition = 'SEN'), 0)                AS parent_cb_rate
FROM date_spine d
JOIN txn t
  ON  t.event_ts <  d.as_of_date
  AND t.event_ts >= d.as_of_date - INTERVAL '90 days'
LEFT JOIN v_label_arrival la ON la.txn_id = t.txn_id
GROUP BY 1, 2;


-- -----------------------------------------------------------------------------
-- 3. Out-of-fold encoding (check 3). Never encode on the rows you train on.
--    The PIT construction above is already time-forward, which satisfies this
--    for time-split training. If you use random K-fold ANYWHERE, encode per fold.
--
--    Validation: shuffle the labels and rebuild. The encoded feature's IV must
--    collapse to ~0. If it does not, the encoding is leaking.
-- -----------------------------------------------------------------------------


-- -----------------------------------------------------------------------------
-- 4. Censoring correction (check 4).
--    The rate above is computed over APPROVED traffic only, so an entity the
--    champion blocks heavily looks clean. Re-weight using release-program labels:
--    released blocks carry sample_weight = 1 / P(release | stratum).
-- -----------------------------------------------------------------------------
WITH observed AS (
    SELECT entity_id, sum(is_fraud_cb) AS cb, count(*) AS n, 1.0 AS w
    FROM   approved_traffic GROUP BY 1
),
released AS (
    SELECT t.entity_id,
           sum(la.is_fraud_cb * r.sample_weight) AS cb,
           sum(r.sample_weight)                  AS n,
           avg(r.sample_weight)                  AS w
    FROM   release_log r
    JOIN   txn t USING (txn_id)
    LEFT JOIN v_label_arrival la USING (txn_id)
    GROUP BY 1
)
SELECT entity_id,
       (o.cb + r.cb) / nullif(o.n + r.n, 0) AS cb_rate_debiased
FROM observed o FULL OUTER JOIN released r USING (entity_id);


-- -----------------------------------------------------------------------------
-- 5. Serving-parity harness (check 2) — the test that catches everything else.
--    Recompute this feature offline for transactions that were also scored in
--    shadow, and diff. The diff distribution IS the training/serving skew metric.
--
--      SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY abs(off.v - shd.v))
--      FROM offline_features off JOIN shadow_features shd USING (txn_id, feature);
--
--    Any feature above tolerance is rejected regardless of offline lift.
--    Run this BEFORE reporting any backtest result, not after.
-- -----------------------------------------------------------------------------
