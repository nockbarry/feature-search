"""
validate_instance.py — refuse to proceed on someone else's environment.

This repo is a starting point, not a specification of any one system. The
method is portable; the numbers are not. Latency budget, maturity horizon,
champion block rate, funnel state names and base rates all vary per
deployment, and each one silently determines downstream correctness:

  * maturity.days_to_90pct sets the purge and embargo widths in the split
    protocol. Inherit someone else's 30 and your validation folds are
    contaminated in a way no metric reveals.
  * serving.latency_budget_ms and serving.supported decide stage 1 of the
    pruning funnel. Inherit them and you screen against a serving path that
    does not exist.
  * volume.positive_rate_bps sets support floors. Inherit it and you expose
    raw rates on cells that cannot resolve.

So an unfilled `TODO` is an error, not a warning. The failure mode this
prevents is not a crash — it is a search that completes, looks reasonable,
and describes a system nobody operates.

`unknown` is always an acceptable value. It records "we looked and cannot
answer yet" and blocks only the work that genuinely depends on it. Leaving
the shipped EXAMPLE in place records nothing at all.

Run:  python validate_instance.py [--instance spec/instance.yaml]
Exit: 0 clean, 1 if any TODO remains or a filled value is inconsistent.
"""
import argparse
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT = os.path.join(ROOT, "spec", "instance.yaml")

# Consequence of leaving each section unfilled. Printed on failure, because
# "TODO at model.champion_block_rate_pct" is not actionable on its own.
CONSEQUENCE = {
    "model": "censoring correction and reject inference cannot be scoped",
    "serving": "stage 1 (availability) screens against an imaginary serving path",
    "funnel": "outcome denominators are wrong, so every rate feature is miscalibrated",
    "maturity": "purge and embargo widths are wrong; validation folds are contaminated",
    "volume": "support floors are wrong; raw rates get exposed on cells that cannot resolve",
    "warehouse": "the agent has nowhere to start looking and will guess table names",
    "fitness": "field-fitness has no bar, so any column that exists looks acceptable",
    "governance": "prohibited fields may be built and shipped before anyone notices",
}

UNKNOWN = {"unknown", "none", "n/a"}


def walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, node


def is_todo(v):
    return isinstance(v, str) and v.strip().upper() == "TODO"


def is_unknown(v):
    return isinstance(v, str) and v.strip().lower() in UNKNOWN


