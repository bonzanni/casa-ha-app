#!/usr/bin/env bash
# Record that the manual gates happened, bound to the exact commit they happened on.
#
#   scripts/attest.sh --read-in-full --reviewers "sol,terra" --findings-applied --re-reviewed
#   scripts/attest.sh ... --object v1.2.3     (attest a TAG; its annotation is swept)
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

read_in_full=0; findings_applied=0; re_reviewed=0; reviewers=""; object=""; tag_ref=""
while [ $# -gt 0 ]; do
  case "$1" in
    --read-in-full)     read_in_full=1 ;;
    --findings-applied) findings_applied=1 ;;
    --re-reviewed)      re_reviewed=1 ;;
    --reviewers)        shift; reviewers="${1:-}" ;;
    # An annotated tag's object sha is never HEAD's commit sha, so a tag push could not be
    # attested at all through the commit-only flow. The tag ANNOTATION is published text
    # that no range or message sweep covers, which is why the hook gates tags.
    --object)           shift; object="${1:-}" ;;
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
if [ -n "$object" ]; then
  head_sha="$(git rev-parse --verify "$object^{}" >/dev/null 2>&1 && git rev-parse "$object")" || {
    echo "✋ attest: --object '$object' does not resolve" >&2; exit 1; }
  # Sweep the WHOLE tag object, headers included, and scan it for secrets.
  #
  # An earlier version discarded the headers with `sed -n '/^$/,$p'`, which threw away the
  # `tagger` line — a name and an e-mail address, published verbatim. It also never ran the
  # secret scanner, so a recognisable credential in an annotation passed a fully attested
  # path. The ref NAME is published too and is recorded here so pre-push can bind to it:
  # otherwise the same attested object could be pushed under a different, unswept name.
  if [ "$(git cat-file -t "$object" 2>/dev/null)" = "tag" ]; then
    tagfile="$(git rev-parse --git-path casa-tag-object)"
    git cat-file tag "$object" > "$tagfile"
    printf '%s\n' "$object" >> "$tagfile"          # the ref name is public text too
    scripts/sweep-text.sh "$tagfile" || {
      echo "✋ attest: the tag object (annotation, tagger identity or name) carries" >&2
      echo "        denied content. It is published verbatim." >&2
      exit 1; }
    probe_dir="$(mktemp -d)"; cp "$tagfile" "$probe_dir/tag.txt"
    if ! gitleaks dir "$probe_dir" --config .gitleaks.toml --no-banner >/dev/null 2>&1; then
      rm -rf "$probe_dir"
      echo "✋ attest: the secret scanner flagged the tag object." >&2; exit 1
    fi
    rm -rf "$probe_dir"
    tag_ref="$object"
  fi
else
  head_sha="$(git rev-parse HEAD)"
fi
automated="$(git rev-parse --git-path casa-gate-automated)"
[ -f "$automated" ] || { echo "✋ attest: run scripts/gate.sh first." >&2; exit 1; }
# A tag attestation stands on the gate run for the commit it points at.
expected="$head_sha"
[ -n "$object" ] && expected="$(git rev-parse "$object^{commit}")"
[ "$(cat "$automated")" = "$expected" ] || {
  echo "✋ attest: the automated receipt is for a different commit. Re-run scripts/gate.sh." >&2
  exit 1
}

printf '%s\nread-in-full; reviewers=%s; findings-applied; re-reviewed; ref=%s\n' \
  "$head_sha" "$reviewers" "${tag_ref:-}" > "$(git rev-parse --git-path casa-gate-approved)"
echo "✓ attested $head_sha"
