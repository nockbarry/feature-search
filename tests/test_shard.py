"""
test_shard.py — a work packet must stand alone.

The property under test is not "the file was written." It is: **a worker that
reads only its TASK.md and its own queue.csv can do the job correctly.** That is
the whole reason shards exist. A packet that says "see AGENT_BRIEF section 7" has
failed, because the reader this is built for does not go and look.

The sharpest test here is test_worked_examples_pass_the_output_validator. The
examples exist so fast models can pattern-match instead of reasoning from prose —
which means a wrong example is worse than no example: it would teach exactly the
fabrication the validator was written to reject. So the examples are run through
validate_queue.py, and a demonstration of a *rejected* shape is checked to
actually be rejected.
"""
import csv
import io
import os
import sys

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from shard import NON_NEGOTIABLES, PROCEDURE, WORKED, build  # noqa: E402
from validate_queue import check, load_statuses  # noqa: E402


@pytest.fixture(scope="module")
def spec():
    with open(os.path.join(ROOT, "feature_space.yaml")) as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def sharded(tmp_path_factory):
    out = tmp_path_factory.mktemp("shards")
    assert build(out=str(out)) == 0
    dirs = sorted(p for p in out.iterdir() if p.is_dir())
    assert dirs, "no shards written"
    return out, dirs


def test_every_shard_has_a_card_and_a_queue(sharded):
    _, dirs = sharded
    for d in dirs:
        assert (d / "TASK.md").exists(), f"{d.name}: no TASK.md"
        assert (d / "queue.csv").exists(), f"{d.name}: no queue.csv"


def test_shards_partition_the_catalog_exactly(sharded):
    """No row lost, no row duplicated across shards. If a shard boundary drops
    features, the coverage claim in the brief becomes false and nobody notices."""
    out, dirs = sharded
    seen = []
    for d in dirs:
        with open(d / "queue.csv", newline="") as f:
            seen += [r["feature_name"] for r in csv.DictReader(f)]
    with open(os.path.join(ROOT, "candidates.csv"), newline="") as f:
        expected = [r["feature_name"] for r in csv.DictReader(f)]
    assert len(seen) == len(set(seen)), "a feature appears in more than one shard"
    assert set(seen) == set(expected)
    assert len(seen) == len(expected)


def test_every_card_is_self_contained(sharded):
    """
    The load-bearing test. Each packet must restate the rules rather than
    reference them, because the reader will not follow a reference.
    """
    _, dirs = sharded
    required = [
        ("label_arrival_ts", "the two-clock rule names the field to filter on"),
        ("Never write a number you did not compute", "the anti-fabrication contract"),
        ("blocked", "the escape hatch for uncomputable rows"),
        ("validate_queue.py", "how the work will be checked"),
        ("Stage 1", "the per-row procedure"),
        ("Worked examples", "shapes to pattern-match against"),
    ]
    for d in dirs:
        text = (d / "TASK.md").read_text()
        for needle, why in required:
            assert needle in text, f"{d.name}: missing {needle!r} — {why}"


def test_cards_do_not_defer_to_documents_not_in_the_packet(sharded):
    """"See AGENT_BRIEF section 7" is exactly the failure mode shards exist to
    prevent: the worker does not have that file open."""
    _, dirs = sharded
    for d in dirs:
        text = (d / "TASK.md").read_text()
        for bad in ("see AGENT_BRIEF", "See AGENT_BRIEF", "section 7", "docs/"):
            assert bad not in text, f"{d.name}: defers to {bad!r}, which is not in the packet"


def test_card_states_that_cross_shard_stages_are_out_of_scope(sharded):
    """
    A worker told to "screen these features" will invent a SHAP rank for 44 rows
    in isolation unless told that ranking is meaningless within a shard.
    """
    _, dirs = sharded
    for d in dirs:
        text = (d / "TASK.md").read_text()
        assert "shap_rank` empty" in text or "shap_rank empty" in text, \
            f"{d.name}: does not tell the worker to leave shap_rank empty"
        assert "wrong by construction" in text, \
            f"{d.name}: does not say a shard-written shipped/shap_rank is invalid"


def test_worked_examples_pass_the_output_validator(spec):
    """
    A wrong example teaches the exact fabrication the validator rejects, so the
    examples are themselves validated. This is the test most likely to catch a
    careless edit to the task card.
    """
    block = WORKED.split("```csv")[1].split("```")[0].strip()
    rows = list(csv.DictReader(io.StringIO(block)))
    assert len(rows) >= 5, "the examples should cover every terminal shape"

    statuses = load_statuses(spec)
    errors, _ = check(rows, statuses)
    assert errors == [], "worked examples do not survive validate_queue.py:\n" + "\n".join(errors)

    shown = {r["status"] for r in rows}
    assert {"screened", "dropped", "blocked"} <= shown, f"examples only demonstrate {shown}"


def test_examples_demonstrate_the_stage_evidence_rule(spec):
    """Each example must actually exhibit the rule its commentary claims: a
    stage-3 drop carries no iv, a blocked row carries no numbers at all."""
    block = WORKED.split("```csv")[1].split("```")[0].strip()
    rows = {r["feature_name"]: r for r in csv.DictReader(io.StringIO(block))}

    stage3 = [r for r in rows.values() if "stage 3" in r["drop_reason"]]
    assert stage3 and all(r["iv"] == "" for r in stage3), \
        "the stage-3 example must have an empty iv, or it teaches the opposite lesson"

    blocked = [r for r in rows.values() if r["status"] == "blocked"]
    assert blocked and all(not r[c] for r in blocked
                           for c in ("iv", "psi_12m", "coverage_pct", "shap_rank")), \
        "the blocked example must carry no numbers"


def test_a_fabricated_row_would_be_rejected(spec):
    """Non-vacuity for the example test: prove the validator that passes the
    examples still rejects the shape the examples are teaching against."""
    statuses = load_statuses(spec)
    bad = [{"feature_name": "x__cb_rate__rate__w30d", "status": "dropped",
            "drop_reason": "stage 4 low IV", "iv": "", "psi_12m": "0.02",
            "coverage_pct": "90", "shap_rank": ""}]
    assert check(bad, statuses)[0], "validator does not reject a drop with no measurement"


def test_shard_queue_carries_the_screening_columns(sharded):
    _, dirs = sharded
    with open(dirs[0] / "queue.csv", newline="") as f:
        cols = csv.DictReader(f).fieldnames
    for c in ("feature_name", "availability", "min_support_n",
              "status", "drop_reason", "iv", "psi_12m", "coverage_pct", "shap_rank"):
        assert c in cols, f"shard queue is missing {c}"


def test_packet_is_small_enough_to_actually_read(sharded):
    """The point of sharding is that a worker loads its packet, not the pack."""
    _, dirs = sharded
    for d in dirs:
        total = (d / "TASK.md").stat().st_size + (d / "queue.csv").stat().st_size
        assert total < 40_000, f"{d.name}: packet is {total} bytes — too big to be read closely"


def test_index_lists_every_shard(sharded):
    out, dirs = sharded
    index = (out / "INDEX.md").read_text()
    for d in dirs:
        assert f"`{d.name}`" in index, f"{d.name} missing from INDEX.md"
    assert "must be run by the coordinator" in index, \
        "INDEX.md does not tell the coordinator which stages it still owns"


def test_non_negotiables_and_procedure_are_not_empty():
    for name, text in (("NON_NEGOTIABLES", NON_NEGOTIABLES), ("PROCEDURE", PROCEDURE)):
        assert len(text.split()) > 80, f"{name} is too thin to be worth restating"
