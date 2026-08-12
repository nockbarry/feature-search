# Discovery checklist

What to look for inside the company, and how to tell a real match from a
lookalike. Generated from `spec/data_requirements.yaml` — edit that, not this.

Record every item as **found** (with the table.column), **absent** (we looked,
it does not exist) or **unknown** (nobody has checked). Absent and unknown are
not the same answer: one is a design constraint, the other is work outstanding.

```bash
python discover.py --template > binding.yaml   # skeleton to fill in
python discover.py --binding binding.yaml      # what your findings unlock
```

| Criticality | Count | Meaning |
|---|---|---|
| core | 9 | the search cannot start without it |
| high | 16 | an entity class or typology dies without it |
| medium | 15 | a family degrades |
| optional | 2 | nice to have |

---

## Core transaction record

> Without every core item below there is no search at all — not a degraded one. Confirm these first, before spending any time on enrichment.

### `txn_id` — Unique identifier for the authorization attempt

**CORE** · transaction_record · string

*Search the catalog for:* `transaction_id`, `txn_id`, `auth_id`, `payment_id`, `order_id`, `request_id`, `trace_id`

*Verify:* Must be unique per AUTHORIZATION ATTEMPT, not per order. If one order with three retries shares one id, retry and failed-then-success features are unbuildable and velocity counts are wrong.

*Gotcha:* Many stacks have both an order id and an attempt id and name the attempt one ambiguously. Check the row count ratio against orders.

### `event_ts` — Timestamp the authorization decision was made

**CORE** · transaction_record · timestamp

*Search the catalog for:* `event_time`, `txn_ts`, `created_at`, `auth_ts`, `request_time`, `decision_time`

*Unlocks:* measures: `hour_entropy` · windows: ALL

*Verify:* Confirm the timezone and whether it is decision time or settlement time. Compare against a known incident window.

*Gotcha:* `created_at` on a mutable orders table is often the row's INSERT time and can be later than the decision. Every window in the search is anchored on this field; if it is wrong, everything is wrong.

### `label_arrival_ts` — Timestamp WE LEARNED an outcome, distinct from when it occurred

**CORE** · label_feed · timestamp

*Search the catalog for:* `reported_at`, `received_at`, `ingested_at`, `posted_date`, `alert_date`, `network_report_date`, `load_ts`, `_etl_loaded_at`

*Verify:* Take a chargeback and confirm this is strictly later than the transaction's event_ts, typically by days to weeks. If the two are equal or near-equal for most rows, you have found a backfilled column and not an arrival clock.

*Gotcha:* THE most important field in this document and the one most often missing. Warehouses routinely overwrite dispute rows in place, keeping only the current state, which destroys arrival history. If it does not exist, an ETL audit log or CDC stream may reconstruct it; if nothing can, say so loudly — every outcome-rate feature in the catalog becomes unverifiable and the two-clock rule cannot be enforced. See docs/split_protocol.md and tests/test_pit_leakage.py.

### `amount` — Transaction amount, plus its currency

**CORE** · transaction_record · numeric + string

*Search the catalog for:* `amount`, `amt`, `value`, `total`, `order_total`, `transaction_amount`, `amount_minor`, `currency`, `currency_code`

*Unlocks:* measures: `amt`, `amt_repeat`, `amt_escalate`, `amt_entropy`, `round_amt_rate`, `cb_amt`

*Verify:* Establish minor vs major units (cents vs dollars) and whether a currency conversion has already been applied. Check the min: a minimum of 1 usually means minor units.

*Gotcha:* Mixed-currency portfolios where amount is unconverted make repeat-amount and round-amount features meaningless across corridors. Bind a normalized amount too if one exists.

### `disposition` — Funnel outcome — SEN / DEN / ERR

**CORE** · transaction_record · enum

*Search the catalog for:* `status`, `txn_status`, `disposition`, `result`, `state`, `outcome`, `settlement_status`, `auth_result`

*Unlocks:* measures: `sen_rate`, `den_rate`, `err_rate`, `txn_cnt`

*Verify:* Map every distinct value to exactly one of SEN / DEN / ERR and confirm the mapping with whoever owns the payments pipeline. Count the unmapped remainder — it is never zero on the first pass.

