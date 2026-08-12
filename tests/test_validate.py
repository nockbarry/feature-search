"""
test_validate.py — the validator must reject the bugs it was written for.

A validator that passes everything is worse than no validator: it converts an
unchecked file into a file everyone believes is checked. Each test below mutates
a valid spec into a specific known-bad state and asserts the error is raised.

The stray-key case is not hypothetical. It shipped:

    - {id: cb_fraud_rate, name: CB rate, fraud reason group, family: outcome, ...}

YAML parses the unquoted comma as a new key, so name becomes "CB rate" and
"fraud reason group" becomes a null-valued stray. cb_fraud_rate and
cb_nonfr_rate then render identically in the workbook — the fraud/non-fraud
split the whole search depends on, invisible to whoever is reading the catalog.
"""
import copy
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nongrid_features import NONGRID  # noqa: E402
from validate import check  # noqa: E402


@pytest.fixture
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


def errs(spec, nongrid=None):
    return check(copy.deepcopy(spec), nongrid if nongrid is not None else NONGRID)[0]


def test_shipped_spec_is_valid(spec):
    """The committed spec must pass. If this fails, the repo is shipping a bad catalog."""
    e, _ = check(spec, NONGRID)
    assert e == [], "\n".join(e)


def test_unquoted_comma_in_flow_mapping_is_caught():
    """The real bug: `name: CB rate, fraud reason group` splits into a stray key."""
    bad = yaml.safe_load(
        "measures:\n"
        "  - {id: cb_fraud_rate, name: CB rate, fraud reason group, family: outcome, "
        "label_dependent: true, dtype: rate, priority: 1}\n"
    )
    m = bad["measures"][0]
    # Confirm the parse really is lossy before asserting we catch it.
    assert m["name"] == "CB rate"
    assert "fraud reason group" in m

    e = check(bad, [])[0]
    assert any("stray key 'fraud reason group'" in x for x in e)


def test_missing_required_key_is_caught(spec):
    del spec["entities"][0]["availability"]
    assert any("missing required key 'availability'" in x for x in errs(spec))


def test_duplicate_entity_id_is_caught(spec):
    spec["entities"].append(dict(spec["entities"][0]))
    assert any("duplicate id" in x for x in errs(spec))


def test_entity_and_composite_sharing_an_id_is_caught(spec):
    """Both are group-by keys; a collision makes feature names ambiguous."""
    spec["composites"].append({"id": spec["entities"][0]["id"],
                               "parts": ["device", "bin8"], "hypothesis": "x"})
    assert any("defined in both" in x for x in errs(spec))


def test_bad_enum_value_is_caught(spec):
    spec["entities"][0]["adversarial_cost"] = "moderate"
    assert any("adversarial_cost" in x for x in errs(spec))


def test_composite_referencing_unknown_entity_is_caught(spec):
    spec["composites"][0]["parts"] = ["device", "not_an_entity"]
    assert any("not a declared entity" in x for x in errs(spec))


def test_ladder_referencing_unknown_window_is_caught(spec):
    spec["resolution_ladder"]["L0"]["windows"]["outcome"] = ["w30d", "w42d"]
    assert any("'w42d' is not a declared window" in x for x in errs(spec))


def test_measure_family_dropped_from_a_rung_is_caught(spec):
    """
    The coverage invariant. Deleting a family's windows at a rung silently
    removes every feature in that family from that catalog — a blind spot, not
    a coarser view, and no later refinement recovers it.
    """
    del spec["resolution_ladder"]["L0"]["windows"]["behavior"]
    e = errs(spec)
    assert any("measure family 'behavior' has no window" in x for x in e)


def test_emptying_an_entity_class_at_a_rung_is_caught(spec):
    spec["resolution_ladder"]["L0"]["entity_granularity"]["email"] = []
    assert any("gates coverage rather than resolution" in x for x in errs(spec))


def test_all_sentinel_is_accepted(spec):
    """L2 uses `ALL`; it must not be mistaken for a list of ids."""
    assert spec["resolution_ladder"]["L2"]["transforms"] == "ALL"
    assert errs(spec) == []


def test_meta_label_must_name_a_real_label(spec):
    spec["meta"]["label"] = "chargeback_fraud"   # the value that shipped
    assert any("is not a declared label" in x for x in errs(spec))


def test_non_numeric_maturity_is_caught(spec):
    """The split protocol derives its embargo width from this field."""
    spec["labels"][0]["maturity_days_90pct"] = "about a month"
    assert any("maturity_days_90pct must be numeric" in x for x in errs(spec))


def test_uncovered_measure_dtype_is_caught(spec):
    for a in spec["aggregations"]:
        a["applies_to"] = [d for d in a["applies_to"] if d != "money"]
    assert any("no aggregation accepts dtype 'money'" in x for x in errs(spec))


def test_nongrid_unknown_typology_is_caught(spec):
    bad = [("graph", "f1", "d", "i", "stolen_cnp,not_a_typology", "streaming", "LOW", "months")]
    assert any("typology 'not_a_typology' is not declared" in x for x in errs(spec, bad))


def test_nongrid_unknown_family_is_caught(spec):
    bad = [("telepathy", "f1", "d", "i", "stolen_cnp", "streaming", "LOW", "months")]
    assert any("is not in nongrid_families" in x for x in errs(spec, bad))


def test_nongrid_duplicate_name_is_caught(spec):
    row = ("graph", "f1", "d", "i", "stolen_cnp", "streaming", "LOW", "months")
    assert any("duplicate feature name" in x for x in errs(spec, [row, row]))


def test_typology_with_no_nongrid_coverage_warns(spec):
    """
    Warning, not error — but it is the check that catches the friendly-fraud
    hole, where the typology's predictors are near-disjoint from every other
    row's and thin coverage reads as coverage.
    """
    thin = [("graph", "f1", "d", "i", "stolen_cnp", "streaming", "LOW", "months")]
    _, warnings = check(copy.deepcopy(spec), thin)
    assert any("typology 'friendly'" in w for w in warnings)


def test_family_floor_shortfall_warns(spec):
    thin = [("graph", "f1", "d", "i", "stolen_cnp", "streaming", "LOW", "months")]
    _, warnings = check(copy.deepcopy(spec), thin)
    assert any("nongrid family 'graph'" in w and "floor is" in w for w in warnings)
