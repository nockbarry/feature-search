"""
test_discover.py — the discovery checklist must be complete and honest.

Two failure modes worth guarding:

  Incomplete — an agent satisfies every requirement in the file and still
  cannot build some entity, because nothing in the requirements ever unlocked
  it. The checklist then reads as done while the search has a hole. That is
  what test_every_registry_id_is_reachable catches; it already found two
  measures (d_country, hour_entropy) that no requirement unlocked.

  Dishonest — a plausible-looking column bound without running the verify step.
  The canonical case is a per-session cookie bound as a device fingerprint: it
  looks right in a schema browser, and every device-keyed feature silently
  becomes worthless. Unverified finds must not count toward coverage.
"""
import os
import subprocess
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from discover import (  # noqa: E402
    CRIT_ORDER,
    checklist_md,
    coverage,
    iter_reqs,
    load_requirements,
    template_yaml,
)

EXAMPLE = os.path.join(ROOT, "spec", "binding.example.yaml")


@pytest.fixture(scope="module")
def reqs():
    return load_requirements()


@pytest.fixture(scope="module")
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


def unlocked_ids(reqs, key):
    out = set()
    for _, r in iter_reqs(reqs):
        v = r.get(key)
        if v and v != "all":
            out |= set(v)
    return out


def test_every_registry_id_is_reachable(reqs, spec):
    """
    Completeness. Every entity, measure and non-grid family in feature_space.yaml
    must be unlocked by at least one requirement, or the checklist can be fully
    satisfied while part of the search stays unbuildable and nobody is told.
    """
    gaps = []
    for key, registry, label in (("unlocks_entities", "entities", "entity"),
                                 ("unlocks_measures", "measures", "measure"),
                                 ("unlocks_nongrid", "nongrid_families", "non-grid family")):
        declared = {x["id"] for x in spec[registry]}
        gaps += [f"{label} '{i}' is unlocked by no requirement"
                 for i in sorted(declared - unlocked_ids(reqs, key))]
    assert not gaps, "\n".join(gaps)


def test_unlocks_never_reference_unknown_ids(reqs, spec):
    """The mirror check: a typo in an unlocks list would credit coverage that
    does not exist."""
    bad = []
    for key, registry in (("unlocks_entities", "entities"),
                          ("unlocks_measures", "measures"),
                          ("unlocks_nongrid", "nongrid_families")):
        declared = {x["id"] for x in spec[registry]}
        bad += [f"{key}: '{i}' is not in {registry}"
                for i in sorted(unlocked_ids(reqs, key) - declared)]
    assert not bad, "\n".join(bad)


def test_requirement_ids_are_unique(reqs):
    ids = [r["id"] for _, r in iter_reqs(reqs)]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate requirement ids: {dupes}"


def test_every_requirement_has_search_terms_and_a_verify_step(reqs):
    """
    Aliases are what the agent greps a data catalog with, and verify is what
    stops a lookalike being bound. A requirement missing either is not
    actionable inside a company.
    """
    thin = []
    for _, r in iter_reqs(reqs):
        if not r.get("aliases"):
            thin.append(f"{r['id']}: no aliases — nothing to search the catalog for")
        if not (r.get("verify") or "").strip():
            thin.append(f"{r['id']}: no verify step")
        if r["criticality"] not in CRIT_ORDER:
            thin.append(f"{r['id']}: criticality {r['criticality']!r} is not a known level")
    assert not thin, "\n".join(thin)


def test_unverified_find_does_not_count(reqs, spec):
    """
    The device-fingerprint trap. status=found with verified=false must leave the
    entity blocked — otherwise the report certifies a session cookie as a device
    fingerprint and the whole device-keyed family is built on sand.
    """
    binding = {"bindings": {"device_fp": {"status": "found", "source": "x.y.z",
                                          "verified": False}}}
    cov = coverage(reqs, binding, spec)
    assert "device" not in cov["entities"][0]
    assert "device_fp" in cov["unverified"]

    binding["bindings"]["device_fp"]["verified"] = True
    cov = coverage(reqs, binding, spec)
    assert "device" in cov["entities"][0]
    assert not cov["unverified"]


