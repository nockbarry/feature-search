"""
pack.py — assemble the model-facing context pack.

The repo serves two audiences. Humans and CI need the build pipeline, the
determinism and validation tests, the Makefile and the workbook. A model sent to
search the space needs none of that — it needs the specification, the executable
definition of point-in-time correctness, and a work queue.

Handing over the whole repo costs ~169k tokens, of which ~117k is the work queue
and ~42k of *that* is an .xlsx a model reads strictly worse than CSV. This target
produces a pack of roughly 40% the size with nothing a model reasons from removed.

What gets dropped from the queue, and why it is safe:

  level, stage, tier, window_type   constant across a single rung — header
                                    material, stated once in the manifest
  entity, entity_class              lookups on entity_id, which is the first
                                    segment of feature_name
  measure, measure_family           lookups on the measure id, second segment
  aggregation, window, filter,      the remaining segments of feature_name
  transform

Every one is recoverable from feature_name plus feature_space.yaml, which the
model is already holding. `decode()` below does the recovery, and
tests/test_pack.py asserts it reproduces the dropped columns exactly — the claim
is verified, not asserted.

The screening columns (availability, leakage_risk, min_support_n,
adversarial_halflife, hypothesis) are also technically derivable, but they are
what the agent reads on every single row during stages 1-3. Forcing a YAML
lookup per row to save bytes would trade the agent's attention for disk, which
is the wrong direction.

Run:  python pack.py [--level L0] [--out pack]
"""
import argparse
import csv
import os
import shutil
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))

# Copied verbatim into the pack, in the order the manifest tells the model to read.
SPEC_FILES = [
    ("AGENT_BRIEF.md", "Mission, grammar, correctness rules, pruning funnel, definition of done."),
    ("feature_space.yaml", "The six slot registries. Decodes every feature_name in the queue."),
    ("nongrid_features.py", "143 seeded features the grammar cannot express. Expected to carry most of the lift."),
    ("docs/split_protocol.md", "Purge and embargo under label maturity. Read before cutting any split."),
    ("docs/null_semantics.md", "Null encoding, missingness indicators, feature-outage policy."),
    ("docs/label_definition.md", "Reason-code mapping and dispute-lifecycle edge cases."),
]

# The discovery step comes BEFORE the search: nothing in the queue is screenable
# until the inputs are bound to real columns. These ship with the pack.
DISCOVERY_FILES = [
    ("DISCOVERY_CHECKLIST.md", "START HERE. What to look for in the company's data, what it is called, how to verify a match."),
    ("spec/data_requirements.yaml", "The machine-readable source of the checklist. Feeds the coverage report."),
    ("spec/binding.example.yaml", "A worked, deliberately incomplete binding. Copy the shape, not the values."),
    ("discover.py", "`--template` for a binding skeleton; `--binding f.yaml` reports what your findings unlock."),
]

CORRECTNESS_FILES = [
    ("pit_aggregate_template.sql", "Warehouse implementation of the two-clock rule, shrinkage, censoring correction."),
    ("pit_reference.py", "The same logic in dependency-free Python. Executable spec."),
    ("tests/test_pit_leakage.py", "ACCEPTANCE TEST. Hand-computed fixtures your SQL must reproduce."),
]

# Columns the queue keeps. Everything else is recovered by decode().
QUEUE_KEEP = [
    "feature_name",
    "availability", "leakage_risk", "min_support_n", "adversarial_halflife",
    "hypothesis",
    # agent fill-in, empty on delivery
    "status", "drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank",
]

DROPPED = ["level", "stage", "tier", "window_type", "entity", "entity_id", "entity_class",
           "measure", "measure_family", "aggregation", "window", "filter", "transform"]