*Gotcha:* Only SEN can charge back. If the disposition is ambiguous, chargeback rate denominators are wrong and every outcome feature is quietly miscalibrated. Watch for pending/in-flight states that later change.

### `decision_ts_features` — Which fields were actually POPULATED at decision time

**CORE** · derived · n/a

*Search the catalog for:* `(not a column — a property of each column)`

*Verify:* For each field you bind, establish whether its value was known pre-authorization or written afterwards. Compare a live scoring payload against the warehouse row for the same txn_id.

*Gotcha:* The single most productive source of accidental leakage. Warehouse tables are enriched after the fact — a `device_trust_score` column may be populated hours later. A field being present in the table says nothing about it being available at 40ms pre-auth. Anything that fails this check is a later-lifecycle feature, not a pre-auth one.

---

## Payment instrument

### `pan_token` — Stable per-card token or PAN hash

**CORE** · transaction_record · string

*Search the catalog for:* `card_token`, `pan_token`, `card_hash`, `card_fingerprint`, `instrument_id`, `payment_method_id`, `card_id`, `token`

*Unlocks:* entities: `pan` · measures: `d_pan`

*Verify:* Must be stable for the SAME card across sessions, customers and merchants. Check that a card used by two customers yields one token — if tokens are minted per customer, cross-customer card reuse (a strong fraud signal) is invisible.

*Gotcha:* Network tokens rotate on reissue and per-merchant tokens do not join across merchants. If only a per-merchant token exists, say so — card velocity features are then merchant-scoped and much weaker. BIN+last4+expiry is a workable proxy key if nothing better exists.

### `bin` — Bank identification number, 8 and 6 digit

**HIGH** · transaction_record · string

*Search the catalog for:* `bin`, `iin`, `card_bin`, `bin6`, `bin8`, `first_six`, `first_eight`

*Unlocks:* entities: `bin8`, `bin6` · measures: `d_bin8`

*Verify:* Confirm digit length and zero-padding survived the ETL as a string.

*Gotcha:* BIN6 is too coarse post-BIN-expansion — several issuers share a BIN6. If only 6 digits are stored, bin8 entities are unbuildable and you should raise it: the fix is upstream, not in the feature pipeline. A numeric column silently drops leading zeros.

### `bin_table` — BIN reference data — issuer, country, funding type, product

**HIGH** · vendor_enrichment · table

*Search the catalog for:* `bin_table`, `bin_reference`, `iin_lookup`, `card_metadata`, `bin_enrichment`

*Unlocks:* entities: `issuer` · non-grid families: `instrument`

*Verify:* Check join coverage against live traffic — a stale table can miss 10-20% of current BINs. Measure the miss rate before relying on it.

*Gotcha:* Prepaid vs credit vs debit funding type is one of the highest-value single fields in this whole document and is frequently absent from in-house BIN tables. Commercial-vs-consumer and issuer country tier matter nearly as much.

### `auth_response` — Issuer response code, AVS, CVV and 3DS results

**HIGH** · transaction_record · string (several)

*Search the catalog for:* `response_code`, `decline_code`, `avs_result`, `avs_response`, `cvv_result`, `cvc_response`, `three_ds_status`, `eci`, `liability_shift`, `network_response`

*Unlocks:* measures: `err_lost_stln`, `err_dnh`, `retry_success`, `fail_then_ok` · non-grid families: `instrument`

*Verify:* Confirm you have the RAW issuer code, not a bucketed internal status. Lost/stolen must be separable from insufficient-funds.

*Gotcha:* A lost/stolen decline on an entity is nearly a fraud label with a ~1 day lag instead of 30 — it is both a feature and your fastest proxy label. Losing it to a "declined" bucket is an expensive simplification that has usually already happened upstream.

---

## Device and network

### `device_fp` — Device fingerprint stable across sessions

**HIGH** · transaction_record · string

*Search the catalog for:* `device_id`, `device_fingerprint`, `fingerprint`, `fp_hash`, `visitor_id`, `browser_id`, `client_id`, `device_token`, `ja3`

*Unlocks:* entities: `device` · measures: `d_device` · non-grid families: `graph`, `novelty`, `deviation`

