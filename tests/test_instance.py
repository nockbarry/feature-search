"""
test_instance.py — the repo must not ship facts about anyone's environment.

The failure this guards is not a crash. It is an agent reading `40 ms` or
`30 days` in the brief, treating it as ground truth about the system it was
pointed at, and completing a search that describes someone else's deployment.
Nothing errors. The backtest looks fine.

So two properties are pinned:

  1. Instance values are declared in spec/instance.yaml as TODO, and
     validate_instance.py refuses to pass while any remain.
  2. The prose does not quietly restate them as fact. Where the brief shows a
     number it must be marked EXAMPLE and pointed at the instance file.
"""
import copy
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from validate_instance import check, is_todo, walk  # noqa: E402

INSTANCE = os.path.join(ROOT, "spec", "instance.yaml")


@pytest.fixture(scope="module")
def inst():
    with open(INSTANCE) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


def filled(inst, **over):
    """A realistic, internally consistent instance — the shape a real one takes."""
    out = {
        "model": {"name": "m", "champion_name": "c", "decision_point": "pre_authorization",
                  "decision": "block", "champion_block_rate_pct": 4.2, "release_rate_pct": 0.5},
        "serving": {"latency_budget_ms": 40, "budget_is_per": "vector",
                    "supported": {"realtime_lookup": True, "streaming_aggregate": True,
                                  "vendor_enrichment_inline": False,
                                  "graph_traversal_1hop": False, "batch_precompute": True}},
        "funnel": {"states": ["SEN", "DEN", "ERR"], "outcome_eligible": ["SEN"], "notes": "-"},
        "maturity": {"primary_label": "cb_fraud", "days_to_90pct": 30, "days_to_99pct": 90,
                     "longest_feature_window_days": 365, "measured_on": "-",
                     "per_label_days_to_90pct": {"cb_fraud": 30}},
        "volume": {"txn_per_day": 1_000_000, "positive_rate_bps": 20,
                   "history_available_months": 24, "min_support_n": 30},
        "warehouse": {"kind": "bigquery", "project": "p", "datasets": ["d"],
                      "known_tables": ["d.t"], "location": "US", "max_scan_gb_per_query": 50},
        "fitness": dict(inst["fitness"]),
        "governance": {"prohibited_fields": [], "requires_signoff": [],
                       "adverse_action_required": False, "data_residency": "none"},
    }
    for k, v in over.items():
        out[k] = {**out[k], **v} if isinstance(v, dict) else v
    return out


# ------------------------------------------------------------ shipped state
def test_shipped_instance_is_all_todo(inst):
    """The template must ship unfilled. A shipped value is a value someone
    inherits."""
    vals = [(p, v) for p, v in walk(inst) if p.split(".")[0] != "fitness" and p != "version"
            and not p.startswith("serving.budget_is_per")]
    assert vals, "instance has no values to check"
    not_todo = [p for p, v in vals if not is_todo(v)]
    assert not not_todo, f"these ship with a concrete value and would be inherited: {not_todo}"


def test_fitness_thresholds_ship_filled(inst):
    """Fitness bars are method, not environment — conservative defaults are
    correct and a TODO there would just block the survey."""
    for p, v in walk(inst["fitness"]):
        assert not is_todo(v), f"fitness.{p} should ship with a default"


def test_shipped_template_fails_validation(spec, inst):
    e, _ = check(copy.deepcopy(inst), spec)
    assert e, "the unfilled template must not validate — otherwise TODOs sail through"
    assert any("TODO" in x for x in e)


def test_filled_instance_passes(spec, inst):
    e, _ = check(filled(inst), spec)
    assert e == [], "\n".join(e)


# ------------------------------------------------------- consistency checks
def test_maturity_ordering_is_checked(spec, inst):
    e, _ = check(filled(inst, maturity={"days_to_90pct": 90, "days_to_99pct": 30}), spec)
    assert any("days_to_99pct" in x for x in e)


def test_embargo_narrower_than_a_real_window_is_caught(spec, inst):
    """The registry has a 365-day window. Declaring a shorter longest-window
    would produce an embargo too narrow for a feature actually being built."""
    e, _ = check(filled(inst, maturity={"longest_feature_window_days": 30}), spec)
    assert any("embargo would be too" in x for x in e)


def test_block_without_release_is_an_error(spec, inst):
    """Blocking with no release program means censoring cannot be corrected by
    any method — a ceiling on the search that must be stated, not discovered."""
    e, _ = check(filled(inst, model={"champion_block_rate_pct": 4.2, "release_rate_pct": 0}), spec)
    assert any("cannot be de-biased" in x for x in e)


def test_funnel_must_have_an_outcome_eligible_state(spec, inst):
    e, _ = check(filled(inst, funnel={"states": ["A", "B"], "outcome_eligible": []}), spec)
    assert any("no state is outcome_eligible" in x for x in e)
    e, _ = check(filled(inst, funnel={"states": ["A"], "outcome_eligible": ["Z"]}), spec)
    assert any("not in states" in x for x in e)


def test_nothing_servable_is_an_error(spec, inst):
    e, _ = check(filled(inst, serving={"supported": dict.fromkeys(
        ["realtime_lookup", "streaming_aggregate", "vendor_enrichment_inline",
         "graph_traversal_1hop", "batch_precompute"], False)}), spec)
    assert any("no feature in the catalog is buildable" in x for x in e)


def test_session_ratio_threshold_of_one_is_rejected(spec, inst):
    """At 1.0 a per-session cookie passes as a device fingerprint, which is the
    mis-bind the threshold exists to catch."""
    e, _ = check(filled(inst, fitness={**inst["fitness"], "max_entity_to_session_ratio": 1.0}),
                 spec)
    assert any("passes as a device fingerprint" in x for x in e)


def test_unknown_is_accepted_but_surfaced(spec, inst):
    e, w = check(filled(inst, model={"release_rate_pct": "unknown"}), spec)
    assert not any("TODO" in x for x in e)
    assert any("unknown" in x for x in w)


# ------------------------------------------------------------- the prose
def test_brief_does_not_state_instance_values_as_fact():
    """
    The original brief opened with "challenger model `dnn_v50_rc3`" and asserted
    a 40 ms budget and a 30-day maturity in its own voice. An agent reads that as
    a description of the system it was pointed at.
    """
    with open(os.path.join(ROOT, "AGENT_BRIEF.md")) as f:
        text = f.read()
    assert "spec/instance.yaml" in text, "the brief must point at the instance file"
    assert "EXAMPLE" in text, "example values must be labelled as examples"
    head = text.split("## 3.")[0]
    for token in ("dnn_v50_rc3", "gbm_v412"):
        if token in head:
            line = next(ln for ln in head.splitlines() if token in ln)
            assert "EXAMPLE" in line, \
                f"{token} appears outside an EXAMPLE-marked context: {line.strip()!r}"


def test_integration_doc_states_what_the_user_provides():
    p = os.path.join(ROOT, "INTEGRATION.md")
    assert os.path.exists(p), "INTEGRATION.md is the front door and must exist"
    text = open(p).read()
    for needle in ("spec/instance.yaml", "discover.py --binding", "validate_queue.py",
                   "make survey", "session cookie"):
        assert needle in text, f"INTEGRATION.md does not mention {needle}"


def test_cli_allow_todo_mode_passes_for_repo_ci():
    """Repo CI checks the template is well-formed; it cannot require the template
    to be filled, or the repo could never be green."""
    r = subprocess.run([sys.executable, "validate_instance.py", "--allow-todo"],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
