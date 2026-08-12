"""
validate.py — schema and invariant checks on feature_space.yaml.

The expander is permissive by design: it consumes whatever the registries hold
and emits rows. That means a malformed registry does not fail, it ships — as a
plausible-looking catalog with a silent hole in it. This catches that class of
error before it reaches the workbook, and therefore before an agent spends a day
screening a feature whose definition was quietly truncated.

The motivating bug, found in the field: an unquoted comma inside a YAML flow
mapping,

    - {id: cb_fraud_rate, name: CB rate, fraud reason group, family: outcome, ...}

parses as name="CB rate" plus a stray key "fraud reason group": null. The measure
still expands, still names its features correctly, and still reads as "CB rate"
in the workbook — identical to cb_nonfr_rate. Four measures were affected, in
exactly the fraud/non-fraud and lost-stolen/do-not-honor splits the search most
depends on. STRAY_KEYS below is the check that would have caught it on commit.

Run:  python validate.py [--spec feature_space.yaml]
Exit: 0 clean, 1 if any ERROR. Warnings never fail the build.
"""
import argparse
import sys

import yaml

# Allowed keys per registry: (required, optional). Anything else is a stray key.
SCHEMA = {
    "labels":     ({"id", "definition", "maturity_days_90pct"}, {"notes"}),
    "entities":   ({"id", "name", "class", "adversarial_cost", "availability", "priority"}, set()),
    "composites": ({"id", "parts", "hypothesis"}, set()),
    "measures":   ({"id", "name", "family", "label_dependent", "dtype", "priority"}, set()),
    "windows":    ({"id", "label", "seconds", "type", "realtime", "priority"}, set()),
    "filters":    ({"id", "label", "priority"}, set()),
    "aggregations": ({"id", "name", "applies_to", "priority"}, set()),
    "transforms": ({"id", "name", "formula", "priority"}, set()),
    "nongrid_families": ({"id", "name", "min_features"}, set()),
    "typologies": ({"id", "name"}, {"note"}),
}

ENUMS = {
    ("entities", "class"): {"identity", "instrument", "device", "network", "email",
                            "geo", "counterparty", "merchant", "graph"},
    ("entities", "adversarial_cost"): {"free", "cheap", "expensive"},
    ("entities", "availability"): {"realtime", "streaming", "enrichment", "batch_only"},
    ("measures", "family"): {"outcome", "velocity", "behavior", "score"},
    ("measures", "dtype"): {"rate", "count", "money", "numeric"},
    ("windows", "type"): {"time", "count", "since", "excluding"},
}

# dtypes an aggregation may apply to — must match the measure dtype domain.
DTYPES = {"rate", "count", "money", "numeric"}

NONGRID_AVAIL = {"realtime", "streaming", "enrichment", "batch_only"}
NONGRID_LEAK = {"LOW", "MED", "HIGH"}