def decode(feature_name, spec, entities_by_id):
    """
    Recover the slot columns from a feature name.

    Name grammar (expand_catalog.fname):
        {entity}__{measure}__{agg}__{window}[__f_{filter}][__t_{transform}]

    Absent segments carry their identity value: filter 'none', transform 'raw'.
    """
    parts = feature_name.split("__")
    if len(parts) < 4:
        raise ValueError(f"malformed feature name: {feature_name!r}")
    ent_id, mea_id, agg_id, win_id = parts[:4]
    flt_id, tra_id = "none", "raw"
    for extra in parts[4:]:
        if extra.startswith("f_"):
            flt_id = extra[2:]
        elif extra.startswith("t_"):
            tra_id = extra[2:]
        else:
            raise ValueError(f"unrecognised segment {extra!r} in {feature_name!r}")

    by_id = {reg: {r["id"]: r for r in spec[reg]}
             for reg in ("measures", "windows", "filters", "aggregations", "transforms")}
    ent = entities_by_id[ent_id]
    mea = by_id["measures"][mea_id]
    win = by_id["windows"][win_id]
    return {
        "entity": ent["name"],
        "entity_id": ent_id,
        "entity_class": ent["class"],
        "measure": mea["name"],
        "measure_family": mea["family"],
        "aggregation": by_id["aggregations"][agg_id]["name"],
        "window": win["label"],
        "window_type": win["type"],
        "filter": by_id["filters"][flt_id]["label"],
        "transform": by_id["transforms"][tra_id]["name"],
    }


def manifest(level, rows, sizes, constants):
    tot = sum(sizes.values())
    lines = [
        "# Context pack — feature search",
        "",
        f"Work queue: **{rows} candidates** at the `{level}` rung. "
        f"Pack size ~{tot / 4000:.0f}k tokens.",
        "",
        "## Read in this order",
        "",
    ]
    n = 1
    for group, files in (("Discovery — do this first", DISCOVERY_FILES),
                         ("Specification", SPEC_FILES),
                         ("Correctness", CORRECTNESS_FILES)):
        lines.append(f"**{group}**")
        lines.append("")
        for name, why in files:
            base = name if name.startswith("spec/") else os.path.basename(name)
            lines.append(f"{n}. `{base}` — {why}")
            n += 1
        lines.append("")
    lines += [
        f"{n}. `queue.csv` — the work queue. Stream it; do not load it whole.",
        "",
        "## The queue",
        "",
        "One row per candidate feature. Columns `status`, `drop_reason`, `iv`, `psi_12m`,",
        "`coverage_pct` and `shap_rank` are empty on delivery — they are yours to fill.",
        "",
        "Slot columns are omitted because they are recoverable from `feature_name`:",
        "",
        "```",
        "{entity}__{measure}__{aggregation}__{window}[__f_{filter}][__t_{transform}]",
        "```",
        "",
        "Absent segments carry their identity value: filter `none`, transform `raw`.",
        "Resolve each id against the registries in `feature_space.yaml`.",
        "",
        "These are constant across this rung and are therefore stated once here rather",
        "than repeated on every row:",
        "",
    ]
    lines += [f"- `{k}` = `{v}`" for k, v in sorted(constants.items())]
    lines += [
        "",
        "## Before you report any number",
        "",
        "`test_pit_leakage.py` is an acceptance test, not a repo test. Whatever you",
        "implement in the warehouse must reproduce its hand-computed fixtures — including",
        "the chargeback that happened on day 1, was reported on day 40, and must be",
        "invisible when scoring at day 20. If your implementation and",
        "`naive_aggregate_LEAKY` in `pit_reference.py` ever agree on that fixture, the",
        "point-in-time filter has been lost.",
        "",
        "## Not in this pack",
        "",
        "The build pipeline (`expand_catalog.py`, `validate.py`, `build_workbook.py`), the",
        "spec/compatibility/determinism tests, CI and the `.xlsx` workbook. They generate",
        "and guard this pack; they are not inputs to the search. `feature_catalog.xlsx` is",
        "the human fill-in surface — use `queue.csv` instead.",
        "",
        "## Before you screen anything: bind the inputs",
        "",
        "No schema binding exists yet. The SQL template names `txn`, `chargeback`,",
        "`release_log`, `approved_traffic`, `v_label_arrival`, `v_parent_rate_pit`,",
        "`shadow_features`, `offline_features` and `date_spine`, and no entity id is mapped",
        "to a warehouse column. **Do not guess column names.** A wrong bind does not error,",
        "it produces confident nonsense.",
        "",
        "Work `DISCOVERY_CHECKLIST.md` first — 42 requirements, each with the names it",
        "appears under in real systems and a verification step that separates a real match",
        "from a lookalike. Then:",
        "",
        "```bash",
        "python discover.py --template > binding.yaml   # skeleton",
        "# ... fill it in as you search ...",
        "python discover.py --binding binding.yaml      # what your findings unlock",
        "```",
        "",
        "Record every requirement as `found` / `absent` / `unknown`. Absent and unknown",
        "are different answers with different consequences and must never be merged.",
        "A `found` that has not passed its verify step does not count.",
        "",
        "Stage 1 of the pruning funnel is availability, and it cannot run until this is",
        "done. The coverage report tells you which entities, measures and non-grid",
        "families are buildable — that is your real starting queue, not the full 1,167.",
        "",
        "## Other known gaps",
        "",
        "Reject-inference method and the cost-sensitive objective are unspecified; see",
        "the \"Not yet written\" section of `AGENT_BRIEF.md`.",
        "",
    ]
    return "\n".join(lines)


