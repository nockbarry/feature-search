"""
discover.py — take the requirements into the company, bring a binding back.

Two directions:

  python discover.py --checklist
      Writes DISCOVERY_CHECKLIST.md — every requirement with its search terms,
      verification step and gotcha, ordered by criticality. This is what the
      agent carries into the data catalog.

  python discover.py --binding binding.yaml
      Reads what the agent found and reports what it buys: which entities and
      measures become buildable, how many catalog rows survive, and which
      typologies are unaddressable. Exits non-zero if a `core` requirement is
      missing, because the search genuinely cannot start.

  python discover.py --template > binding.yaml
      A binding skeleton with every requirement stubbed as `unknown`.

WHY THE COVERAGE REPORT MATTERS

Stage 1 of the pruning funnel is availability — drop anything not computable at
decision time. It is the cheapest filter and it runs first, but it cannot run at
all without knowing which inputs exist. That is why this is the blocker before a
real search: not the catalog, the binding.

A binding entry is one of:
  found     — bound to a real column/table, with `source`
  absent    — we looked, it does not exist
  unknown   — nobody has checked yet

`absent` and `unknown` have opposite consequences and are never merged. An
unknown is work outstanding; an absent is a design constraint.
"""
import argparse
import os
import sys
from collections import defaultdict

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
REQ_PATH = os.path.join(ROOT, "spec", "data_requirements.yaml")

CRIT_ORDER = ["core", "high", "medium", "optional"]
STATUSES = ("found", "absent", "unknown")