*Verify:* Compare distinct devices to distinct sessions over a month. A ratio near 1.0 means you have a session cookie, NOT a device fingerprint, and cross-session linkage — the entire point — is absent.

*Gotcha:* A per-session id looks identical in a schema browser and fails silently: every device is new, novelty features fire constantly, and the graph has no edges. This is the single most common bad bind in this document. Verify before building anything device-keyed.

### `ip_address` — Client IP address

**HIGH** · transaction_record · string

*Search the catalog for:* `ip`, `ip_address`, `client_ip`, `remote_addr`, `source_ip`, `x_forwarded_for`

*Unlocks:* entities: `ip`, `ip24`, `ip16` · measures: `d_ip24` · non-grid families: `graph`, `consistency`

*Verify:* Check whether it is the true client IP or your load balancer's. If the top few IPs carry a large share of traffic, you are looking at your own infrastructure. Confirm IPv6 handling — /24 masking is IPv4 logic.

*Gotcha:* Frequently truncated or dropped for privacy reasons. If retention is short, entity history windows are capped by it — check the retention period, not just the presence of the column.

### `ip_intel` — ASN, ISP, connection type, VPN/proxy/Tor flags, IP geolocation

**HIGH** · vendor_enrichment · table

*Search the catalog for:* `asn`, `as_number`, `isp`, `connection_type`, `is_vpn`, `is_proxy`, `is_datacenter`, `ip_country`, `ip_city`, `ip_lat`, `ip_lon`, `maxmind`, `ipinfo`

*Unlocks:* entities: `asn` · non-grid families: `consistency`, `population`

*Verify:* Confirm the enrichment is point-in-time (IP-to-ASN mappings change) or accept that historical rows carry today's answer.

*Gotcha:* Residential vs datacenter vs mobile is the highest-value bit here. Re-enriching history with current mappings is itself a mild leak and also a training/serving skew source — note which you have.

### `device_attrs` — Device model, OS, browser, locale, timezone, screen

**MEDIUM** · transaction_record · string (several)

*Search the catalog for:* `user_agent`, `ua`, `os`, `os_version`, `browser`, `browser_version`, `locale`, `language`, `timezone_offset`, `screen_resolution`, `device_model`, `platform`

*Unlocks:* entities: `device_model`, `os_browser` · non-grid families: `consistency`, `biometrics`

*Verify:* Confirm the raw user-agent is retained, not only a parsed summary.

*Gotcha:* Device locale and timezone vs IP timezone is a cross-field consistency check that costs nothing and catches proxied sessions.

### `canvas_hash` — Canvas / WebGL rendering fingerprint

**OPTIONAL** · transaction_record · string

*Search the catalog for:* `canvas_hash`, `webgl_hash`, `canvas_fp`, `gpu_hash`, `render_fingerprint`

*Unlocks:* entities: `canvas`

*Verify:* Present only if a client-side SDK collects it.

*Gotcha:* Usually arrives with a device-intelligence vendor rather than separately. If absent, this is a procurement decision, not something the feature pipeline can recover.

---

## Email and phone

### `email_address` — Raw email address as entered

**HIGH** · transaction_record · string

*Search the catalog for:* `email`, `email_address`, `user_email`, `contact_email`, `buyer_email`

*Unlocks:* entities: `email`, `email_canon`, `email_dom` · measures: `d_email` · non-grid families: `strings`, `graph`, `novelty`

*Verify:* Confirm the RAW value is retained, not a lowercased/normalized one — canonicalization (stripping dots and plus-tags) must be yours to do, and the difference between raw and canonical is itself a signal.

*Gotcha:* If hashed at rest, string and entropy features (local-part length, digit ratio, keyboard walks, edit distance to nearest known email) are all dead, and that is a whole family. Check for a hashed-only policy before planning around it.

### `email_intel` — Email reputation — first-seen-in-the-wild, breach presence, domain age, MX

**MEDIUM** · vendor_enrichment · table

*Search the catalog for:* `email_score`, `emailage`, `email_risk`, `domain_age`, `mx_record`, `disposable_flag`, `free_provider_flag`

*Unlocks:* entities: `email_mx` · non-grid families: `strings`, `novelty`

