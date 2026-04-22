# Run from the repo root: `make -C test-local test`
SHELL := /usr/bin/env bash
IMAGE ?= casa-test
ROOT  := $(shell git rev-parse --show-toplevel)

.PHONY: build test test-smoke test-migration test-runtime test-voice clean help

help:
	@echo "Targets:"
	@echo "  build           Build the test image"
	@echo "  test            Smoke + migration + runtime + voice (default)"
	@echo "  test-smoke      Image builds + /healthz + dashboard"
	@echo "  test-migration  Legacy YAML migration scenarios"
	@echo "  test-runtime    /invoke session isolation + concurrency"
	@echo "  test-voice      Voice pipeline SSE + WS smoke scenarios"
	@echo "  clean           Remove dangling test containers"

build:
	cd $(ROOT) && docker build -f test-local/Dockerfile.test -t $(IMAGE) .

test-smoke: build
	cd $(ROOT) && bash test-local/e2e/test_smoke.sh

test-migration: build
	cd $(ROOT) && bash test-local/e2e/test_migration.sh

test-runtime: build
	cd $(ROOT) && bash test-local/e2e/test_invoke_sessions.sh
	cd $(ROOT) && bash test-local/e2e/test_concurrency.sh

test-voice: build
	cd $(ROOT) && bash test-local/e2e/test_voice_sse.sh
	cd $(ROOT) && bash test-local/e2e/test_voice_ws.sh

test: test-smoke test-migration test-runtime test-voice

clean:
	-docker ps -a --filter 'name=casa-' --format '{{.ID}}' | xargs -r docker rm -f