def check(inst, spec=None):
    errors, warnings = [], []
    E, W = errors.append, warnings.append

    todos = [p for p, v in walk(inst) if is_todo(v)]
    by_section = {}
    for p in todos:
        by_section.setdefault(p.split(".")[0], []).append(p)
    for section, paths in sorted(by_section.items()):
        E(f"{len(paths)} unfilled TODO(s) under `{section}` — "
          f"{CONSEQUENCE.get(section, 'downstream work depends on this')}")
        for p in paths[:6]:
            E(f"    {p}")
        if len(paths) > 6:
            E(f"    ... and {len(paths) - 6} more")

    unknowns = [p for p, v in walk(inst) if is_unknown(v)]
    if unknowns:
        W(f"{len(unknowns)} value(s) recorded `unknown` — honest, but the work "
          f"depending on them is blocked: {', '.join(unknowns[:6])}")

    if todos:
        # Consistency checks below would report noise against placeholders.
        return errors, warnings

    # ---- consistency, once the numbers are real -----------------------------
    mat = inst.get("maturity") or {}
    d90, d99 = mat.get("days_to_90pct"), mat.get("days_to_99pct")
    if isinstance(d90, (int, float)) and isinstance(d99, (int, float)) and d99 < d90:
        E(f"maturity: days_to_99pct ({d99}) < days_to_90pct ({d90})")

    longest = mat.get("longest_feature_window_days")
    if isinstance(d90, (int, float)) and isinstance(longest, (int, float)):
        embargo = max(d90, longest)
        W(f"embargo must be at least {embargo:g} days "
          f"(max of maturity {d90:g} and longest window {longest:g}) — "
          f"see docs/split_protocol.md")

    if spec:
        wins = [w for w in spec.get("windows") or [] if isinstance(w, dict)]
        secs = [w["seconds"] for w in wins if isinstance(w.get("seconds"), (int, float))
                and w["seconds"] > 0]
        if secs and isinstance(longest, (int, float)):
            reg = max(secs) / 86400
            if reg > longest:
                E(f"maturity.longest_feature_window_days = {longest:g} but the windows "
                  f"registry contains a {reg:g}-day window — the embargo would be too "
                  f"narrow for a window you are actually building")

        # History has to cover the longest window plus a PSI baseline.
        months = (inst.get("volume") or {}).get("history_available_months")
        if isinstance(months, (int, float)) and secs:
            need = max(secs) / 86400 / 30
            if months < need + 12:
                W(f"volume.history_available_months = {months:g} but the longest window "
                  f"needs ~{need:.1f} months and PSI needs a 12-month baseline on top")

    srv = inst.get("serving") or {}
    sup = srv.get("supported") or {}
    if sup and not any(v is True for v in sup.values()):
        E("serving.supported: nothing is supported — no feature in the catalog is buildable")
    if sup.get("streaming_aggregate") is False:
        W("serving.supported.streaming_aggregate is false — every windowed entity "
          "aggregate becomes batch-only or unbuildable. That is most of the grid.")
    if sup.get("graph_traversal_1hop") is False:
        W("serving.supported.graph_traversal_1hop is false — the graph family is "
          "unbuildable at decision time; it can still inform batch features")

    mdl = inst.get("model") or {}
    blk, rel = mdl.get("champion_block_rate_pct"), mdl.get("release_rate_pct")
    if isinstance(blk, (int, float)) and isinstance(rel, (int, float)):
        if blk > 0 and rel == 0:
            E(f"model: champion blocks {blk:g}% with a release rate of 0 — entity outcome "
              f"rates cannot be de-biased in the blocked region by any method. Either "
              f"start a release program or record this as a stated ceiling on the search.")

    fun = inst.get("funnel") or {}
    states, eligible = fun.get("states") or [], fun.get("outcome_eligible") or []
    for s in eligible:
        if states and s not in states:
            E(f"funnel: outcome_eligible names '{s}', which is not in states {states}")
    if states and not eligible:
        E("funnel: no state is outcome_eligible — nothing can produce the label")

    fit = inst.get("fitness") or {}
    ratio = fit.get("max_entity_to_session_ratio")
    if isinstance(ratio, (int, float)) and ratio >= 1.0:
        E(f"fitness.max_entity_to_session_ratio = {ratio:g} — at 1.0 a per-session "
          f"cookie passes as a device fingerprint, which is the mis-bind this "
          f"threshold exists to catch")

    return errors, warnings


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the instance profile is filled and consistent.")
    ap.add_argument("--instance", default=DEFAULT)
    ap.add_argument("--spec", default=os.path.join(ROOT, "feature_space.yaml"))
    ap.add_argument("--allow-todo", action="store_true",
                    help="report TODOs as warnings (for repo CI on the shipped template)")
    a = ap.parse_args(argv)

    with open(a.instance) as f:
        inst = yaml.safe_load(f)
    spec = None
    if os.path.exists(a.spec):
        with open(a.spec) as f:
            spec = yaml.safe_load(f)

    errors, warnings = check(inst, spec)

    if a.allow_todo:
        warnings = [e for e in errors if "TODO" in e or e.startswith("    ")] + warnings
        errors = [e for e in errors if "TODO" not in e and not e.startswith("    ")]

    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"ERROR {e}")

    if errors:
        print(f"\n{len(errors)} error(s) — {a.instance} is not ready. "
              f"The method is portable; these numbers are not.")
        return 1
    print(f"{a.instance}: ok ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