*Verify:* Check coverage rate on live traffic before designing around it.

*Gotcha:* Aged-email-but-everything-else-new is a synthetic identity signature and needs the external age, not your own first-seen date.

### `phone_number` — Phone number in E.164, plus line intelligence

**MEDIUM** · transaction_record · string

*Search the catalog for:* `phone`, `phone_number`, `msisdn`, `mobile`, `contact_number`, `tel`, `line_type`, `carrier`, `ported_at`

*Unlocks:* entities: `phone` · non-grid families: `consistency`

*Verify:* Confirm E.164 normalization, or you cannot derive country code reliably.

*Gotcha:* Line type (VOIP vs mobile vs landline) and port-in recency are vendor enrichment, not raw fields, and are what make phone predictive. The raw number alone is a weak entity.

---

## Identity and KYC

### `customer_id` — Stable customer/account identifier

**CORE** · transaction_record · string

*Search the catalog for:* `customer_id`, `user_id`, `account_id`, `party_id`, `member_id`, `client_id`

*Unlocks:* entities: `cust` · measures: `d_cust` · non-grid families: `deviation`, `novelty`

*Verify:* Confirm it survives email or phone changes. An id that rotates on profile edit destroys the customer-baseline family, which is the best ATO detector you have.

*Gotcha:* Guest checkout produces null or per-order ids — measure that share.

### `kyc_identity` — Name, DOB, national ID, birth country

**MEDIUM** · transaction_record · string (several)

*Search the catalog for:* `first_name`, `last_name`, `full_name`, `legal_name`, `date_of_birth`, `dob`, `national_id`, `ssn_hash`, `tax_id`, `nationality`, `country_of_birth`

*Unlocks:* entities: `name_norm`, `dob`, `dob_surname`, `natid`, `birth_country` · non-grid families: `consistency`, `strings`

*Verify:* Confirm access is permitted for modelling under your privacy policy.

*Gotcha:* Name and DOB features carry disparate-impact exposure. Do not bind these without knowing who signs off — see the fairness protocol gap in AGENT_BRIEF.md.

### `kyc_document` — Document number, type, issuing authority, capture metadata

**MEDIUM** · transaction_record · string (several)

*Search the catalog for:* `document_id`, `doc_number`, `passport_number`, `id_document`, `doc_type`, `issuing_country`, `issuing_authority`, `nfc_read`, `liveness_score`, `tamper_score`, `doc_expiry`

*Unlocks:* entities: `doc`, `doc_auth` · non-grid families: `consistency`, `novelty`

*Verify:* The high-value derived signal is document-number REUSE COUNT across distinct customers. Confirm you can compute it.

*Gotcha:* NFC-chip read vs photo capture, and liveness/tamper scores, are strong synthetic-identity signals and usually sit in the KYC vendor's response payload rather than your customer table.

---

## Addresses and geography

### `addresses` — Billing and shipping/payout addresses

**HIGH** · transaction_record · string (several)

*Search the catalog for:* `billing_address`, `bill_addr`, `shipping_address`, `ship_addr`, `address_line1`, `postcode`, `postal_code`, `zip`, `city`, `country_code`, `delivery_address`

*Unlocks:* entities: `addr_bill`, `addr_ship`, `postcode` · measures: `d_country` · non-grid families: `consistency`, `graph`

*Verify:* Confirm line-1 is retained. Unit-number cycling on one drop address is only visible if you can hash line-1 separately from the full string.

*Gotcha:* Raw addresses need normalization before they work as entity keys — "12 Main St." and "12 Main Street" must collapse. That is a build item, listed under derived_assets below.

### `geocoding` — Lat/lon for addresses, for distance and geohash features

**MEDIUM** · vendor_enrichment · numeric

*Search the catalog for:* `latitude`, `longitude`, `lat`, `lon`, `geohash`, `geo_point`, `coordinates`

*Unlocks:* entities: `geohash5` · non-grid families: `consistency`, `deviation`

*Verify:* Check the geocoder's fallback behaviour — many return a country centroid.

*Gotcha:* Centroid fallbacks make impossible-travel velocity features fire constantly and falsely. Keep the precision/confidence field.

---

## Counterparty and payout corridor

