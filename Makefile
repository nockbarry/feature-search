# feature-search — regenerate everything from feature_space.yaml
#
# The YAML is the source of truth. candidates.csv and feature_catalog.xlsx are
# BUILD ARTIFACTS: never hand-edit them, never commit them.

LEVEL    ?= L0
SHARD_BY ?= entity_id
PY       ?= python

.DEFAULT_GOAL := help
.PHONY: help install validate checklist catalog expand workbook pack shards accept test lint check clean all

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  LEVEL=L0|L1|L2 selects the resolution rung (default: $(LEVEL))"

install:  ## Install runtime + dev dependencies
	$(PY) -m pip install -e ".[dev]"

validate:  ## Schema + invariant checks on feature_space.yaml
	$(PY) validate.py

checklist:  ## Regenerate DISCOVERY_CHECKLIST.md from spec/data_requirements.yaml
	$(PY) discover.py --checklist

catalog: validate  ## Expand the YAML into candidates.csv at $(LEVEL)
	$(PY) expand_catalog.py --level $(LEVEL)

expand:  ## Stage B/C expansion on stage-4 survivors (needs survivors.txt)
	@test -f survivors.txt || { echo "survivors.txt not found — screen the base grid first"; exit 1; }
	$(PY) expand_catalog.py --level $(LEVEL) --expand --survivors survivors.txt

workbook: catalog  ## Rebuild feature_catalog.xlsx (human fill-in surface)
	$(PY) build_workbook.py

pack: catalog  ## Assemble pack/ — the model-facing context pack
	$(PY) pack.py --level $(LEVEL)

shards: catalog  ## Split the queue into self-contained per-worker packets
	$(PY) shard.py --by $(SHARD_BY)

accept:  ## Validate a filled queue: make accept QUEUE=path/to/queue.csv
	@test -n "$(QUEUE)" || { echo "usage: make accept QUEUE=path/to/filled_queue.csv"; exit 1; }
	$(PY) validate_queue.py "$(QUEUE)" --catalog candidates.csv --strict

test:  ## Run the test suite
	$(PY) -m pytest

lint:  ## Static checks
	$(PY) -m ruff check .

check: validate checklist test lint  ## Everything CI runs
	@echo "ok — determinism is asserted by tests/test_determinism.py"

clean:  ## Remove build artifacts
	rm -rf candidates.csv feature_catalog.xlsx __pycache__ .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

all: check workbook  ## Full build
