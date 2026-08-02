#!/usr/bin/env bash
# The documentation-drift gate: a change to a claimed code surface must come with
# a change to the document that claims it, or a reasoned per-document waiver.
#
# Usage: scripts/docs_impact.sh <base-ref> <ack-commit>
#
#   base-ref     what this change is measured against (origin/main, or the PR base)
#   ack-commit   the commit whose message carries the waivers — the TIP of the work
#
# Exit 0 = every impacted document was updated or waived. Exit 1 = it was not, or
# the input was unusable. It fails CLOSED: an unreadable manifest, an empty
# ack-commit and an unresolvable base are all refusals, never silent passes.
#
# WHY THIS IS A SCRIPT AND NOT A CI STEP. It runs in two places on purpose:
#
#   * `scripts/gate.sh` (the pre-push gate) — the control that actually binds.
#     A CI check reports AFTER a pull request exists, which leaves a red mark
#     somebody can merge past; that is exactly what happened on PR #383, where
#     this check failed naming six documents and the batch was squash-merged
#     with `--admin` a minute later, before it reported. Run at pre-push, the
#     same logic refuses before anything is published, and the attestation
#     `.githooks/pre-push` demands cannot be produced without it.
#   * `.github/workflows/docs.yml` — the backstop that catches a push made with
#     hooks uninstalled or `--no-verify`, and anything arriving from elsewhere.
#
# One implementation, two callers: a second copy would drift, and the copy that
# drifted would be the one nobody was watching.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

base="${1:?usage: docs_impact.sh <base-ref> <ack-commit>}"
ack_commit="${2:?usage: docs_impact.sh <base-ref> <ack-commit>}"

# GitHub Actions renders ::error:: as an annotation; a terminal wants plain text.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  err() { echo "::error::$*"; }
else
  err() { echo "✋ docs-impact: $*" >&2; }
fi