> Given the SEN/receiver-country funnel this looks like remittance or marketplace payout. These are usually the strongest features nobody builds — treat a gap here as high priority, not exotic.

### `receiver_identity` — Receiver/payee id, payout account or IBAN, receiver bank and country

**HIGH** · transaction_record · string (several)

*Search the catalog for:* `receiver_id`, `payee_id`, `beneficiary_id`, `recipient_id`, `payout_account`, `iban`, `wallet_address`, `beneficiary_account`, `receiving_bank`, `swift`, `bic`, `destination_country`, `receive_country`

*Unlocks:* entities: `receiver`, `recv_acct`, `recv_bank`, `recv_country`, `corridor`, `send_recv` · measures: `d_receiver`, `d_sender` · non-grid families: `graph`

*Verify:* Confirm the receiver id is stable across senders. If it is scoped per sender, receiver FAN-IN — many senders to one receiver, your primary mule detector — cannot be computed at all.

*Gotcha:* `d_sender` on a receiver key is the highest-value mule feature in the catalog and depends entirely on this being a shared key.

### `payout_method` — Payout rail — bank transfer, wallet, cash pickup, card push

**MEDIUM** · transaction_record · enum

*Search the catalog for:* `payout_method`, `disbursement_type`, `payout_type`, `rail`, `delivery_method`, `payment_method`

*Unlocks:* non-grid families: `population`

*Verify:* Enumerate distinct values and confirm risk differs across them.

*Gotcha:* Cash pickup and wallet rails carry materially different mule risk than bank transfer. Noted as a coverage gap in the entity registry — worth adding as an entity if this field exists.

---

## Merchant side

> Applicable if you are a PSP, marketplace or acquirer. Skip if single-merchant.

### `merchant_identity` — Merchant id, MCC, tenure

**MEDIUM** · transaction_record · string (several)

*Search the catalog for:* `merchant_id`, `seller_id`, `sub_merchant_id`, `mcc`, `merchant_category_code`, `category`, `onboarded_at`, `merchant_since`

*Unlocks:* entities: `merchant`, `mcc` · measures: `d_mcc`

*Verify:* Confirm MCC is the network value, not an internal taxonomy.

*Gotcha:* Merchant tenure bucket is a coverage gap in the current entity registry. If `onboarded_at` exists, flag it — new-merchant risk is a distinct phenomenon.

---

## Outcome and label feeds

> Each of these carries its own arrival clock. Bind label_arrival_ts per feed, not once globally — maturity differs by feed.

### `chargeback_feed` — Disputes with reason codes, amounts, and both clocks

**CORE** · label_feed · table

*Search the catalog for:* `chargeback`, `dispute`, `cb`, `retrieval_request`, `case`, `claim`, `reason_code`, `dispute_reason`, `dispute_amount`, `dispute_date`

*Unlocks:* measures: `cb_rate`, `cb_fraud_rate`, `cb_nonfr_rate`, `cb_amt`, `cb_lag_days`

*Verify:* Confirm reason codes are the RAW network codes and that you can map them to fraud vs non-fraud groups per docs/label_definition.md. Check how representments, partial disputes and second presentments are represented — each is a fork in the label.

*Gotcha:* If dispute rows are mutated in place, arrival history is gone. See label_arrival_ts. Also confirm whether a won representment flips the label back — that decision must match between features and training.

### `tc40_safe` — Issuer fraud alert feed (TC40 / SAFE)

**HIGH** · label_feed · table

*Search the catalog for:* `tc40`, `safe`, `issuer_fraud_alert`, `fraud_alert`, `network_alert`, `ethoca`, `verifi`, `cdrn`, `rdr`

*Unlocks:* measures: `tc40_rate`

*Verify:* Confirm you receive it directly or via your acquirer; check the lag.

*Gotcha:* Doubles as a fast proxy label at ~7 days maturity versus 30 for chargebacks. High lift-per-integration-effort — if it is not being consumed, that is a finding worth escalating on its own.

### `refunds_reviews` — Refunds, manual-review dispositions, rule-engine denials

**MEDIUM** · label_feed · table

*Search the catalog for:* `refund`, `credit`, `reversal`, `void`, `review_decision`, `analyst_decision`, `queue_outcome`, `rule_id`, `triggered_rules`, `denial_reason`

