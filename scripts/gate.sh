#!/usr/bin/env bash
# The automated half of the pre-push gate. Writes a receipt naming the sha it approved.
#
# It is NOT sufficient to push: scripts/attest.sh records the manual half — the read of
# every introduced file and the independent review — and .githooks/pre-push requires THAT
# receipt, not this one.
#
# It evaluates HEAD, not the working tree: `git push` publishes commits, so a gate that
# reads uncommitted files can approve something other than what goes out.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

base="${CASA_GATE_BASE:-origin/main}"

echo "==> 0/7 committed state"
if [ -n "$(git status --porcelain)" ]; then
  echo "✋ gate: working tree is dirty. Commit or stash first — the gate approves a" >&2
  echo "        commit, and a push publishes commits, not files." >&2
  exit 1
fi
# Resolve to immutable shas up front: a concurrent fetch moving `origin/main` between the
# scans and the final rev-list would otherwise record commits that were never swept.
head_sha="$(git rev-parse HEAD)"
base_sha="$(git rev-parse "$base")"
range="$base_sha..$head_sha"
echo "    HEAD  = $head_sha"
echo "    range = $range ($(git rev-list --count "$range") unpublished commit(s))"

supplement="${CASA_DENY_SUPPLEMENT:-}"
if [ -z "${CASA_ALLOW_NO_SUPPLEMENT:-}" ]; then
  if [ -z "$supplement" ] || [ ! -s "$supplement" ]; then
    echo "✋ gate: CASA_DENY_SUPPLEMENT is unset, missing or empty." >&2
    echo "        The private exact-literal layer is the only thing that catches an" >&2
    echo "        identifying name that is neither secret-shaped nor generic. Running" >&2
    echo "        without it is a gate that fails open." >&2
    echo "        Set it, or export CASA_ALLOW_NO_SUPPLEMENT=1 (CI only, which has none)." >&2
    exit 1
  fi
fi

echo "==> 1/7 corpus verifier"
if [ -f docs/manifest.yaml ]; then
  venv_test/bin/python -m scripts.verify_docs . --report
  venv_test/bin/python -m scripts.verify_docs . --check-nav
else
  echo "    (no corpus yet — every other check applies in full)"
fi

echo "==> 2/7 docs-impact — claimed surfaces vs the corpus"
# THE binding copy of the drift gate. CI runs the same script, but a CI check
# reports after a pull request exists, which leaves a red mark somebody can
# merge past — PR #383 is exactly that story. Here it refuses before the push,
# and no attestation can be produced without it.
#
# UNCONDITIONAL, deliberately (Terra+Sol): guarding on a manifest at HEAD let a
# commit DELETE the manifest, change any claimed surface, and skip the gate
# entirely. The script carries the BASE manifest precisely so deleting a claim
# cannot delete the obligation, and it handles a base that genuinely predates
# the corpus on its own.
scripts/docs_impact.sh "$base_sha" "$head_sha"

echo "==> 3/7 deny sweep — endpoint tree"
scripts/deny-sweep.sh tree

echo "==> 4/7 deny sweep — every unpublished commit, and their messages"
# The endpoint tree can be spotless while an intermediate commit carries the leak, and a
# push publishes the objects either way. Commit messages are published with them.
scripts/deny-sweep.sh range "$range"
scripts/deny-sweep.sh messages "$range"

echo "==> 5/7 secret scan — tree and history"
scripts/run-gitleaks.sh tree
scripts/run-gitleaks.sh range "$range"

echo "==> 6/7 unit gate"
make test-unit

echo "==> 7/7 automated receipt"
# Record the exact commit SET that was swept, not just its tip, and DIGEST it into the
# receipt. Binding only the tip let a tip gated against origin/main be pushed to a
# destination with less history, carrying commits the review never covered. Digesting it
# additionally stops a second gate run at the same HEAD with a wider base from silently
# swapping the set while an older approval still stands.
commits_file="$(git rev-parse --git-path casa-gate-commits)"
git rev-list "$range" > "$commits_file"
digest="$(sha256sum < "$commits_file" | cut -d" " -f1)"
printf '%s\n%s\n' "$head_sha" "$digest" > "$(git rev-parse --git-path casa-gate-automated)"
cat <<EOF

Automated gate PASSED for $head_sha.

This is NOT permission to push. Now do the part no script can do:
  * read EVERY introduced file and commit message from a local clone
      git log -p $range
  * independent review, findings applied, then re-review
Then record it:
  scripts/attest.sh --read-in-full --reviewers "..." --findings-applied --re-reviewed

Applying any finding creates a new commit, which voids both receipts. That is the point.
EOF
