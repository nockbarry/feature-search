#!/usr/bin/env python3
"""
expand_catalog.py — expand feature_space.yaml into the candidate feature catalog.

    python expand_catalog.py --spec feature_space.yaml --out candidates.csv

Emits one row per candidate feature with the slot decomposition, a deterministic
feature name, and the four gating attributes every candidate must carry:
availability tier, leakage risk, support floor, adversarial half-life.

The full slot product is astronomically large; this applies COMPATIBILITY rules
(dtype/agg, window/transform, label-dependence) and PRIORITY gating so that what
lands in the catalog is buildable. Tier counts for the ungated space are reported
so nobody mistakes the materialized set for the whole space.
"""
import argparse, csv, itertools, sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pip install pyyaml")

# --------------------------------------------------------------------------
# compatibility rules
# --------------------------------------------------------------------------
DISTINCT_MEASURES = {"d_pan","d_device","d_email","d_cust","d_ip24","d_receiver",
                     "d_sender","d_bin8","d_country","d_mcc"}
LAG_TRANSFORMS    = {"ratio_sl","delta_lag","lograt_lag","accel"}
SELF_REF_ENTITY   = {  # measure -> entity it counts; never aggregate an entity by itself
    "d_pan":"pan","d_device":"device","d_email":"email","d_cust":"cust",
    "d_ip24":"ip24","d_receiver":"receiver","d_sender":"send_recv","d_bin8":"bin8",
}

def compatible(ent, mea, agg, win, tra, flt) -> bool:
    if mea["dtype"] not in agg["applies_to"]:
        return False
    if mea["id"] in DISTINCT_MEASURES and agg["id"] != "dcnt":
        return False
    if mea["id"] not in DISTINCT_MEASURES and agg["id"] == "dcnt":
        return False
    if mea["dtype"] == "rate" and agg["id"] not in ("rate","ewma","slope","tsl","tsf"):
        return False
    if SELF_REF_ENTITY.get(mea["id"]) == ent["id"]:
        return False                                   # distinct devices per device
    # time-since aggs are windowless
    if agg["id"] in ("tsl","tsf") and win["id"] != "life":
        return False
    if agg["id"] not in ("tsl","tsf") and win["id"] == "life" and mea["family"] == "behavior":
        return False
    # label-dependent measures need enough calendar time to accrue matured labels
    if mea["label_dependent"] and win["seconds"] is not None and win["seconds"] < 604800:
        return False
    if mea["label_dependent"] and win["type"] == "count":
        return False
    # lag/ratio transforms need a time-type window to have a prior window
    if tra["id"] in LAG_TRANSFORMS and win["type"] != "time":
        return False
    # shrinkage only makes sense on rates
    if tra["id"] == "shrunk" and mea["dtype"] != "rate":
        return False
    if tra["id"] == "decay" and mea["dtype"] != "rate":
        return False
    # z_self needs history longer than the window itself
    if tra["id"] == "z_self" and win["type"] != "time":
        return False
    # graph component entity: only meaningful with outcome/velocity measures
    if ent.get("class") == "graph" and mea["family"] in ("behavior","score"):
        return False
    # diff_* filters only apply where the contrast exists
    if flt["id"] == "diff_device" and ent["id"] in ("device","canvas","device_model"):
        return False
    if flt["id"] == "diff_recvctry" and ent["id"] in ("recv_country","corridor"):
        return False
    return True

def tier(parts) -> int:
    """P1 = everything priority-1 (build first). P2 = nothing worse than 2. P3 = rest."""
    ps = [p.get("priority", 3) for p in parts]
    if max(ps) == 1: return 1
    if max(ps) <= 2: return 2
    return 3

# Windows are gated by measure family: a 1-minute chargeback rate is meaningless
# and a 365-day inter-arrival mean is not actionable. The L2 sets below are the
# full registry; L0/L1 are coarser rungs read from resolution_ladder in the YAML.
WINDOWS_BY_FAMILY = {
    "outcome":  {"w7d","w30d","w90d","w365d","life","w30x7"},
    "velocity": {"w5m","w1h","w6h","w1d","w7d","w30d","life","n10","n50"},
    "behavior": {"w1m","w5m","w1h","w1d","w7d","n10"},
    "score":    {"w1d","w7d","w30d","life"},
}