*Unlocks:* measures: `refund_rate`, `review_conf`, `den_by_rule`

*Verify:* Confirm rule ids are stable over time or the by-rule split is meaningless.

*Gotcha:* Manual-review outcomes are biased to the reviewed population; they need propensity weights, not raw use.

---

## Model and policy telemetry

> Required for the censoring correction. Without these, entity outcome rates silently encode current policy and the challenger relearns the champion's blind spots.

### `champion_score` — The production model's score and decision for each transaction

**HIGH** · transaction_record · numeric + enum

*Search the catalog for:* `model_score`, `risk_score`, `fraud_score`, `score`, `decision`, `action`, `blocked`, `model_version`

*Unlocks:* measures: `champ_score`, `block_rate`, `rule_hits`

*Verify:* Confirm the score is LOGGED AT DECISION TIME, not recomputed later. A recomputed score is leakage and will look excellent in backtest.

*Gotcha:* Model version must be logged alongside; a score column spanning several champion versions is not one variable.

### `release_log` — Champion-blocked transactions sampled and released, with sample weights

**HIGH** · label_feed · table

*Search the catalog for:* `release_program`, `holdout`, `bypass`, `forced_approve`, `champion_override`, `sample_weight`, `control_group`, `exploration_log`

*Verify:* Confirm a release program exists and find its rate. If none exists, this is the highest-priority finding in the whole discovery: entity outcome rates cannot be de-biased in the blocked region at all.

*Gotcha:* Referenced as `release_log` by pit_aggregate_template.sql. The reject-inference method is still unspecified — see the "Not yet written" section of AGENT_BRIEF.md. Bring the release rate back regardless; it bounds what any method can do.

---

## Event streams beyond the transaction

> The ordered event stream before a transaction is richer than the transaction itself. Credential-change recency in particular is the strongest single ATO feature most teams lack.

### `account_events` — Password, email, phone and address change events with timestamps

**HIGH** · event_stream · event stream

*Search the catalog for:* `account_events`, `user_events`, `profile_changes`, `audit_log`, `change_log`, `credential_change`, `password_reset`, `email_change`, `security_events`

*Unlocks:* non-grid families: `sequence`, `novelty`

*Verify:* Confirm timestamps and that the stream joins to customer_id. Check retention — 90 days is common and caps your longest window.

*Gotcha:* Usually owned by the identity or platform team, not payments, and therefore routinely missed in a payments-scoped discovery. Ask explicitly. Highest lift-per-integration-effort item in this file.

### `session_events` — Login, navigation, checkout funnel, cart edits, failed logins

**MEDIUM** · event_stream · event stream

*Search the catalog for:* `session`, `clickstream`, `page_views`, `events`, `funnel`, `login_attempts`, `failed_login`, `cart_events`, `checkout_events`, `segment`, `amplitude`

*Unlocks:* non-grid families: `sequence`

*Verify:* Confirm the session id joins to the transaction record.

*Gotcha:* Analytics warehouses often sample clickstream. Sampled data is unusable for per-transaction features — check the sampling rate before designing around it.

### `attribution` — Acquisition channel, campaign, affiliate

**MEDIUM** · event_stream · string

*Search the catalog for:* `utm_source`, `utm_campaign`, `channel`, `referrer`, `affiliate_id`, `partner_id`, `attribution`, `source`, `medium`

*Unlocks:* non-grid families: `sequence`, `population`

*Verify:* Confirm it is retained per transaction, not only at signup.

*Gotcha:* Fraud concentrates hard by campaign and affiliate. Wildly underused and usually already collected by marketing — a cheap win.

### `auth_history` — Prior authorization attempts including declines, in-session

**MEDIUM** · event_stream · event stream

*Search the catalog for:* `auth_log`, `attempts`, `retries`, `decline_history`, `authorization_attempts`

*Unlocks:* measures: `fail_then_ok`, `retry_success`, `interarrival`

*Verify:* Confirm failed attempts are retained, not only successful ones.

*Gotcha:* Many pipelines keep only approved transactions. Card testing is almost entirely visible in the declines — dropping them removes the typology.