git rev-parse --verify --quiet "$base" >/dev/null || {
  err "base ref '$base' does not resolve — cannot tell what changed."
  exit 1
}
git rev-parse --verify --quiet "$ack_commit" >/dev/null || {
  err "ack commit '$ack_commit' does not resolve — cannot read waivers."
  exit 1
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

python_bin="python3"
[ -x venv_test/bin/python ] && python_bin="venv_test/bin/python"

changed="$(git diff --name-only "$base"...HEAD)"

# A version-only bump to the app manifest is not a schema change. BOTH added and
# removed lines are inspected: deleting an option produces no added schema line,
# and treating that as version-only would skip the check on an option removal —
# the change most likely to invalidate a document.
if printf '%s\n' "$changed" | grep -qx 'casa/config\.yaml'; then
  substantive="$(git diff -U0 "$base"...HEAD -- casa/config.yaml \
    | grep -E '^[+-]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[+-]version:' || true)"
  if [ -z "$substantive" ]; then
    changed="$(printf '%s\n' "$changed" | grep -vx 'casa/config\.yaml' || true)"
  fi
fi

# The claim map is editable in the same change, so consult the BASE manifest too:
# deleting a covers anchor must not delete the obligation.
#
# FAIL CLOSED. Substituting an empty manifest on any error silently drops every
# base-side claim — the exact obligations this exists to preserve. An empty one is
# accepted only when the base genuinely has no manifest, which is true until the
# corpus first lands and false forever after.
if git cat-file -e "$base:docs/manifest.yaml" 2>/dev/null; then
  git show "$base:docs/manifest.yaml" > "$tmp/base-manifest.yaml"
  # #367: base-side claims live in manifest shards too. Each shard is a top-level
  # YAML list, so plain concatenation yields one valid list. `grep || true`: a base
  # with no shards (grep exit 1) must not kill the run under pipefail.
  git ls-tree --name-only "$base" -- docs/manifest.d/ 2>/dev/null \
    | { grep '\.yaml$' || true; } | while read -r shard; do
        git show "$base:$shard" >> "$tmp/base-manifest.yaml"
      done
else
  echo "note: $base has no docs/manifest.yaml yet — no base-side claims to carry"
  : > "$tmp/base-manifest.yaml"
fi

impacted="$(printf '%s\n' "$changed" \
  | "$python_bin" -m scripts.verify_docs . --impact --base-manifest "$tmp/base-manifest.yaml")"
[ -n "$impacted" ] || exit 0

# A DELETED doc is not an updated doc.
touched="$(git diff --name-only --diff-filter=d "$base"...HEAD | grep '^docs/' | sed 's|^docs/||' || true)"
deleted="$(git diff --name-only --diff-filter=D "$base"...HEAD | grep '^docs/' | sed 's|^docs/||' || true)"

# A claimed surface can genuinely change without invalidating the prose — the claim
# is file-level, so editing one function in a file whose OTHER symbol the document
# quotes impacts nothing readers can see. Requiring a doc edit anyway would buy
# cosmetic commits and teach everyone to make them, which is how a gate stops
# meaning anything. So there is an explicit, per-document, reasoned waiver,
# recorded in a commit message and therefore in history:
#
#     Docs-impact: architecture/tools-interface.md — none (claimed symbols unchanged)
#
# Per-document ON PURPOSE: one blanket waiver would let a six-document change
# through on a single line, and looking at each document is the whole point.
#
# Read from the ACK COMMIT ONLY, as a trailer at column zero. This mirrors
# scripts/attest.sh, whose receipts a new commit voids on purpose: a waiver is a
# statement about the diff as it finally stands, so adding another commit must
# invalidate it rather than let a line written five commits ago — possibly since
# reverted, cherry-picked, or merged in from elsewhere — waive a surface it never
# saw. Column zero also stops an indented EXAMPLE inside prose (a commit that
# documents this very mechanism, say) from acting as a live waiver.
#
# ACCEPTED RESIDUAL (both reviewers, rounds 1-2, deliberately not chased): this
# cannot verify that a waiver is SINCERE. `--amend --no-edit` after further edits,
# `git commit -C <old>`, or a reason of "abc" all satisfy the letter. No text check
# can do better — a determined author can equally write a false reason — so the
# mechanism aims at the failure that actually happened: a batch merged before
# anyone looked. What it guarantees is narrow and worth having: nobody publishes
# without naming each impacted document and writing a sentence about it, on the
# record. Substance is a review question. Do not add a diff-digest binding scheme
# without evidence of a real case it would have caught.
: > "$tmp/acked.txt"
git log -1 --format=%B "$ack_commit" \
  | sed -n 's/^[Dd]ocs-[Ii]mpact:[[:space:]]*//p' \
  > "$tmp/acks.txt"
# Read from a FILE, never a pipe: a `while` on the right of a pipe runs in a
# subshell, where `exit 1` would end the subshell and let the run continue.
while IFS= read -r ack; do
  [ -n "$ack" ] || continue
  ack_doc="${ack%%[[:space:]]*}"
  ack_rest="$(printf '%s' "${ack#"$ack_doc"}" | sed 's/^[[:space:]]*//')"
  # A separator is required, so `Docs-impact: <doc> <doc>` cannot pass by looking
  # like a reason.
  case "$ack_rest" in
    "—"*|"--"*|"-"*) ;;
    *)
      err "Docs-impact for '$ack_doc' needs '— <reason>'."
      exit 1 ;;
  esac
  ack_reason="$(printf '%s' "$ack_rest" | sed 's/^\(—\|--\|-\)[[:space:]]*//')"
  # A REAL reason: at least three alphanumerics, so punctuation, a second
  # separator, or a lone character cannot stand in for having thought about it;
  # and not merely the document's own name echoed back.
  ack_core="$(printf '%s' "$ack_reason" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
  ack_docname="$(printf '%s' "$ack_doc" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]')"
  if [ "${#ack_core}" -lt 3 ] || [ "$ack_core" = "$ack_docname" ]; then
    err "Docs-impact for '$ack_doc' has no real reason: '$ack_reason'"
    err "Say why the prose is still true, in words."
    exit 1
  fi
  printf '%s\n' "$ack_doc" >> "$tmp/acked.txt"
  echo "note: docs-impact acknowledged for $ack_doc — $ack_reason"
done < "$tmp/acks.txt"

# One path per line, never `for doc in $impacted`: word splitting would break a
# path containing a space into fragments, and matching those fragments against
# `touched` could satisfy the check while the real claiming document is untouched.
# The verifier separately forbids whitespace in manifest paths; this does not rely
# on that holding.
: > "$tmp/missing.txt"
while IFS= read -r doc; do
  [ -n "$doc" ] || continue
  if printf '%s\n' "$deleted" | grep -qxF -- "$doc"; then
    err "$doc claimed a changed surface and was deleted in the same change."
    err "Retire a claimed document on its own, not alongside the change."
    exit 1
  fi
  printf '%s\n' "$touched" | grep -qxF -- "$doc" && continue
  grep -qxF -- "$doc" "$tmp/acked.txt" 2>/dev/null && continue
  printf '%s\n' "$doc" >> "$tmp/missing.txt"
done <<< "$impacted"

if [ -s "$tmp/missing.txt" ]; then
  err "changed a claimed surface; these docs claim it but did not change:"
  while IFS= read -r doc; do err "  $doc"; done < "$tmp/missing.txt"
  err "Update each document, or acknowledge it in the tip commit message:"
  err "  Docs-impact: <doc> — <why the prose is still true>"
  exit 1
fi
