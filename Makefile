PY := venv_test/bin/python

.PHONY: help setup test-unit test-docker test-image lint
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-13s %s\n", $$1, $$2}'

setup: ## One-time WSL dev setup (Linux venv + git hooks)
	./scripts/setup-dev.sh

test-unit: ## Fast unit tests (everything except docker/slow — opt-out gate)
	$(PY) -m pytest tests/ -m "not docker and not slow" --tb=short

test-docker: ## Docker-backed unit tests
	$(PY) -m pytest tests/ -m "docker and not slow" --tb=short

test-image: ## Build the e2e test image (mirrors CI tier1/baseline)
	docker build -f test-local/Dockerfile.test -t casa-test .

lint: ## (no linter configured yet)
	@echo "No linter configured. CI gate is pytest tier2 (see make test-unit)."