---

## Basket and fulfillment

> Mostly POST-authorization. The model here scores pre-auth, so these cannot be features for it — but delivery confirmation and dispute history are the friendly-fraud predictors, and friendly fraud is the thinnest typology in the catalog. Record what exists; it argues for a second model scoring later in the lifecycle.

### `basket` — Line items, categories, quantities, shipping method and cost

**MEDIUM** · transaction_record · table

*Search the catalog for:* `line_items`, `order_items`, `sku`, `product_category`, `quantity`, `shipping_method`, `shipping_cost`, `expedited`, `gift_message`

*Unlocks:* non-grid families: `basket`

*Verify:* Confirm availability pre-authorization, which is usually true for basket.

*Gotcha:* Resale liquidity of the goods is a derived attribute you will have to build.

### `support_history` — Customer-support contacts and prior dispute history

**MEDIUM** · event_stream · table

*Search the catalog for:* `tickets`, `cases`, `contacts`, `zendesk`, `support_history`, `prior_disputes`, `dispute_count`

*Unlocks:* non-grid families: `basket`, `population`

*Verify:* Confirm prior-dispute counts are computable point-in-time.

*Gotcha:* Prior dispute count and customer tenure are the friendly-fraud predictors that DO work pre-auth. Given how thin that typology is in the catalog, this is a priority despite the modest criticality.

### `delivery` — Delivery confirmation, address type, freight-forwarder match

**OPTIONAL** · event_stream · table

*Search the catalog for:* `tracking`, `delivery_status`, `delivered_at`, `carrier`, `address_type`, `residential_flag`, `po_box`, `freight_forwarder`, `reshipper`

*Unlocks:* non-grid families: `basket`

*Verify:* Confirm timestamps to establish what is known pre-auth (usually nothing).

*Gotcha:* POST-TRANSACTION. Enormously predictive of friendly-fraud disputes and unusable for a pre-auth model. Do not bind it as a pre-auth feature; record it as the seed of a later-lifecycle model.

---

## Things you must build, not find

> These do not exist as a column anywhere. They are the prerequisites the search assumes; each is a build item with an owner and an estimate.

### `first_seen_table` — First-seen timestamp per entity value, point-in-time correct

**HIGH** · derived · table

*Search the catalog for:* `(build it)`

*Unlocks:* non-grid families: `novelty`

*Verify:* Must be as-of correct: the first-seen date for an entity queried at time T must reflect only events known by T.

*Gotcha:* Trivially easy to build with a leak — a MIN(event_ts) GROUP BY over the full table gives every row the benefit of hindsight. Whole novelty family depends on getting this right.

### `entity_graph` — Multipartite graph over card, device, email, IP, address, phone, receiver

**HIGH** · derived · graph

*Search the catalog for:* `(build it)`

*Unlocks:* entities: `comp2` · non-grid families: `graph`

*Verify:* Component ids must be computed as-of scoring time. Label propagation across the graph must filter on label_arrival_ts like everything else.

*Gotcha:* Ring fraud is invisible to per-entity aggregates and glaring here, so it is worth the build. But 2-hop centrality inside a 40ms pre-auth budget is its own project — component id and 1-hop fraud counts are the realistic pre-auth subset.

### `serving_parity_harness` — Shadow-computed vs offline-recomputed feature values on the same transactions

**HIGH** · derived · table

*Search the catalog for:* `shadow_features`, `offline_features`, `feature_store`, `parity_check`

*Verify:* Referenced as `shadow_features` and `offline_features` by pit_aggregate_template.sql. The diff between them IS your training/serving skew metric.

*Gotcha:* Must exist before any backtest number is reported, not after. A feature store that mutates history in place makes parity unmeasurable — check for point-in-time correctness support.

### `address_normalization` — Address standardization plus freight-forwarder/reshipper lists

**MEDIUM** · derived · service

*Search the catalog for:* `address_normalization`, `address_standardization`, `libpostal`, `smarty`, `loqate`, `reshipper_list`

*Unlocks:* entities: `addr_bill`, `addr_ship`

*Verify:* Check collapse rate on known-duplicate addresses.

*Gotcha:* Without it, address entities fragment and every address feature is diluted.

---