class Resolution:
    """
    Coarse-to-fine gate over the resolution slots (windows, transforms, filters,
    aggregations, entity granularity). Coverage slots — entities and measures —
    are NEVER gated: a missing quantity is a blind spot no resolution recovers.

    corr(N_short, N_long) = sqrt(w_short/w_long) for nested count windows under
    Poisson, so rungs are spaced so adjacent windows stay below ~0.5 correlation.
    """

    def __init__(self, spec, level="L2"):
        self.level = level
        ladder = spec.get("resolution_ladder", {})
        cfg = ladder.get(level)
        if cfg is None:
            raise SystemExit(f"unknown level {level!r}; have {sorted(ladder) or 'none'}")
        self.windows = {fam: set(ws) for fam, ws in cfg["windows"].items()}
        self.transforms = self._set(cfg.get("transforms"))
        self.filters = self._set(cfg.get("filters"))
        self.aggregations = self._set(cfg.get("aggregations"))
        gran = cfg.get("entity_granularity")
        # entity_granularity restricts SIBLINGS within a family (ip/ip24/ip16),
        # never the entity list as a whole
        self.granularity = None
        if gran not in (None, "ALL"):
            self.allowed_siblings = {e for ids in gran.values() for e in ids}
            self.sibling_pool = {e for ids in
                                 spec["resolution_ladder"]["L2"].get("entity_granularity", {}).values()
                                 for e in ids} if isinstance(
                spec["resolution_ladder"]["L2"].get("entity_granularity"), dict) else set()
            # L2 declares ALL, so derive the pool from L1 (its widest explicit listing)
            if not self.sibling_pool:
                l1 = ladder.get("L1", {}).get("entity_granularity", {})
                self.sibling_pool = {e for ids in l1.values() for e in ids}
            self.granularity = True

    @staticmethod
    def _set(v):
        return None if v in (None, "ALL") else set(v)

    def ok_window(self, mea, win):
        return win["id"] in self.windows.get(mea["family"], set())

    def ok_transform(self, tra):
        return self.transforms is None or tra["id"] in self.transforms

    def ok_filter(self, flt):
        return self.filters is None or flt["id"] in self.filters

    def ok_agg(self, agg):
        return self.aggregations is None or agg["id"] in self.aggregations

    def ok_entity(self, ent):
        # only prunes entities that are declared siblings at some rung; every
        # other entity passes untouched so coverage is never narrowed
        if not self.granularity:
            return True
        if ent["id"] not in self.sibling_pool:
            return True
        return ent["id"] in self.allowed_siblings

# --------------------------------------------------------------------------
# derived gating attributes
# --------------------------------------------------------------------------
HALFLIFE = {"free":"days-weeks","cheap":"weeks-months","expensive":"months-quarters"}

def leakage_risk(mea, tra):
    if mea["label_dependent"]:
        return "HIGH - point-in-time on label_arrival_ts + out-of-fold encoding required"
    if mea["id"] in ("block_rate","den_rate","err_rate","champ_score"):
        return "MED - encodes current policy; re-baseline on threshold change"
    return "LOW"

def support_floor(ent, mea, win, default):
    n = default
    if mea["dtype"] == "rate":     n = max(n, 50)
    if mea["label_dependent"]:     n = max(n, 200)
    if ent.get("class") in ("network","email"): n = max(n, 100)
    if win["id"] in ("w1m","w5m","w1h"): n = 5
    return n

def availability(ent, win, mea):
    if ent["availability"] == "batch_only" or not win.get("realtime", True):
        return "batch_only"
    if ent["availability"] == "enrichment":
        return "enrichment (vendor latency budget applies)"
    if ent["availability"] == "streaming" or mea["family"] == "outcome":
        return "streaming aggregate (sketch-backed)"
    return "realtime"

def fname(ent, mea, agg, win, flt, tra):
    s = f"{ent['id']}__{mea['id']}__{agg['id']}__{win['id']}"
    if flt["id"] != "none": s += f"__f_{flt['id']}"
    if tra["id"] != "raw":  s += f"__t_{tra['id']}"
    return s

# --------------------------------------------------------------------------
def build_entities(spec):
    ents = list(spec["entities"])
    order = ["free", "cheap", "expensive"]
    for c in spec.get("composites", []):
        parts = [e for e in spec["entities"] if e["id"] in c["parts"]]
        ents.append({
            "id": c["id"], "name": " x ".join(p["name"] for p in parts),
            "class": "composite",
            # a composite is only as durable as its cheapest-to-rotate part
            "adversarial_cost": min((p["adversarial_cost"] for p in parts),
                                    key=lambda x: order.index(x)),
            "availability": "streaming",
            "priority": 1 if all(p.get("priority", 3) == 1 for p in parts) else 2,
            "hypothesis": c["hypothesis"],
        })
    return ents