def load_requirements(path=REQ_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def iter_reqs(reqs):
    for g in reqs["groups"]:
        for r in g["requirements"]:
            yield g, r


# ---------------------------------------------------------------- checklist
def checklist_md(reqs):
    out = [
        "# Discovery checklist",
        "",
        "What to look for inside the company, and how to tell a real match from a",
        "lookalike. Generated from `spec/data_requirements.yaml` — edit that, not this.",
        "",
        "Record every item as **found** (with the table.column), **absent** (we looked,",
        "it does not exist) or **unknown** (nobody has checked). Absent and unknown are",
        "not the same answer: one is a design constraint, the other is work outstanding.",
        "",
        "```bash",
        "python discover.py --template > binding.yaml   # skeleton to fill in",
        "python discover.py --binding binding.yaml      # what your findings unlock",
        "```",
        "",
    ]

    by_crit = defaultdict(int)
    for _, r in iter_reqs(reqs):
        by_crit[r["criticality"]] += 1
    out += [
        "| Criticality | Count | Meaning |",
        "|---|---|---|",
        f"| core | {by_crit['core']} | the search cannot start without it |",
        f"| high | {by_crit['high']} | an entity class or typology dies without it |",
        f"| medium | {by_crit['medium']} | a family degrades |",
        f"| optional | {by_crit['optional']} | nice to have |",
        "",
        "---",
        "",
    ]

    for g in reqs["groups"]:
        out.append(f"## {g['name']}")
        out.append("")
        if g.get("note"):
            out.append("> " + " ".join(g["note"].split()))
            out.append("")
        for r in sorted(g["requirements"], key=lambda x: CRIT_ORDER.index(x["criticality"])):
            out.append(f"### `{r['id']}` — {r['what']}")
            out.append("")
            out.append(f"**{r['criticality'].upper()}** · {r['source_kind']} · {r['dtype']}")
            out.append("")
            out.append("*Search the catalog for:* " + ", ".join(f"`{a}`" for a in r["aliases"]))
            out.append("")
            unlocks = []
            for key, label in (("unlocks_entities", "entities"),
                               ("unlocks_measures", "measures"),
                               ("unlocks_nongrid", "non-grid families"),
                               ("unlocks_windows", "windows")):
                if r.get(key):
                    v = r[key]
                    v = "ALL" if v == "all" else ", ".join(f"`{x}`" for x in v)
                    unlocks.append(f"{label}: {v}")
            if unlocks:
                out.append("*Unlocks:* " + " · ".join(unlocks))
                out.append("")
            out.append("*Verify:* " + " ".join(r["verify"].split()))
            out.append("")
            if r.get("gotchas"):
                out.append("*Gotcha:* " + " ".join(r["gotchas"].split()))
                out.append("")
        out.append("---")
        out.append("")
    return "\n".join(out)


def template_yaml(reqs):
    out = [
        "# Binding — fill this in as you search. Status is one of:",
        "#   found   (set `source` to the real table.column)",
        "#   absent  (we looked; it does not exist here)",
        "#   unknown (nobody has checked yet)",
        "#",
        "# Then: python discover.py --binding this_file.yaml",
        "",
        "bindings:",
    ]
    for g, r in iter_reqs(reqs):
        out.append(f"  # [{r['criticality']}] {g['name']} — {r['what']}")
        out.append(f"  {r['id']}:")
        out.append("    status: unknown")
        out.append("    source: null        # e.g. warehouse.payments.txn.device_id")
        out.append("    verified: false     # did you run the `verify` step?")
        out.append("    notes: null")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------- coverage
def coverage(reqs, binding, spec):
    """
    Resolve a binding into what is buildable.

    A requirement counts as satisfied only when status == found AND verified.
    An unverified find is deliberately NOT counted: the whole point of the
    verify step is that a plausible-looking column (a session cookie bound as a
    device fingerprint) fails silently and takes weeks of work with it.
    """
    b = binding.get("bindings") or {}
    by_id = {r["id"]: (g, r) for g, r in iter_reqs(reqs)}

    unknown_ids = sorted(set(b) - set(by_id))

    status = {}
    for rid in by_id:
        entry = b.get(rid) or {}
        st = entry.get("status", "unknown")
        if st not in STATUSES:
            st = "unknown"
        status[rid] = {
            "status": st,
            "verified": bool(entry.get("verified")),
            "source": entry.get("source"),
            "satisfied": st == "found" and bool(entry.get("verified")),
        }

    ent_ok, mea_ok, ng_ok = set(), set(), set()
    for rid, s in status.items():
        if not s["satisfied"]:
            continue
        _, r = by_id[rid]
        ent_ok |= set(r.get("unlocks_entities") or [])
        mea_ok |= set(r.get("unlocks_measures") or [])
        ng_ok |= set(r.get("unlocks_nongrid") or [])

    all_ent = {e["id"] for e in spec["entities"]}
    all_mea = {m["id"] for m in spec["measures"]}
    all_ng = {f["id"] for f in spec["nongrid_families"]}

    # A composite is buildable only if every part is.
    comp_ok = {c["id"] for c in spec["composites"] if set(c["parts"]) <= ent_ok}
    all_comp = {c["id"] for c in spec["composites"]}

    blocked_core = sorted(rid for rid, s in status.items()
                          if by_id[rid][1]["criticality"] == "core" and not s["satisfied"])
    unverified = sorted(rid for rid, s in status.items()
                        if s["status"] == "found" and not s["verified"])

    return {
        "status": status,
        "by_id": by_id,
        "unknown_ids": unknown_ids,
        "entities": (ent_ok & all_ent, all_ent),
        "composites": (comp_ok, all_comp),
        "measures": (mea_ok & all_mea, all_mea),
        "nongrid": (ng_ok & all_ng, all_ng),
        "blocked_core": blocked_core,
        "unverified": unverified,
    }


def report(cov, reqs):
    lines = []
    st = cov["status"]
    counts = defaultdict(int)
    for s in st.values():
        counts[s["status"]] += 1

    lines.append("DISCOVERY COVERAGE")
    lines.append("=" * 60)
    lines.append(f"requirements   {len(st)} total — "
                 f"{counts['found']} found, {counts['absent']} absent, "
                 f"{counts['unknown']} unknown")
    if cov["unverified"]:
        lines.append(f"               {len(cov['unverified'])} found but UNVERIFIED "
                     f"(not counted): {', '.join(cov['unverified'])}")
    lines.append("")

    for label, key in (("entities", "entities"), ("composites", "composites"),
                       ("measures", "measures"), ("non-grid families", "nongrid")):
        ok, allv = cov[key]
        pct = 100 * len(ok) / len(allv) if allv else 0
        lines.append(f"{label:20} {len(ok):3}/{len(allv):3}  ({pct:3.0f}%)")
    lines.append("")

    if cov["blocked_core"]:
        lines.append("BLOCKING — core requirements not satisfied:")
        for rid in cov["blocked_core"]:
            _, r = cov["by_id"][rid]
            lines.append(f"  {rid:24} {r['what']}")
            lines.append(f"  {'':24} status={st[rid]['status']}")
        lines.append("")
        lines.append("  The search cannot start. These are not degradations.")
        lines.append("")

    missing_ent = sorted(cov["entities"][1] - cov["entities"][0])
    if missing_ent:
        lines.append(f"entities not yet buildable ({len(missing_ent)}): "
                     f"{', '.join(missing_ent)}")
        lines.append("")
    missing_ng = sorted(cov["nongrid"][1] - cov["nongrid"][0])
    if missing_ng:
        lines.append(f"non-grid families not yet buildable ({len(missing_ng)}): "
                     f"{', '.join(missing_ng)}")
        lines.append("")

    unresolved = [rid for rid, s in st.items() if s["status"] == "unknown"]
    if unresolved:
        ranked = sorted(unresolved,
                        key=lambda r: CRIT_ORDER.index(cov["by_id"][r][1]["criticality"]))
        lines.append("NEXT — unchecked, highest criticality first:")
        for rid in ranked[:12]:
            _, r = cov["by_id"][rid]
            lines.append(f"  [{r['criticality']:8}] {rid:24} {r['what']}")
        if len(ranked) > 12:
            lines.append(f"  ... and {len(ranked) - 12} more")
        lines.append("")

    if cov["unknown_ids"]:
        lines.append(f"WARNING — binding names {len(cov['unknown_ids'])} requirement(s) "
                     f"not in the registry: {', '.join(cov['unknown_ids'])}")
        lines.append("")

    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Requirements out, binding in.")
    ap.add_argument("--checklist", action="store_true", help="write DISCOVERY_CHECKLIST.md")
    ap.add_argument("--template", action="store_true", help="print a binding skeleton")
    ap.add_argument("--binding", help="a filled binding YAML; prints a coverage report")
    ap.add_argument("--requirements", default=REQ_PATH)
    ap.add_argument("--out", default=os.path.join(ROOT, "DISCOVERY_CHECKLIST.md"))
    a = ap.parse_args(argv)

    reqs = load_requirements(a.requirements)

    if a.template:
        print(template_yaml(reqs))
        return 0

    if a.binding:
        with open(a.binding) as f:
            binding = yaml.safe_load(f) or {}
        with open(os.path.join(ROOT, "feature_space.yaml")) as f:
            spec = yaml.safe_load(f)
        cov = coverage(reqs, binding, spec)
        print(report(cov, reqs))
        return 1 if cov["blocked_core"] else 0

    if a.checklist or True:
        with open(a.out, "w") as f:
            f.write(checklist_md(reqs))
        n = sum(1 for _ in iter_reqs(reqs))
        print(f"wrote {a.out} — {n} requirements across {len(reqs['groups'])} groups")
        return 0


if __name__ == "__main__":
    sys.exit(main())
