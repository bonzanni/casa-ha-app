#!/usr/bin/env bash
# Sweep an arbitrary text file with the same rules the corpus gets.
#
#   scripts/sweep-text.sh /tmp/pr-body.md
#
# PR titles, PR bodies, squash subjects and squash bodies are all public the moment they
# are submitted, and none of them is a commit any gate ever saw. Each goes into a file and
# through this sweeper before it is used.
#
# It SOURCES scripts/deny-sweep.sh as a library rather than reimplementing the grammar:
# three divergent copies disagreed once, and one of them would have failed CI on the
# project's own public identity.
set -euo pipefail
# Resolve the library BEFORE changing directory: $0 may be relative to where we started.
lib_dir="$(cd "$(dirname "$0")" && pwd)"
cd "$(git rev-parse --show-toplevel)"

file="${1:?usage: sweep-text.sh <file>}"
[ -r "$file" ] || { echo "✋ sweep-text: $file is not readable" >&2; exit 2; }

# Source FIRST: the library returns before allocating any workspace of its own, so nothing
# of ours is clobbered.
# shellcheck source=deny-sweep.sh
CASA_SWEEP_LIB=1 . "$lib_dir/deny-sweep.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cp "$file" "$work/body"

fail=0
sweep_one() {                            # $1=pattern $2=label $3=1 to skip allow rules
  local pat="$1" label="$2" skip_allow="$3" status line text
  set +e
  grep -noE -- "$pat" "$work/body" > "$work/matches"
  status=$?
  set -e
  [ "$status" -ge 2 ] && { echo "✋ sweep-text: pattern /$pat/ failed" >&2; exit 2; }
  while IFS= read -r line; do
    text="${line#*:}"
    if [ "$skip_allow" = 0 ]; then
      local allowed=1 apat
      for apat in ${allow_pats[@]+"${allow_pats[@]}"}; do
        printf '%s' "$text" | grep -qxE -- "$apat" && { allowed=0; break; }
      done
      [ "$allowed" = 0 ] && continue
    fi
    echo "✋ $file matches $label /$pat/: $text" >&2
    fail=1
  done < "$work/matches"
  return 0
}

for pat in ${generic_content_pats[@]+"${generic_content_pats[@]}"}; do
  sweep_one "$pat" "generic" 0
done
for pat in ${supplement_pats[@]+"${supplement_pats[@]}"}; do
  sweep_one "$pat" "private" 1
done

[ "$fail" -eq 0 ] && echo "✓ $file clean"
exit "$fail"