def check(spec, nongrid):
    errors, warnings = [], []
    E = errors.append
    W = warnings.append

    # ---- 1. per-registry schema: required present, no stray keys -------------
    for reg, (required, optional) in SCHEMA.items():
        rows = spec.get(reg)
        if not rows:
            E(f"{reg}: registry missing or empty")
            continue
        allowed = required | optional
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                E(f"{reg}[{i}]: expected a mapping, got {type(row).__name__}")
                continue
            rid = row.get("id", f"<index {i}>")
            for k in sorted(required - set(row)):
                E(f"{reg}.{rid}: missing required key '{k}'")
            for k in sorted(set(row) - allowed):
                # Almost always an unquoted comma inside a YAML flow mapping.
                E(f"{reg}.{rid}: stray key '{k}' (unquoted comma in a flow mapping? "
                  f"quote the value: name: \"a, b\")")

    # ---- 2. ids unique within each registry, and globally ---------------------
    seen = {}
    for reg in SCHEMA:
        local = set()
        for row in spec.get(reg) or []:
            rid = row.get("id") if isinstance(row, dict) else None
            if rid is None:
                continue
            if rid in local:
                E(f"{reg}: duplicate id '{rid}'")
            local.add(rid)
            # entity and composite ids share the group-by namespace; measures,
            # windows, filters etc. are separate slots and may legally collide.
            if reg in ("entities", "composites"):
                if rid in seen:
                    E(f"group-by id '{rid}' defined in both {seen[rid]} and {reg}")
                seen[rid] = reg

    entity_ids = {e["id"] for e in spec.get("entities") or [] if isinstance(e, dict) and "id" in e}
    groupby_ids = entity_ids | {c["id"] for c in spec.get("composites") or []
                                if isinstance(c, dict) and "id" in c}

    # ---- 3. enum domains -----------------------------------------------------
    for (reg, field), domain in ENUMS.items():
        for row in spec.get(reg) or []:
            if not isinstance(row, dict) or field not in row:
                continue
            if row[field] not in domain:
                E(f"{reg}.{row.get('id')}: {field}={row[field]!r} not in {sorted(domain)}")

    # aggregations declare which measure dtypes they accept; a typo there
    # silently deletes every feature pairing that aggregation with that dtype.
    for a in spec.get("aggregations") or []:
        if not isinstance(a, dict):
            continue
        for d in a.get("applies_to") or []:
            if d not in DTYPES:
                E(f"aggregations.{a.get('id')}: applies_to '{d}' is not a measure dtype")
    covered_dtypes = {d for a in spec.get("aggregations") or [] if isinstance(a, dict)
                      for d in a.get("applies_to") or []}
    for d in sorted(DTYPES - covered_dtypes):
        E(f"aggregations: no aggregation accepts dtype '{d}' — every measure of that "
          f"dtype expands to zero rows")

    # ---- 4. composites reference real entities -------------------------------
    for c in spec.get("composites") or []:
        if not isinstance(c, dict):
            continue
        parts = c.get("parts") or []
        if len(parts) < 2:
            E(f"composites.{c.get('id')}: needs >=2 parts, got {parts}")
        for p in parts:
            if p not in entity_ids:
                E(f"composites.{c.get('id')}: part '{p}' is not a declared entity")

    # ---- 5. resolution ladder resolves, and never gates COVERAGE -------------
    win_ids = {w["id"] for w in spec.get("windows") or [] if isinstance(w, dict)}
    slot_ids = {
        "transforms": {t["id"] for t in spec.get("transforms") or [] if isinstance(t, dict)},
        "filters": {f["id"] for f in spec.get("filters") or [] if isinstance(f, dict)},
        "aggregations": {a["id"] for a in spec.get("aggregations") or [] if isinstance(a, dict)},
    }
    measure_families = {m["family"] for m in spec.get("measures") or []
                        if isinstance(m, dict) and "family" in m}

    # "ALL" is the legal sentinel for "the whole registry at this rung".
    for lvl, cfg in (spec.get("resolution_ladder") or {}).items():
        for slot, ids in slot_ids.items():
            listed = cfg.get(slot)
            if listed == "ALL" or listed is None:
                continue
            for x in listed:
                if x not in ids:
                    E(f"resolution_ladder.{lvl}.{slot}: '{x}' is not a declared {slot[:-1]}")
        wmap = cfg.get("windows") or {}
        # COVERAGE INVARIANT: a rung narrows resolution, never removes a measure
        # family. A family with no window at some rung silently disappears from
        # that catalog, which is a blind spot no later refinement recovers.
        for fam in sorted(measure_families - set(wmap)):
            E(f"resolution_ladder.{lvl}.windows: measure family '{fam}' has no window "
              f"— every feature in that family vanishes at {lvl}")
        for fam, ws in wmap.items():
            if fam not in measure_families:
                W(f"resolution_ladder.{lvl}.windows: family '{fam}' has no measures")
            if not ws:
                E(f"resolution_ladder.{lvl}.windows.{fam}: empty window list")
            for w in ws or []:
                if w not in win_ids:
                    E(f"resolution_ladder.{lvl}.windows.{fam}: '{w}' is not a declared window")
        gran = cfg.get("entity_granularity")
        for cls, sibs in ({} if gran in ("ALL", None) else gran).items():
            if not sibs:
                E(f"resolution_ladder.{lvl}.entity_granularity.{cls}: empty — the whole "
                  f"class is dropped, which gates coverage rather than resolution")
            for s in sibs or []:
                if s not in groupby_ids:
                    E(f"resolution_ladder.{lvl}.entity_granularity.{cls}: "
                      f"'{s}' is not a declared entity or composite")

    # ---- 6. labels -----------------------------------------------------------
    primary = (spec.get("meta") or {}).get("label")
    label_ids = {le["id"] for le in spec.get("labels") or [] if isinstance(le, dict)}
    if primary and primary not in label_ids:
        E(f"meta.label '{primary}' is not a declared label")
    for le in spec.get("labels") or []:
        if isinstance(le, dict) and not isinstance(le.get("maturity_days_90pct"), (int, float)):
            E(f"labels.{le.get('id')}: maturity_days_90pct must be numeric — the split "
              f"protocol's embargo width is derived from it")

    # ---- 7. nongrid_features.py agrees with the registries -------------------
    typ_ids = {t["id"] for t in spec.get("typologies") or [] if isinstance(t, dict)}
    fam_min = {f["id"]: f["min_features"] for f in spec.get("nongrid_families") or []
               if isinstance(f, dict) and "id" in f}
    counts = dict.fromkeys(fam_min, 0)
    ng_names = set()
    for row in nongrid:
        fam, name, _defn, _inputs, typs, avail, leak, _hl = row
        if fam not in fam_min:
            E(f"nongrid.{name}: family '{fam}' is not in nongrid_families")
        else:
            counts[fam] += 1
        if name in ng_names:
            E(f"nongrid: duplicate feature name '{name}'")
        ng_names.add(name)
        for t in (typs or "").split(","):
            t = t.strip()
            if t and t not in typ_ids:
                E(f"nongrid.{name}: typology '{t}' is not declared")
        if avail not in NONGRID_AVAIL:
            E(f"nongrid.{name}: availability {avail!r} not in {sorted(NONGRID_AVAIL)}")
        if leak not in NONGRID_LEAK:
            E(f"nongrid.{name}: leakage_risk {leak!r} not in {sorted(NONGRID_LEAK)}")
    for fam, floor in fam_min.items():
        if counts[fam] < floor:
            W(f"nongrid family '{fam}': {counts[fam]} features, floor is {floor} "
              f"— {floor - counts[fam]} short")

    # ---- 8. typology coverage ------------------------------------------------
    covered = {t.strip() for row in nongrid for t in (row[4] or "").split(",") if t.strip()}
    for t in sorted(typ_ids - covered):
        W(f"typology '{t}': no non-grid feature targets it — near-disjoint predictors "
          f"mean this typology is unaddressed, not merely under-weighted")

    return errors, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--spec", default="feature_space.yaml")
    ap.add_argument("--strict", action="store_true", help="treat warnings as errors")
    a = ap.parse_args(argv)

    with open(a.spec) as f:
        spec = yaml.safe_load(f)
    from nongrid_features import NONGRID

    errors, warnings = check(spec, NONGRID)

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    if errors or (a.strict and warnings):
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s) — {a.spec} is not valid.")
        return 1
    print(f"{a.spec}: ok ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
