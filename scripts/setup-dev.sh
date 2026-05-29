#!/usr/bin/env bash
# One-time developer setup for Casa, for WSL2 / Linux. Idempotent — safe to re-run.
#
#   ./scripts/setup-dev.sh   (or: make setup)
#
# Does three things:
#   1. Installs the shared git hooks (the docs/ leak guard).
#   2. Builds a Linux test venv at venv_test/ (replacing any stale Windows-layout venv).
#   3. Installs runtime + test dependencies into it.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/3 git hooks (docs/ leak guard)"
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "==> 2/3 Linux test venv at venv_test/"
# A venv created on Windows has Scripts/ + Activate.ps1 and no bin/python — unusable on WSL.
if [ ! -x venv_test/bin/python ]; then
  rm -rf venv_test
  python3 -m venv venv_test
fi

echo "==> 3/3 dependencies"
venv_test/bin/python -m pip install --quiet --upgrade pip
venv_test/bin/python -m pip install --quiet -r casa-agent/requirements.txt pytest pytest-asyncio anyio

cat <<'DONE'

Setup complete.
  make test-unit     # fast unit tests
  make test-docker   # docker-backed unit tests

Internal engineering docs live in the PRIVATE docs/ tree (its own repo, not part of this
public repo). If you have access, hydrate it with:
  git clone <private-docs-remote> docs/     # URL is in your team notes, not in this repo
DONE
