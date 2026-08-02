PY := venv_test/bin/python

.PHONY: help setup test-unit test-unit-serial test-docker test-image lint
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-13s %s\n", $$1, $$2}'

setup: ## One-time WSL dev setup (Linux venv + git hooks)
	./scripts/setup-dev.sh

# -n auto --maxprocesses=12 --dist loadfile: 12 workers take this from ~185s to ~25s, and
# file-scoped distribution keeps every test in a module on one worker, so the
# module-level state a few suites monkeypatch cannot straddle processes.
# Measured identical results over repeated runs; see test-unit-serial when a
# failure needs readable, interleaving-free output.
# CAGE := the documented systemd-run memory cage, applied automatically when
# available. RLIMIT_AS in conftest bounds one process's ADDRESS SPACE; only this
# bounds REAL memory across all workers, which is what actually killed the VM
# twice. Degrades to running uncaged where systemd-run is absent (CI images).
# Probe that the cage actually WORKS, not merely that the binary exists: in WSL,
# containers and non-login SSH sessions /usr/bin/systemd-run is present while the
# user bus is not, and prepending it there would make pytest never run at all —
# taking the binding pre-push gate down with it. Both reviewers made this their
# top finding, and it passes on this machine, which is exactly why it needed a
# real probe rather than my judgement.
CAGE := $(shell systemd-run --user --scope -q true >/dev/null 2>&1 && echo "systemd-run --user --scope -q -p MemoryMax=8G -p MemorySwapMax=2G")

test-unit: ## Fast unit tests, parallel + memory-caged (except docker/slow)
	$(CAGE) $(PY) -m pytest tests/ -m "not docker and not slow" -n auto --maxprocesses=12 --dist loadfile --tb=short

test-unit-serial: ## Same suite, one process (for debugging a failure)
	$(PY) -m pytest tests/ -m "not docker and not slow" --tb=short

test-docker: ## Docker-backed unit tests
	$(PY) -m pytest tests/ -m "docker and not slow" --tb=short

test-image: ## Build the e2e test image (mirrors CI tier1/baseline)
	docker build -f test-local/Dockerfile.test -t casa-test .

lint: ## (no linter configured yet)
	@echo "No linter configured. CI gate is pytest tier2 (see make test-unit)."
