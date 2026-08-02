#!/usr/bin/env bash
# Behavioural harness for the `Docs-impact:` acknowledgement parser in
# .github/workflows/docs.yml ("Docs impact on claimed surfaces").
#
# The step itself only runs on GitHub, so its logic is otherwise unexercised
# until a pull request depends on it. This extracts the acknowledgement block
# from the workflow — the real text, never a copy that could drift — and drives
# it over synthetic commit ranges.
#
# Run: bash tests/test_docs_impact_ack.sh
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
export RUNNER_TEMP="$work"

fails=0
check() {  # check <name> <expected: ok|fail> <expected-substring-or-->
  local name="$1" expect="$2" needle="$3" rc=0 out
  out="$(run_ack 2>&1)" || rc=$?
  if [ "$expect" = ok ] && [ "$rc" -ne 0 ]; then
    echo "FAIL $name: expected success, got rc=$rc"; echo "$out" | sed 's/^/    /'
    fails=$((fails + 1)); return
  fi
  if [ "$expect" = fail ] && [ "$rc" -eq 0 ]; then
    echo "FAIL $name: expected failure, got success"; echo "$out" | sed 's/^/    /'
    fails=$((fails + 1)); return
  fi
  if [ "$needle" != "-" ] && ! printf '%s' "$out" | grep -qF -- "$needle"; then
    echo "FAIL $name: output missing '$needle'"; echo "$out" | sed 's/^/    /'
    fails=$((fails + 1)); return
  fi
  echo "ok   $name"
}

# The acknowledgement parser, lifted verbatim from the workflow so this harness
# cannot pass against a stale copy of the logic.
extract_parser() {
  python3 - "$repo_root" <<'PY'
import pathlib, sys, yaml
root = pathlib.Path(sys.argv[1])
wf = yaml.safe_load((root / ".github/workflows/docs.yml").read_text())
for job in wf["jobs"].values():
    for step in job.get("steps", []):
        if step.get("name") == "Docs impact on claimed surfaces":
            body = step["run"]
            start = body.index(': > "$RUNNER_TEMP/acked.txt"')
            end = body.index('done < "$RUNNER_TEMP/acks.txt"') + len('done < "$RUNNER_TEMP/acks.txt"')
            print(body[start:end])
            sys.exit(0)
raise SystemExit("acknowledgement block not found in docs.yml")
PY
}

parser="$(extract_parser)"

run_ack() {
  ( set -euo pipefail
    cd "$work/repo"
    base="$(git rev-parse main)"
    eval "$parser" )
}

# A throwaway repo whose commit messages carry the acknowledgements.
git init -q "$work/repo"
cd "$work/repo"
# Deliberately not address-shaped: this repo's pre-commit hook refuses anything
# matching an email pattern, and git does not validate the field.
git config user.email harness; git config user.name harness
git commit -q --allow-empty -m "base"
git branch -M main
git checkout -q -b pr

commit() { git commit -q --allow-empty -m "$1"; }

# 1. A well-formed acknowledgement is accepted and records its document.
commit "change

Docs-impact: architecture/tools-interface.md — none (claimed symbols unchanged)"
check "well-formed ack accepted" ok "acknowledged for architecture/tools-interface.md"
grep -qx "architecture/tools-interface.md" "$work/acked.txt" \
  || { echo "FAIL: acked.txt missing the document"; fails=$((fails + 1)); }

# 2. A reason is mandatory — the bare form is the one that would turn this into
#    a rubber stamp, so it must fail loudly.
git checkout -q main; git checkout -q -B pr
commit "change

Docs-impact: architecture/telegram.md"
check "ack without a reason rejected" fail "has no reason"

# 3. A plain-hyphen separator is accepted (not everyone types an em dash).
git checkout -q main; git checkout -q -B pr
commit "change

Docs-impact: architecture/telegram.md - still accurate"
check "hyphen separator accepted" ok "acknowledged for architecture/telegram.md"

# 4. Separator alone, no words after it, is still no reason.
git checkout -q main; git checkout -q -B pr
commit "change

Docs-impact: architecture/telegram.md —"
check "separator with no words rejected" fail "has no reason"

# 5. Several documents need several lines — there is no blanket waiver.
git checkout -q main; git checkout -q -B pr
commit "change

Docs-impact: architecture/telegram.md — a
Docs-impact: architecture/turn-loop.md — b"
check "multiple acks parse" ok "acknowledged for architecture/turn-loop.md"
[ "$(wc -l < "$work/acked.txt")" -eq 2 ] \
  || { echo "FAIL: expected 2 acked docs"; fails=$((fails + 1)); }

# 6. An acknowledgement on the BASE branch must not carry over — that is the
#    two-dot-versus-three-dot bug this range spelling exists to avoid.
git checkout -q main
commit "base-side waiver

Docs-impact: architecture/telegram.md — waived on main"
git checkout -q -B pr
commit "change with no waiver of its own"
check "base-side ack does not carry" ok "-"
[ ! -s "$work/acked.txt" ] \
  || { echo "FAIL: base-side ack leaked into this PR"; fails=$((fails + 1)); }

echo
[ "$fails" -eq 0 ] && { echo "docs-impact ack: all checks passed"; exit 0; }
echo "docs-impact ack: $fails check(s) failed"; exit 1
