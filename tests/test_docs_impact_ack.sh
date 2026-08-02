#!/usr/bin/env bash
# Behavioural harness for the waiver logic in .github/workflows/docs.yml
# ("Docs impact on claimed surfaces").
#
# That step is the corpus's drift gate, and its acknowledgement parser is the
# one part of it that can let drift through deliberately — but it runs only on
# pull requests, so without this harness it is first exercised by the very PR
# that depends on it. Everything below drives the REAL decision block, sliced
# out of the workflow at run time, so the harness cannot pass against a stale
# copy of the logic.
#
# The slice runs from the acknowledgement collection to the end of the step,
# and covers both halves: producing `acked.txt`, and the impacted/touched/acked
# decision that writes `missing.txt`. Its inputs (`impacted`, `touched`,
# `deleted`) are set per case, standing in for what the earlier half of the step
# computes from the manifest and the diff.
#
# Run: bash tests/test_docs_impact_ack.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export RUNNER_TEMP="$work"

fails=0
pass() { echo "ok   $1"; }
fail() { echo "FAIL $1"; shift; [ $# -gt 0 ] && printf '%s\n' "$@" | sed 's/^/       /'; fails=$((fails + 1)); }

# The decision block, lifted verbatim. Anchored on markers that are themselves
# load-bearing lines of the step; if an edit moves them, extraction fails loudly
# rather than silently testing nothing.
extract_block() {
  python3 - "$repo_root" <<'PY'
import pathlib, sys, yaml
root = pathlib.Path(sys.argv[1])
wf = yaml.safe_load((root / ".github/workflows/docs.yml").read_text())
for job in wf["jobs"].values():
    for step in job.get("steps", []):
        if step.get("name") != "Docs impact on claimed surfaces":
            continue
        body = step["run"]
        start_marker = ': > "$RUNNER_TEMP/acked.txt"'
        end_marker = "fi"
        if start_marker not in body:
            raise SystemExit("harness: acknowledgement block start marker not found — "
                             "the step was restructured; update this harness")
        block = body[body.index(start_marker):].rstrip()
        if not block.endswith(end_marker):
            raise SystemExit("harness: step no longer ends with the missing.txt check — "
                             "update this harness")
        for needle in ('"$RUNNER_TEMP/missing.txt"', 'acked.txt', '$impacted'):
            if needle not in block:
                raise SystemExit(f"harness: extracted block lost {needle!r} — "
                                 "the decision moved out of the slice")
        print(block)
        sys.exit(0)
raise SystemExit("harness: step 'Docs impact on claimed surfaces' not found")
PY
}

block="$(extract_block)"

# Drive the block with a given impacted/touched/deleted set against the repo's
# current tip commit message.
run_block() {  # run_block <impacted> <touched> <deleted>   [ACK_COMMIT in env]
  ( set -euo pipefail
    cd "$work/repo"
    : > "$RUNNER_TEMP/missing.txt"
    impacted="$1" touched="$2" deleted="$3"
    # Production passes the PR head sha; default to the tip for ordinary cases.
    export ACK_COMMIT="${ACK_COMMIT-$(git rev-parse HEAD)}"
    eval "$block" )
}

expect() {  # expect <name> <ok|fail> <impacted> <touched> <deleted> [needle]
  local name="$1" want="$2" imp="$3" tch="$4" del="$5" needle="${6:--}" rc=0 out
  out="$(run_block "$imp" "$tch" "$del" 2>&1)" || rc=$?
  if [ "$want" = ok ] && [ "$rc" -ne 0 ]; then fail "$name (wanted pass, rc=$rc)" "$out"; return; fi
  if [ "$want" = fail ] && [ "$rc" -eq 0 ]; then fail "$name (wanted failure)" "$out"; return; fi
  if [ "$needle" != "-" ] && ! printf '%s' "$out" | grep -qF -- "$needle"; then
    fail "$name (missing '$needle')" "$out"; return
  fi
  pass "$name"
}

git init -q "$work/repo"
cd "$work/repo"
# Deliberately not address-shaped: this repo's pre-commit hook refuses anything
# matching an email pattern, and git does not validate the field.
git config user.email harness; git config user.name harness
git commit -q --allow-empty -m "base"
git branch -M main
tip() { git commit -q --allow-empty -m "$1"; }
reset_pr() { git checkout -q main; git checkout -q -B pr; }

D1=architecture/telegram.md
D2=architecture/turn-loop.md

# --- the gate still bites -------------------------------------------------
reset_pr; tip "change with no waiver"
expect "unwaived impacted doc fails" fail "$D1" "" "" "these docs claim it but did not change"

reset_pr; tip "change"
expect "touched doc satisfies the gate" ok "$D1" "$D1" ""

# --- a well-formed waiver -------------------------------------------------
reset_pr; tip "change

Docs-impact: $D1 — none, the claimed symbols were not modified"
expect "reasoned waiver accepted" ok "$D1" "" "" "acknowledged for $D1"

# --- reasons that are not reasons ----------------------------------------
reset_pr; tip "change

Docs-impact: $D1"
expect "no separator rejected" fail "$D1" "" "" "needs"

reset_pr; tip "change

Docs-impact: $D1 —"
expect "separator with nothing after it rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — ."
expect "punctuation-only reason rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — --"
expect "second separator as reason rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 — $D1"
expect "doc name echoed back rejected" fail "$D1" "" "" "no real reason"

reset_pr; tip "change

Docs-impact: $D1 - still accurate here"
expect "plain hyphen separator accepted" ok "$D1" "" "" "acknowledged for $D1"

# --- no blanket waiver ----------------------------------------------------
reset_pr; tip "change

Docs-impact: $D1 — reason one"
expect "one waiver does not cover a second doc" fail "$D1
$D2" "" "" "$D2"

reset_pr; tip "change

Docs-impact: $D1 — reason one
Docs-impact: $D2 — reason two"
expect "per-document waivers cover both" ok "$D1
$D2" "" ""

# --- the waiver is a statement about the FINAL diff -----------------------
reset_pr
tip "change

Docs-impact: $D1 — considered at the time"
tip "a later commit that changed more"
expect "waiver in an earlier commit does not carry" fail "$D1" "" "" "did not change"

git checkout -q main
tip "base-side waiver

Docs-impact: $D1 — waived on main"
git checkout -q -B pr; tip "change with no waiver of its own"
expect "base-side waiver does not carry" fail "$D1" "" "" "did not change"

# --- an indented example is prose, not a waiver ---------------------------
reset_pr; tip "document the mechanism

Write it like this:
    Docs-impact: $D1 — some reason"
expect "indented example is not a waiver" fail "$D1" "" "" "did not change"

# --- a deleted claimant is refused outright -------------------------------
reset_pr; tip "change"
expect "deleting a claimant is refused" fail "$D1" "" "$D1" "deleted in the same PR"

# --- document names match WHOLE, never as substrings ----------------------
# Terra r2 found the harness blind to `grep -qxF` decaying to `grep -qF`: with
# no name that contains another, substring matching passes every case. These
# two pin it on both the waiver and the touched paths.
# SUPER must genuinely CONTAIN SUB, or the case proves nothing: the realistic
# form is a waiver written with the `docs/` prefix the impacted list never uses.
SUB=architecture/memory.md
SUPER=docs/architecture/memory.md
case "$SUPER" in *"$SUB"*) ;; *) fail "harness bug: SUPER does not contain SUB"; esac
reset_pr; tip "change

