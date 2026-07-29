#!/usr/bin/env bash
# Record that the manual gates happened, bound to the exact commit they happened on.
#
#   scripts/attest.sh --read-in-full --reviewers "sol,terra" --findings-applied --re-reviewed
#
# Commits only. A tag attestation flow existed briefly and was removed: tags are refused
# outright by .githooks/pre-push, because this repo does not tag locally and the flow kept
# producing publication holes.
#
# Structured on purpose: an earlier draft accepted any string, so `attest.sh x` minted the
# receipt pre-push honours. Each flag is a distinct claim, and all four are required.
#
# Refuses unless the automated gate passed for THIS commit. Applying a review finding
# creates a new commit, which invalidates both receipts — that is the point.
set -euo pipefail
# Operate on the repository containing the CURRENT DIRECTORY, not the one containing this
# script: `cd $(dirname $0)/..` made the tests read and write the real checkout's receipts.
cd "$(git rev-parse --show-toplevel)"

read_in_full=0; findings_applied=0; re_reviewed=0; reviewers=""
while [ $# -gt 0 ]; do
  case "$1" in
    --read-in-full)     read_in_full=1 ;;
    --findings-applied) findings_applied=1 ;;
    --re-reviewed)      re_reviewed=1 ;;
    --reviewers)        shift; reviewers="${1:-}" ;;
    *) echo "✋ attest: unknown argument $1" >&2; exit 1 ;;
  esac
  shift
done

missing=""
[ "$read_in_full" = 1 ]     || missing="$missing --read-in-full"
[ "$findings_applied" = 1 ] || missing="$missing --findings-applied"
[ "$re_reviewed" = 1 ]      || missing="$missing --re-reviewed"
[ -n "$reviewers" ]         || missing="$missing --reviewers"
if [ -n "$missing" ]; then
  cat >&2 <<EOF
✋ attest: missing$missing

  This receipt is what pre-push honours. It asserts things no script checked:
    --read-in-full        every introduced file and commit message was read from a clone
    --reviewers "a,b"     who reviewed it independently
    --findings-applied    their findings were applied
    --re-reviewed         the post-fix state was reviewed again
  If any of those is not true, do not attest.
EOF
  exit 1
fi

[ -z "$(git status --porcelain)" ] || { echo "✋ attest: working tree is dirty." >&2; exit 1; }
head_sha="$(git rev-parse HEAD)"
automated="$(git rev-parse --git-path casa-gate-automated)"
[ -f "$automated" ] || { echo "✋ attest: run scripts/gate.sh first." >&2; exit 1; }
[ "$(cat "$automated")" = "$head_sha" ] || {
  echo "✋ attest: the automated receipt is for a different commit. Re-run scripts/gate.sh." >&2
  exit 1
}

printf '%s\nread-in-full; reviewers=%s; findings-applied; re-reviewed\n' \
  "$head_sha" "$reviewers" > "$(git rev-parse --git-path casa-gate-approved)"
echo "✓ attested $head_sha"
