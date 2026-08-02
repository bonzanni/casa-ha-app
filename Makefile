PY := venv_test/bin/python

.PHONY: help setup test-unit test-unit-serial test-docker test-image lint
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-13s %s\n", $$1, $$2}'

setup: ## One-time WSL dev setup (Linux venv + git hooks)
	./scripts/setup-dev.sh

# -n auto --dist loadfile: 12 workers take this from ~185s to ~25s, and
# file-scoped distribution keeps every test in a module on one worker, so the
# module-level state a few suites monkeypatch cannot straddle processes.
# Measured identical results over repeated runs; see test-unit-serial when a
# failure needs readable, interleaving-free output.
test-unit: ## Fast unit tests, parallel (everything except docker/slow)
	$(PY) -m pytest tests/ -m "not docker and not slow" -n auto --dist loadfile --tb=short

test-unit-serial: ## Same suite, one process (for debugging a failure)
	$(PY) -m pytest tests/ -m "not docker and not slow" --tb=short

test-docker: ## Docker-backed unit tests
	$(PY) -m pytest tests/ -m "docker and not slow" --tb=short

test-image: ## Build the e2e test image (mirrors CI tier1/baseline)
	docker build -f test-local/Dockerfile.test -t casa-test .

lint: ## (no linter configured yet)
	@echo "No linter configured. CI gate is pytest tier2 (see make test-unit)."
