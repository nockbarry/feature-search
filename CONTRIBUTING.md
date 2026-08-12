# Contributing

## The one rule

**`feature_space.yaml` is the source of truth. `candidates.csv` and `feature_catalog.xlsx` are build artifacts.**

Hand-editing a generated file means your change is erased by the next `make workbook`, and the artifact no longer matches the spec that supposedly produced it. CI fails if either artifact is committed.

To change the search space, edit the YAML and regenerate:

```bash
make workbook && make check
```

## Adding to the registries

**A new entity** needs `id`, `name`, `class`, `adversarial_cost` (free / cheap / expensive — how cheaply an attacker rotates it), `availability`, and `priority`.

**A new composite** needs a stated `hypothesis` — what ring or pattern it catches. Enforced by `test_every_composite_states_a_hypothesis`. If you cannot state one, support will kill the feature anyway.

**A new measure** needs `label_dependent` set correctly. If it derives from chargebacks, disputes, refunds or review outcomes, it is `true` and every aggregate over it becomes point-in-time critical.

**A new non-grid feature** goes in `nongrid_features.py` as an 8-tuple: family, name, definition, inputs required, typologies covered, availability, leakage risk, adversarial half-life. Check the workbook's Typology Coverage sheet afterwards — the point is closing gaps, not deepening a column that is already dense.

## Changing the expander

Compatibility rules are the search's semantics. Any change to `compatible()` needs a test in `tests/test_compatibility.py` stating the rule in prose and asserting both directions — what it rejects *and* what it still permits. One-sided tests pass trivially when a rule is over-broad.

If you touch aggregation logic in `pit_reference.py`, update `pit_aggregate_template.sql` to match, and prove the agreement with a fixture. The two implementations diverging silently is the failure this repo exists to prevent.

## Adding a resolution rung

Rungs must nest: `L0 ⊆ L1 ⊆ L2` per measure family. No rung invents a scale that a finer rung lacks. Enforced by `test_ladder_narrows_resolution_monotonically`.

Resolution gating may prune windows, transforms, filters and sibling granularities. It may **never** remove a measure or an entity class — that is coverage, and it is enforced by `test_coverage_is_never_gated_by_resolution`.

## Before opening a PR

```bash
make check
```

Tests and lint must pass. If you added a feature family, say in the PR which typology gap it closes and what it costs at L0.
