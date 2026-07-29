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
# Resolve siblings BEFORE changing directory: the repo being attested is not necessarily
# the repo this script lives in, and `scripts/sweep-text.sh` resolved against the wrong
# one — reporting a MISSING sweeper as "carries denied content", which is a fail-open
# message dressed as a fail-closed one.
script_dir="$(cd "$(dirname "$0")" && pwd)"
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
[ "$(head -1 "$automated")" = "$head_sha" ] || {
  echo "✋ attest: the automated receipt is for a different commit. Re-run scripts/gate.sh." >&2
  exit 1
}

# Bind the reviewed SET, not just its tip. Re-running the gate at the same HEAD with a
# wider base rewrites the set; without this the older approval would still authorise it.
commits_file="$(git rev-parse --git-path casa-gate-commits)"
[ -f "$commits_file" ] || { echo "✋ attest: no reviewed commit set. Re-run scripts/gate.sh." >&2; exit 1; }
digest="$(sha256sum < "$commits_file" | cut -d' ' -f1)"
[ "$(sed -n 2p "$automated")" = "$digest" ] || {
  echo "✋ attest: the reviewed commit set has changed since the gate ran." >&2
  echo "        Re-run scripts/gate.sh, then read and review the new range." >&2
  exit 1
}

# The destination BRANCH NAME is published metadata too, and no sweep covers it.
branch="$(git rev-parse --abbrev-ref HEAD)"
sweeper="$script_dir/sweep-text.sh"
[ -x "$sweeper" ] || { echo "✋ attest: $sweeper is missing — cannot sweep the branch name." >&2; exit 1; }
printf '%s\n' "$branch" > "$(git rev-parse --git-path casa-branch-name)"
set +e
"$sweeper" "$(git rev-parse --git-path casa-branch-name)" >/dev/null 2>&1
sweep_status=$?
set -e
if [ "$sweep_status" -eq 1 ]; then
  echo "✋ attest: the branch name '$branch' carries denied content. It is published." >&2
  exit 1
elif [ "$sweep_status" -ne 0 ]; then
  echo "✋ attest: the branch-name sweep failed (status $sweep_status)." >&2
  exit 1
fi

printf '%s\n%s\n%s\nread-in-full; reviewers=%s; findings-applied; re-reviewed\n' \
  "$head_sha" "$digest" "$branch" "$reviewers" > "$(git rev-parse --git-path casa-gate-approved)"
echo "✓ attested $head_sha"
