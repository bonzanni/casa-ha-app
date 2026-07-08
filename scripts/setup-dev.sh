#!/usr/bin/env bash
# One-time developer setup for Casa, for WSL2 / Linux. Idempotent — safe to re-run.
#
#   ./scripts/setup-dev.sh   (or: make setup)
#
# 1. Installs the shared git hooks (the docs/ leak guard).
# 2. Builds a Linux test venv at venv_test/ (replacing any stale Windows-layout venv);
#    falls back to uv's managed CPython 3.12 when system python3 can't make a venv.
# 3. Installs runtime + test dependencies into it.
# 4. Deploys private AI assets (subagents/skills) into .claude/ if the private docs tree
#    is present (no-op on a public clone). In a linked git worktree, docs/ is first
#    auto-symlinked from the main checkout.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 1/4 git hooks (docs/ leak guard)"
git config core.hooksPath .githooks
chmod +x .githooks/* 2>/dev/null || true

echo "==> 2/4 Linux test venv at venv_test/"
# A venv created on Windows has Scripts/ + Activate.ps1 and no bin/python — unusable on WSL.
if [ ! -x venv_test/bin/python ]; then
  rm -rf venv_test
  python3 -m venv venv_test >/dev/null 2>&1 || true
fi
# No usable pip (system python lacks ensurepip/python3-venv, or venv creation failed
# partway): rebuild with uv's managed CPython 3.12, which seeds pip.
if ! venv_test/bin/python -m pip --version >/dev/null 2>&1 \
   && command -v uv >/dev/null 2>&1; then
  echo "    python3 venv unusable — rebuilding with uv (CPython 3.12 + pip)"
  rm -rf venv_test
  uv venv --seed --python 3.12 venv_test
fi
if ! venv_test/bin/python -m pip --version >/dev/null 2>&1; then
  echo "ERROR: could not build a venv with pip." >&2
  echo "       Install uv (https://docs.astral.sh/uv/), or:  sudo apt install python3-venv python3-pip" >&2
  exit 1
fi

echo "==> 3/4 dependencies"
venv_test/bin/python -m pip install --quiet --upgrade pip
venv_test/bin/python -m pip install --quiet -r casa-agent/requirements.txt pytest pytest-asyncio anyio

echo "==> 4/4 AI assets (subagents/skills) → .claude/"
# In a linked git worktree, docs/ (private inner repo, gitignored) exists only in the
# main checkout — symlink it so this step and docs-dependent tooling work here too.
if [ ! -e docs ] && [ ! -L docs ]; then
  main_root="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")"
  if [ "$main_root" != "$(pwd)" ] && [ -d "$main_root/docs" ]; then
    ln -s "$main_root/docs" docs
    echo "    linked docs/ -> $main_root/docs (git worktree)"
  fi
fi
if [ -d docs/ai-assets ]; then
  mkdir -p .claude/agents .claude/skills
  cp -f  docs/ai-assets/agents/*.md   .claude/agents/  2>/dev/null || true
  cp -rf docs/ai-assets/skills/*      .claude/skills/  2>/dev/null || true
  echo "    deployed from docs/ai-assets/ (source of truth; re-run to re-sync)"
else
  echo "    (no docs/ai-assets — private docs tree not present; skipping)"
fi

cat <<'DONE'

Setup complete.
  make test-unit     # fast unit tests
  make test-docker   # docker-backed unit tests

Internal engineering docs live in the PRIVATE docs/ tree (its own repo, not part of this
public repo). If you have access, hydrate it with:
  git clone <private-docs-remote> docs/     # URL is in your team notes, not in this repo
DONE