def test_absent_and_unknown_are_not_merged(reqs, spec):
    """Opposite consequences: absent is a design constraint, unknown is work
    outstanding. The report must distinguish them."""
    a = coverage(reqs, {"bindings": {"bin": {"status": "absent"}}}, spec)
    u = coverage(reqs, {"bindings": {"bin": {"status": "unknown"}}}, spec)
    assert a["status"]["bin"]["status"] == "absent"
    assert u["status"]["bin"]["status"] == "unknown"
    assert not a["status"]["bin"]["satisfied"]
    assert not u["status"]["bin"]["satisfied"]


def test_missing_core_requirement_blocks(reqs, spec):
    cov = coverage(reqs, {"bindings": {}}, spec)
    core = {r["id"] for _, r in iter_reqs(reqs) if r["criticality"] == "core"}
    assert set(cov["blocked_core"]) == core


def test_composite_needs_every_part(reqs, spec):
    """A composite entity is buildable only when all of its parts are."""
    comp = spec["composites"][0]
    parts = comp["parts"]
    reqs_for = {}
    for _, r in iter_reqs(reqs):
        for p in r.get("unlocks_entities") or []:
            if p in parts:
                reqs_for.setdefault(p, r["id"])
    assert len(reqs_for) == len(parts), "test needs a composite whose parts are all unlockable"

    one = {"bindings": {next(iter(reqs_for.values())): {"status": "found", "verified": True}}}
    assert comp["id"] not in coverage(reqs, one, spec)["composites"][0]

    every = {"bindings": {rid: {"status": "found", "verified": True}
                          for rid in reqs_for.values()}}
    assert comp["id"] in coverage(reqs, every, spec)["composites"][0]


def test_binding_naming_an_unknown_requirement_is_reported(reqs, spec):
    cov = coverage(reqs, {"bindings": {"not_a_requirement": {"status": "found"}}}, spec)
    assert cov["unknown_ids"] == ["not_a_requirement"]


def test_shipped_example_binding_parses_and_blocks(reqs, spec):
    """The worked example is deliberately mid-discovery: it must still parse,
    and it must still report as blocking."""
    with open(EXAMPLE) as f:
        binding = yaml.safe_load(f)
    cov = coverage(reqs, binding, spec)
    assert not cov["unknown_ids"], f"example names unknown requirements: {cov['unknown_ids']}"
    assert cov["blocked_core"], "the example is meant to demonstrate a blocked state"
    assert "device_fp" in cov["unverified"]


def test_template_covers_every_requirement(reqs):
    text = template_yaml(reqs)
    parsed = yaml.safe_load(text)
    assert set(parsed["bindings"]) == {r["id"] for _, r in iter_reqs(reqs)}
    assert all(v["status"] == "unknown" for v in parsed["bindings"].values())


def test_checklist_renders_every_requirement(reqs):
    md = checklist_md(reqs)
    for _, r in iter_reqs(reqs):
        assert f"`{r['id']}`" in md, f"{r['id']} missing from the checklist"
        for alias in r["aliases"]:
            assert alias in md, f"{r['id']}: alias {alias!r} not rendered"


def test_checklist_on_disk_is_current(reqs):
    """DISCOVERY_CHECKLIST.md is generated; a stale one sends the agent looking
    for the wrong fields."""
    path = os.path.join(ROOT, "DISCOVERY_CHECKLIST.md")
    if not os.path.exists(path):
        pytest.skip("checklist not generated yet")
    with open(path) as f:
        assert f.read() == checklist_md(reqs), "run `make checklist` — it is out of date"


def test_cli_exits_nonzero_when_core_is_missing():
    r = subprocess.run([sys.executable, "discover.py", "--binding", EXAMPLE],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 1
    assert "BLOCKING" in r.stdout
