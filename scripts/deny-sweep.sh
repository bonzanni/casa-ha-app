#!/usr/bin/env bash
# The one implementation of the deny-pattern grammar. The pre-commit hook, the gate and
# CI all go through this file — three divergent copies disagreed once, and one of them
# would have failed CI on the project's own public identity.
#
#   scripts/deny-sweep.sh staged            (pre-commit: added lines in the index)
#   scripts/deny-sweep.sh tree              (whole tracked tree at HEAD)
#   scripts/deny-sweep.sh range <git-range> (content introduced by every commit in range)
#   scripts/deny-sweep.sh messages <range>  (commit messages — they are published too)
#
# It operates on the repository containing the CURRENT DIRECTORY, not the one containing
# this script: the tests drive it against a throwaway repo, and a `cd` to the script's own
# root would silently scan the project instead.
#
# CASA_DENY_FILE        override the pattern file (tests)
# CASA_DENY_SUPPLEMENT  optional private pattern file with exact literals
# CASA_SWEEP_LIB=1      source instead of run: defines the pattern arrays, scans nothing
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

mode="${1:-tree}"
range="${2:-}"
deny_file="${CASA_DENY_FILE:-.githooks/deny-patterns.txt}"
# Exclude whichever pattern file is actually in use, not just the canonical path: an
# override pointing at a file inside the repo would otherwise be swept and match its own
# rules. Resolved repo-relative; an override outside the repo excludes nothing, correctly.
deny_rel="$(realpath --relative-base="$PWD" -- "$deny_file" 2>/dev/null || echo "$deny_file")"
case "$deny_rel" in /*) deny_rel=".githooks/deny-patterns.txt" ;; esac

path_pats=(); allow_pats=(); generic_content_pats=(); supplement_pats=()
read_patterns() {                       # $1=file  $2=1 if this is the private supplement
  local file="$1" is_supplement="${2:-0}" section="" line
  # Fail CLOSED. An unreadable policy file used to load zero rules and exit 0, so a commit
  # that deleted .githooks/deny-patterns.txt disabled its own guard — and in staged mode a
  # deletion is not even in --diff-filter=ACMR, so nothing else would have noticed.
  if [ -z "$file" ]; then
    [ "$is_supplement" = 1 ] && return 0
    echo "✋ deny-sweep: no pattern file configured" >&2; exit 2
  fi
  if [ ! -r "$file" ]; then
    echo "✋ deny-sweep: pattern file $file is missing or unreadable — refusing to run" >&2
    echo "        with no policy. This is the guard failing closed, as designed." >&2
    exit 2
  fi
  while IFS= read -r line; do
    case "$line" in
      ""|\#*) continue ;;
      "[paths]")         section=paths;   continue ;;
      "[content]")       section=content; continue ;;
      "[allow-content]") section=allow;   continue ;;
    esac
    case "$section" in
      paths)   path_pats+=("$line") ;;
      allow)   allow_pats+=("$line") ;;
      content)
        if [ "$is_supplement" = 1 ]; then
          supplement_pats+=("$line")      # matched against the RAW text
        else
          generic_content_pats+=("$line") # matched after allow-substitution
        fi ;;
    esac
  done < "$file"
}
read_patterns "$deny_file" 0
read_patterns "${CASA_DENY_SUPPLEMENT:-}" 1

# "Fails closed" has to cover an INVALID policy, not only an unreadable one. A blank or
# malformed pattern file used to parse into empty arrays, and the file is itself excluded
# from the primary content sweep — so a committed blank policy disabled the guard while
# every check still reported success.
for required in '[paths]' '[content]' '[allow-content]'; do
  grep -qxF -- "$required" "$deny_file" || {
    echo "✋ deny-sweep: $deny_file has no $required section — refusing to run on a" >&2
    echo "        malformed policy. An empty policy is indistinguishable from no rules." >&2
    exit 2
  }
done
if [ "${#generic_content_pats[@]}" -eq 0 ] || [ "${#path_pats[@]}" -eq 0 ]; then
  echo "✋ deny-sweep: $deny_file declares no path or content rules — refusing to run." >&2
  exit 2
fi

[ -n "${CASA_SWEEP_LIB:-}" ] && return 0   # sourced as a library: patterns only, no state

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# grep exits 0=match, 1=no match, 2=bad expression. Never capture $? after `!` — the
# negation rewrites it to 0, so a perfectly good pattern reads as invalid and the whole
# sweep fails closed on its first real rule.
: > "$work/empty"
die_on_bad_pattern() {
  set +e
  grep -E "$1" "$work/empty" >/dev/null 2>&1
  local status=$?
  set -e
  [ "$status" -le 1 ] || { echo "✋ deny-sweep: invalid pattern /$1/" >&2; exit 2; }
}
for pat in ${path_pats[@]+"${path_pats[@]}"} ${generic_content_pats[@]+"${generic_content_pats[@]}"} \
           ${supplement_pats[@]+"${supplement_pats[@]}"} ${allow_pats[@]+"${allow_pats[@]}"}; do
  die_on_bad_pattern "$pat"
done

# An allow rule is a whole-match exemption, so `.*` under [allow-content] would exempt
# every finding there is. Refuse any allow rule that whole-matches an arbitrary canary.
for pat in ${allow_pats[@]+"${allow_pats[@]}"}; do
  # Assembled at runtime for the same reason as the marker: written literally, these
  # canaries are themselves findings and this file could not be committed.
  canary_mail="nobody@nowhere"".invalid"
  canary_addr="10.11.12"".13"
  for canary in 'CANARY-9f3c' "$canary_mail" "$canary_addr"; do
    if printf '%s' "$canary" | grep -qxE -- "$pat" 2>/dev/null; then
      echo "✋ deny-sweep: allow rule /$pat/ matches the canary '$canary' — it is broad" >&2
      echo "        enough to exempt unrelated findings. Allow rules name ONE value." >&2
      exit 2
    fi
  done
done

fail=0

# An allow entry exempts a match only when it covers the match ENTIRELY.
#
# The previous implementation deleted allow matches from the text with `sed s///g`, which
# is an unanchored substring rewrite: a broader match ending in an allowed value had
# that value deleted, and the remainder no longer matched — a real address passed.
# Whole-match semantics cannot do that: an allow rule either IS the finding, or is
# irrelevant to it.
allowed_match() {                        # $1=the matched text; 0 when wholly allowed
  local text="$1" pat
  for pat in ${allow_pats[@]+"${allow_pats[@]}"}; do
    printf '%s' "$text" | grep -qxE -- "$pat" && return 0
  done
  return 1
}

scan() {                                 # $1=pattern $2=input file $3=label $4=1 to skip allow
  local pat="$1" file="$2" label="$3" skip_allow="${4:-0}" status text hit=0 lineno
  set +e
  grep -noE -- "$pat" "$file" > "$work/matches"
  status=$?
  set -e
  if [ "$status" -ge 2 ]; then
    echo "✋ deny-sweep: pattern /$pat/ failed at runtime" >&2
    exit 2
  fi
  while IFS= read -r line; do
    text="${line#*:}"
    if [ "$skip_allow" = 0 ] && allowed_match "$text"; then
      continue
    fi
    if [ "$hit" -eq 0 ]; then
      echo "✋ deny $label /$pat/:" >&2
      hit=1
    fi
    # `grep -oE` gives the fragment, which is what the allow decision needs; the operator
    # needs the whole line to know WHICH file. Report both.
    lineno="${line%%:*}"
    printf '   %s\n' "$(sed -n "${lineno}p" "$file" | cut -c1-160)" >&2
  done < "$work/matches"
  [ "$hit" -eq 1 ] && fail=1
  return 0
}

# --- paths ---
# `range` enumerates EVERY commit, not the endpoint diff: a denied path added in one
# commit and deleted in the next vanishes from `git diff base..HEAD` while its blob is
# published all the same.
case "$mode" in
  staged) git diff --cached --name-only --diff-filter=ACMR > "$work/paths" ;;
  tree)   git ls-files > "$work/paths" ;;
  range)  # -m: emit a diff against EACH parent. Without it git prints no file list at all
          # for a merge, so a denied path introduced by conflict resolution and removed
          # later is invisible to both this and the content pass.
          git rev-list "$range" | while read -r commit; do
            git show --name-only --format= -m "$commit"
          done | sort -u > "$work/paths" ;;
  messages) : > "$work/paths" ;;
  *)      echo "✋ deny-sweep: unknown mode $mode" >&2; exit 2 ;;
esac
# A path containing a newline is C-quoted by git, so anchored path rules stop matching and
# such a commit could evade both gated-path detection and path denial. Refuse the name
# rather than try to parse it.
if grep -q '^"' "$work/paths" 2>/dev/null; then
  echo "✋ deny-sweep: path name(s) containing control characters:" >&2
  grep '^"' "$work/paths" | head -5 | sed 's/^/   /' >&2
  echo "   git C-quotes these, which defeats anchored path rules. Rename them." >&2
  fail=1
fi
for pat in ${path_pats[@]+"${path_pats[@]}"}; do
  scan "$pat" "$work/paths" "path" 1
done

# --- root-file allowlist: the generic replacement for naming private artefacts ---
if [ "$mode" != "messages" ]; then
  if [ ! -r .githooks/root-allowlist.txt ]; then
    echo "✋ deny-sweep: .githooks/root-allowlist.txt is missing — refusing to run with" >&2
    echo "        the root-file check silently disabled." >&2
    exit 2
  fi
  grep -vE '/' "$work/paths" | grep -vxF -f .githooks/root-allowlist.txt > "$work/strays" || true
  if [ -s "$work/strays" ]; then
    echo "✋ root-level file(s) outside .githooks/root-allowlist.txt:" >&2
    cat "$work/strays" >&2
    fail=1
  fi
fi

# --- binary blobs: unscannable, therefore not publishable without an explicit decision ---
# `git grep -I` skips binaries and patches say only "Binary files differ", so one NUL byte
# would otherwise make any payload invisible to every content rule below.
if [ "$mode" != "messages" ]; then
  : > "$work/binaries"
  case "$mode" in
    tree)
      git ls-tree -r --name-only HEAD | sort > "$work/all"
      # `git grep -Il` exits 1 when NOTHING is textual; under `pipefail` that killed the
      # whole sweep with an empty message — a silent failure in the guard itself.
      { git grep -Il '' HEAD -- . 2>/dev/null || true; } | sed 's|^HEAD:||' | sort > "$work/textual"
      comm -23 "$work/all" "$work/textual" > "$work/binaries" ;;
    range)
      # numstat prints `-	-	<path>` for a binary change.
      { git log --numstat --format= -m "$range" 2>/dev/null || true; } \
        | awk -F'\t' '$1=="-" && $2=="-" {print $3}' | sort -u > "$work/binaries" ;;
    staged)
      { git diff --cached --numstat --diff-filter=ACMR || true; } \
        | awk -F'\t' '$1=="-" && $2=="-" {print $3}' | sort -u > "$work/binaries" ;;
  esac
  if [ -s "$work/binaries" ]; then
    if [ -f .githooks/binary-allowlist.txt ]; then
      grep -vxF -f .githooks/binary-allowlist.txt "$work/binaries" > "$work/new-binaries" || true
    else
      cp "$work/binaries" "$work/new-binaries"
    fi
    if [ -s "$work/new-binaries" ]; then
      echo "✋ binary blob(s) no content rule can inspect:" >&2
      sed 's/^/   /' "$work/new-binaries" >&2
      echo "   A binary cannot be swept or read in review. Add it to" >&2
      echo "   .githooks/binary-allowlist.txt in the same commit, having looked at it." >&2
      fail=1
    fi
  fi
fi

# --- the inline scanner allow-marker may only appear where it has been reviewed ---
# Without this the marker is an unrestricted scanner bypass: a real credential plus the
# comment produces a clean secret scan.
if [ "$mode" != "messages" ]; then
  if [ ! -r .githooks/gitleaks-allow-sites.txt ]; then
    echo "✋ deny-sweep: .githooks/gitleaks-allow-sites.txt is missing — refusing to run" >&2
    echo "        with the scanner-bypass check silently disabled." >&2
    exit 2
  fi
  # Assembled at runtime: writing the marker literally here would make THIS file a
  # bypass site by its own rule.
  marker="gitleaks"":""allow"
  # Mode-aware, and POLICY-VERSION-aware. A marker in commit C is authorised only if C's
  # own allowlist named it: comparing every commit's markers against the working-tree
  # policy would let a later commit retroactively authorise an earlier marker.
  : > "$work/new-allow-sites"
  check_sites() {                        # $1=file list  $2=policy file
    grep -vxF -f "$2" "$1" >> "$work/new-allow-sites" || true
  }
  case "$mode" in
    staged)
      { git grep -lI --cached -- "$marker" -- . 2>/dev/null || true; } | sort -u > "$work/allow-sites"
      git show ":.githooks/gitleaks-allow-sites.txt" > "$work/allow-policy" 2>/dev/null \
        || cp .githooks/gitleaks-allow-sites.txt "$work/allow-policy"
      check_sites "$work/allow-sites" "$work/allow-policy" ;;
    range)
      for commit in $(git rev-list "$range"); do
        { git grep -lI -- "$marker" "$commit" -- . 2>/dev/null || true; } \
          | sed 's|^[0-9a-f]*:||' | sort -u > "$work/allow-sites"
        [ -s "$work/allow-sites" ] || continue
        git show "$commit:.githooks/gitleaks-allow-sites.txt" > "$work/allow-policy" 2>/dev/null \
          || : > "$work/allow-policy"
        check_sites "$work/allow-sites" "$work/allow-policy"
      done ;;
    *)
      { git grep -lI -- "$marker" HEAD -- . 2>/dev/null || true; } | sed 's|^HEAD:||' | sort -u > "$work/allow-sites"
      cp .githooks/gitleaks-allow-sites.txt "$work/allow-policy"
      check_sites "$work/allow-sites" "$work/allow-policy" ;;
  esac
  sort -u -o "$work/new-allow-sites" "$work/new-allow-sites"
  if [ -s "$work/new-allow-sites" ]; then
    echo "✋ inline scanner allow-marker used in file(s) that are not reviewed sites:" >&2
    sed 's/^/   /' "$work/new-allow-sites" >&2
    echo "   That marker silences the secret scanner. Add the path to" >&2
    echo "   .githooks/gitleaks-allow-sites.txt in the same commit, having looked at it." >&2
    fail=1
  fi
fi

# --- content ---
case "$mode" in
  staged)   git diff --cached -U0 -- . ":!$deny_rel" | grep -E '^\+' | grep -vE '^\+\+\+' > "$work/body" || true ;;
  tree)     git grep -I --no-color -n '' HEAD -- . ":!$deny_rel" > "$work/body" || true ;;
  range)    git log -p -m --no-color "$range" -- . ":!$deny_rel" | grep -E '^\+' | grep -vE '^\+\+\+' > "$work/body" || true ;;
  messages) git log --format='%B' "$range" > "$work/body" || true ;;
esac
# Generic rules honour [allow-content]; the private supplement never does — an allow entry
# must not be able to exempt an exact private literal.
for pat in ${generic_content_pats[@]+"${generic_content_pats[@]}"}; do
  scan "$pat" "$work/body" "content" 0
done
for pat in ${supplement_pats[@]+"${supplement_pats[@]}"}; do
  scan "$pat" "$work/body" "content(private)" 1
done

# --- the pattern file: exclude only its DECLARED PATTERN LINES, not the whole file ---
# It is excluded from the sweep above because it necessarily contains the rules it
# defines. Excluding the entire file made it a hiding place for anything else, so the
# residue — comments, blank lines, and any stray content — is scanned with the generic
# rules like any other file.
# The residue must come from the version being ASSESSED. Reading the working-tree copy
# let a leaking version be staged behind a clean worktree file, and left every historical
# version of the file unexamined in range mode.
: > "$work/denyfile-src"
case "$mode" in
  staged) git show ":$deny_rel" >> "$work/denyfile-src" 2>/dev/null || true ;;
  tree)   git show "HEAD:$deny_rel" >> "$work/denyfile-src" 2>/dev/null || true ;;
  range)  git rev-list "$range" | while read -r commit; do
            git show "$commit:$deny_rel" 2>/dev/null || true
          done >> "$work/denyfile-src" ;;
esac
if [ -s "$work/denyfile-src" ]; then
  {
    printf '%s\n' ${path_pats[@]+"${path_pats[@]}"} ${generic_content_pats[@]+"${generic_content_pats[@]}"} \
                   ${supplement_pats[@]+"${supplement_pats[@]}"} ${allow_pats[@]+"${allow_pats[@]}"}
    printf '[paths]\n[content]\n[allow-content]\n'
  } > "$work/declared"
  grep -vxF -f "$work/declared" "$work/denyfile-src" > "$work/deny-residue" || true
  for pat in ${generic_content_pats[@]+"${generic_content_pats[@]}"}; do
    scan "$pat" "$work/deny-residue" "content(pattern-file residue)" 0
  done
fi

# --- the pattern file itself, swept with the PRIVATE patterns only ---
# It is excluded above because it necessarily contains the generic patterns it defines.
# The supplement's exact literals have no legitimate reason to appear there, so they can
# and must be applied — otherwise the one excluded file is a hiding place for exactly the
# literals the supplement exists to catch.
if [ -s "$work/denyfile-src" ] && [ "${#supplement_pats[@]}" -gt 0 ]; then
  for pat in "${supplement_pats[@]}"; do
    scan "$pat" "$work/denyfile-src" "content(pattern-file)" 1
  done
fi

exit "$fail"