def build(level="L0", out="pack", candidates="candidates.csv", spec_path="feature_space.yaml"):
    with open(os.path.join(ROOT, spec_path)) as f:
        spec = yaml.safe_load(f)

    cpath = os.path.join(ROOT, candidates)
    if not os.path.exists(cpath):
        print(f"{cpath} not found.\nIt is a build artifact, not source. Generate it first:\n"
              f"    make catalog LEVEL={level}", file=sys.stderr)
        return 1

    with open(cpath, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{cpath} is empty.", file=sys.stderr)
        return 1

    # The pack drops 13 columns claiming they are recoverable from feature_name.
    # Verify that on the rows actually being shipped, not just in the test suite:
    # a name the model cannot decode is a row it cannot screen.
    from expand_catalog import build_entities
    ents = {e["id"]: e for e in build_entities(spec)}
    undecodable = []
    for r in rows:
        try:
            decode(r["feature_name"], spec, ents)
        except (ValueError, KeyError) as exc:
            undecodable.append(f"  {r['feature_name']}: {exc}")
    if undecodable:
        print(f"{len(undecodable)} feature name(s) do not decode against {spec_path}; "
              f"the queue would ship rows the model cannot resolve:", file=sys.stderr)
        print("\n".join(undecodable[:10]), file=sys.stderr)
        return 1

    outdir = os.path.join(ROOT, out)
    shutil.rmtree(outdir, ignore_errors=True)
    os.makedirs(outdir)

    sizes = {}
    # spec/ paths are preserved so discover.py finds data_requirements.yaml
    # where it expects it; everything else flattens.
    for name, _ in DISCOVERY_FILES + SPEC_FILES + CORRECTNESS_FILES:
        src = os.path.join(ROOT, name)
        rel = name if name.startswith("spec/") else os.path.basename(name)
        dst = os.path.join(outdir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        sizes[rel] = os.path.getsize(dst)

    qpath = os.path.join(outdir, "queue.csv")
    with open(qpath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QUEUE_KEEP, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    sizes["queue.csv"] = os.path.getsize(qpath)

    # Columns with a single value across the rung: state once, not 1167 times.
    constants = {c: rows[0][c] for c in ("level", "stage", "tier", "window_type")
                 if len({r[c] for r in rows}) == 1}

    mpath = os.path.join(outdir, "MANIFEST.md")
    with open(mpath, "w") as f:
        f.write(manifest(level, len(rows), sizes, constants))

    full = os.path.getsize(cpath) + sum(
        os.path.getsize(os.path.join(ROOT, n))
        for n, _ in DISCOVERY_FILES + SPEC_FILES + CORRECTNESS_FILES)
    packed = sum(sizes.values()) + os.path.getsize(mpath)
    print(f"wrote {outdir}/ — {len(rows)} candidates, {len(sizes) + 1} files")
    print(f"  ~{packed / 4000:.0f}k tokens (queue ~{sizes['queue.csv'] / 4000:.0f}k), "
          f"down from ~{full / 4000:.0f}k")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Assemble the model-facing context pack.")
    ap.add_argument("--level", default="L0")
    ap.add_argument("--out", default="pack")
    ap.add_argument("--candidates", default="candidates.csv")
    a = ap.parse_args(argv)
    return build(level=a.level, out=a.out, candidates=a.candidates)


if __name__ == "__main__":
    sys.exit(main())