def expand(spec, max_tier=1, stages_bc=False, survivors=None, level='L2'):
    """
    Staged generation. The unconstrained slot product is ~10^7 and materializing it
    would produce a catalog nobody reads. Instead:
      Stage A  base grid          filter=none, transform=raw
      Stage B  transform variants applied only to tier-1 base rows
      Stage C  filter variants    applied only to tier-1 base rows
    Stage counts for the ungated space are reported separately so the search
    remains auditable — nothing is silently dropped, it is deferred with a reason.
    """
    res = Resolution(spec, level)
    ents = [e for e in build_entities(spec) if res.ok_entity(e)]
    default_support = spec["meta"]["min_support_default"]
    by_id = lambda seq, i: next(x for x in seq if x["id"] == i)
    raw    = by_id(spec["transforms"], "raw")
    nofilt = by_id(spec["filters"], "none")

    def row(ent, mea, agg, win, flt, tra, t, stage):
        return {
            "feature_name": fname(ent, mea, agg, win, flt, tra),
            "tier": t, "stage": stage, "level": level,
            "entity": ent["name"], "entity_id": ent["id"], "entity_class": ent["class"],
            "measure": mea["name"], "measure_family": mea["family"],
            "aggregation": agg["name"], "window": win["label"], "window_type": win["type"],
            "filter": flt["label"], "transform": tra["name"],
            "availability": availability(ent, win, mea),
            "leakage_risk": leakage_risk(mea, tra),
            "min_support_n": support_floor(ent, mea, win, default_support),
            "adversarial_halflife": HALFLIFE[ent["adversarial_cost"]],
            "hypothesis": ent.get("hypothesis", ""),
            "status": "candidate",   # agent updates: candidate|built|dropped|shipped
            "drop_reason": "",
            "iv": "", "psi_12m": "", "coverage_pct": "", "shap_rank": "",
        }

    # ---- Stage A: base grid ------------------------------------------------
    base, tier_counts = [], {1: 0, 2: 0, 3: 0}
    for ent, mea, agg, win in itertools.product(
            ents, spec["measures"], spec["aggregations"], spec["windows"]):
        if not res.ok_window(mea, win) or not res.ok_agg(agg):
            continue
        if not compatible(ent, mea, agg, win, raw, nofilt):
            continue
        t = tier([ent, mea, agg, win])
        tier_counts[t] += 1
        if t <= max_tier:
            base.append((ent, mea, agg, win, t))

    rows = [row(e, m, a, w, nofilt, raw, t, "A_base") for e, m, a, w, t in base]
    if not stages_bc:
        return rows, tier_counts

    # ---- Stage B: transform variants on tier-1 base rows -------------------
    # Run this AFTER the univariate screen, seeded with surviving base features
    # (--survivors survivors.txt). Expanding transforms across the whole base
    # grid multiplies the screening cost ~10x for no additional coverage.
    t1 = [b for b in base if b[4] == 1 and
          (survivors is None or fname(b[0], b[1], b[2], b[3], nofilt, raw) in survivors)]
    for (ent, mea, agg, win, _), tra in itertools.product(t1, spec["transforms"]):
        if tra["id"] == "raw" or not res.ok_transform(tra):
            continue
        if not compatible(ent, mea, agg, win, tra, nofilt):
            continue
        rows.append(row(ent, mea, agg, win, nofilt, tra,
                        tier([ent, mea, agg, win, tra]), "B_transform"))

    # ---- Stage C: filter variants on tier-1 base rows ---------------------
    for (ent, mea, agg, win, _), flt in itertools.product(t1, spec["filters"]):
        if flt["id"] == "none" or not res.ok_filter(flt):
            continue
        if not compatible(ent, mea, agg, win, raw, flt):
            continue
        rows.append(row(ent, mea, agg, win, flt, raw,
                        tier([ent, mea, agg, win, flt]), "C_filter"))

    return rows, tier_counts

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", default="feature_space.yaml")
    ap.add_argument("--out",  default="candidates.csv")
    ap.add_argument("--max-tier", type=int, default=1)
    ap.add_argument("--level", default="L0", choices=["L0", "L1", "L2"],
                    help="resolution rung (default L0 = probe). Coverage slots are never gated.")
    ap.add_argument("--expand", action="store_true",
                    help="also emit stage B/C variants (run AFTER the univariate screen)")
    ap.add_argument("--survivors", help="file of surviving base feature_names, one per line")
    a = ap.parse_args()

    spec = yaml.safe_load(Path(a.spec).read_text())
    surv = set(Path(a.survivors).read_text().split()) if a.survivors else None
    rows, counts = expand(spec, a.max_tier, stages_bc=a.expand, survivors=surv, level=a.level)
    rows.sort(key=lambda r: (r["tier"], r["stage"], r["entity_class"], r["entity"], r["measure"]))

    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)

    total = sum(counts.values())
    print(f"resolution level             : {a.level}")
    print(f"compatible slot combinations : {total:,}")
    for t in (1,2,3):
        print(f"  tier P{t}: {counts[t]:,}")
    print(f"materialized (<=P{a.max_tier}, staged) : {len(rows):,} -> {a.out}")
    from collections import Counter
    for s, c in sorted(Counter(r["stage"] for r in rows).items()):
        print(f"  stage {s}: {c:,}")
    print("\nNOTE: grid features are the FLOOR, not the search. The ten non-grid")
    print("families in feature_space.yaml carry most incremental lift and must be")
    print("enumerated by hand — see AGENT_BRIEF.md section 4.")

if __name__ == "__main__":
    main()
