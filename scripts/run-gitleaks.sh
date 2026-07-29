#!/usr/bin/env bash
# Pinned secret scanner. One invocation, one exit status — never a `||` fallback, which
# cannot tell "unsupported subcommand" from "leak found" or "invalid config".
#
#   scripts/run-gitleaks.sh tree              scan a checkout of HEAD
#   scripts/run-gitleaks.sh range <git-range> scan the history the push would publish
#
# `tree` scans a `git archive` of HEAD rather than the working directory: `gitleaks dir .`
# also reads ignored local material (venv_test/, .claude/), none of which is published,
# and which makes the result machine-dependent.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

REQUIRED="8.28.0"
mode="${1:-tree}"
range="${2:-}"
config=".gitleaks.toml"

have="$(gitleaks version 2>/dev/null | tr -d 'v[:space:]')" || {
  echo "✋ gitleaks is not installed. Install $REQUIRED — the gate does not run without it." >&2
  exit 1
}
if [ "$have" != "$REQUIRED" ]; then
  echo "✋ gitleaks $have installed, $REQUIRED pinned. The config surface and the default" >&2
  echo "        ruleset both change between versions; a scan with an unexpected build is" >&2
  echo "        not the scan CI runs, and its clean result means less." >&2
  exit 1
fi

# An ineffective config reports zero findings, which is indistinguishable from a clean
# tree. Prove detection works before trusting a clean result. The fixture below was chosen
# by EXPERIMENT against this exact version: the canonical AWS secret-key example does NOT
# fire under the 8.28.0 default ruleset, and a wrapper built on it would have failed closed
# on every invocation.
probe="$(mktemp -d)"
trap 'rm -rf "$probe" "${export_dir:-}"' EXIT
probe_tok="xoxb-"                       # split so this tracked file holds no whole token
probe_tok="${probe_tok}123456789012-1234567890123-abcdefghijklmnopqrstuvwx"
printf 'slack_token = "%s"\n' "$probe_tok" > "$probe/probe.txt"
set +e
gitleaks dir "$probe" --config "$config" --no-banner --exit-code 9 >/dev/null 2>&1
probe_status=$?
set -e
if [ "$probe_status" -ne 9 ]; then
  echo "✋ gitleaks probe returned $probe_status, expected 9. Either the config is not" >&2
  echo "        effective or the fixture no longer matches a default rule. Both are fatal:" >&2
  echo "        a clean scan proves nothing unless detection is demonstrated first." >&2
  exit 1
fi

# There is deliberately NO baseline file. Accepted findings are annotated at the SITE
# with an inline gitleaks allow-marker, which a reviewer sees in the diff. A baseline was measured both
# ways and cannot work here: stored verbatim it carries the secrets and trips the scan
# itself (4 findings became 10); stored redacted it suppresses nothing.

case "$mode" in
  tree|range) ;;
  *) echo "✋ run-gitleaks.sh: unknown mode '$mode' (expected tree|range)" >&2; exit 2 ;;
esac

if [ "$mode" = "range" ]; then
  [ -n "$range" ] || { echo "✋ run-gitleaks.sh range needs a git range" >&2; exit 2; }
  # -m for the same reason the deny sweep needs it: git emits no patch for a merge by
  # default, so a secret created by conflict resolution and removed later is invisible.
  gitleaks git . --config "$config" --redact --no-banner --log-opts="-m $range"
else
  root="$PWD"
  export_dir="$(mktemp -d)"
  git archive --format=tar HEAD | tar -x -C "$export_dir"
  # Scan from INSIDE the export root so reported paths are repo-relative.
  ( cd "$export_dir" && gitleaks dir . --config "$root/$config" --redact --no-banner --verbose )
fi