Docs-impact: $SUPER — written with the wrong path form"
expect "waiver naming a superstring path does not cover the document" fail "$SUB" "" "" "$SUB"

reset_pr; tip "change"
expect "touching a superstring path does not cover the document" fail "$SUB" "$SUPER" "" "$SUB"

# --- the production checkout is a MERGE commit ----------------------------
# On `pull_request`, actions/checkout leaves HEAD at GitHub's synthetic merge
# commit, whose message carries no waiver. The step must read the contributor's
# tip instead — the bug both reviewers caught in round 2.
reset_pr
tip "change

Docs-impact: $D1 — none, claimed symbols untouched"
pr_tip="$(git rev-parse HEAD)"
git checkout -q main
git merge -q --no-ff -m "Merge $pr_tip into main" pr
ACK_COMMIT="$pr_tip" expect "waiver is read from the PR tip, not the merge commit" \
  ok "$D1" "" "" "acknowledged for $D1"
unset ACK_COMMIT || true
expect "reading the merge commit instead finds no waiver" fail "$D1" "" "" "did not change"
git checkout -q main; git reset -q --hard HEAD~1

# --- a missing head sha fails closed --------------------------------------
reset_pr; tip "change

Docs-impact: $D1 — some genuine reason"
ACK_COMMIT="" expect "empty ACK_COMMIT fails closed" fail "$D1" "" "" "ACK_COMMIT is empty"
unset ACK_COMMIT || true

echo
[ "$fails" -eq 0 ] && { echo "docs-impact gate: all checks passed"; exit 0; }
echo "docs-impact gate: $fails check(s) failed"; exit 1
