#!/usr/bin/env bash
# Promote an API Printer Service release candidate from preview to stable.
#
# Contract:
#  - Takes an existing GitHub release (tag), verifies EVERY required platform
#    artifact and its SHA-256 checksum is present, then flips the release to
#    the stable latest: prerelease flag cleared, marked latest.
#  - Never rebuilds, re-signs, retags or re-uploads anything. The exact
#    artifacts and checksums tested on preview become stable.
#  - Promoting an older known-good tag by name is the rollback path.
#  - Fails closed: any missing artifact or checksum mismatch aborts before
#    the release metadata is touched.
#
# Usage: scripts/promote-release.sh <tag> [owner/repo]
set -euo pipefail

TAG="${1:?usage: promote-release.sh <tag> [owner/repo]}"
REPO="${2:-${GITHUB_REPOSITORY:-manconsultingltd/pos-api-printer-service}}"

# Required artifacts per release ring. Platforms are deliberately explicit:
# promotion must see the Linux installer AND the Windows installer.
REQUIRED_ARTIFACTS=(
  "api-printer-installer-linux"
  "api-printer-setup-windows.exe"
)

fail() { echo "PROMOTE FAILED: $*" >&2; exit 1; }

command -v gh >/dev/null || fail "gh CLI not available"

# --- the release must exist and be a prerelease candidate -------------------
STATE_JSON="$(gh release view "$TAG" --repo "$REPO" --json isPrerelease,isDraft,assets 2>/dev/null)" \
  || fail "release ${TAG} not found in ${REPO}"

IS_PRERELEASE="$(echo "$STATE_JSON" | jq -r .isPrerelease)"
IS_DRAFT="$(echo "$STATE_JSON" | jq -r .isDraft)"
[[ "$IS_DRAFT" == "false" ]] || fail "release ${TAG} is a draft"
[[ "$IS_PRERELEASE" == "true" ]] || fail "release ${TAG} is already stable (not a prerelease) — nothing to promote"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

echo "Downloading all assets of ${TAG} for verification..."
gh release download "$TAG" --repo "$REPO" --dir . || fail "asset download failed"

# --- every required artifact must exist -------------------------------------
# NOTE: a plain `matches=( $pattern )` fails open — with nullglob disabled an
# unmatched glob stays a literal string and the length check passes even when
# nothing was downloaded. nullglob keeps the check honest.
shopt -s nullglob
for pattern in "${REQUIRED_ARTIFACTS[@]}"; do
  matches=( $pattern )
  if [[ ${#matches[@]} -eq 0 || ! -e "${matches[0]}" ]]; then
    fail "required artifact missing on ${TAG}: ${pattern}"
  fi
  for match in "${matches[@]}"; do
    [[ -f "$match" ]] || fail "required artifact pattern ${pattern} matched a non-regular file: ${match}"
  done
done
shopt -u nullglob

# --- checksums must verify ---------------------------------------------------
[[ -f "SHA256SUMS.txt" ]] || fail "SHA256SUMS.txt missing"
# Every file referenced by SHA256SUMS.txt must exist and match.
sha256sum --check --strict SHA256SUMS.txt || fail "checksum verification failed"

# Cross-check: every asset on the release (except the checksum file itself)
# must be covered by SHA256SUMS.txt — no unchecksummed payload sneaks in.
EXPECTED_LIST="$(awk '{print $2}' SHA256SUMS.txt | sort)"
ACTUAL_LIST="$(find . -maxdepth 1 -type f ! -name 'SHA256SUMS.txt' -printf '%f\n' | sort)"
[[ "$EXPECTED_LIST" == "$ACTUAL_LIST" ]] || fail "asset list differs from SHA256SUMS.txt (missing or extra artifacts)

expected: ${EXPECTED_LIST//$'\n'/, }
actual:   ${ACTUAL_LIST//$'\n'/, }"

# --- the tag must describe THIS release ---------------------------------------
# The version the client parses out of the tag is exactly the version the
# installers were stamped with; a hand-retagged release would confuse the
# ring resolution. Strict grammar, same regex the service-side feed uses.
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "tag ${TAG} is not strict semver (vX.Y.Z)"
echo "Tag ${TAG} passes strict semver."

echo "All required artifacts present and checksums verified."
echo "Promoting ${TAG} to stable latest (metadata only — artifacts are untouched)..."

# --- promotion: metadata-only change ----------------------------------------
gh release edit "$TAG" --repo "$REPO" --prerelease=false --latest=true \
  || fail "gh release edit failed"

echo "Promoted ${TAG} to stable latest."

# --- postcondition: the promotion actually took effect -----------------------
# gh release view has no isLatest JSON field, so verify through the REST
# endpoint that serves the current latest stable release.
LATEST_TAG="$(gh api "repos/${REPO}/releases/latest" --jq .tag_name 2>/dev/null)" \
  || fail "postcondition check failed: could not verify the latest release (the promotion itself already succeeded — inspect ${TAG} manually)"
[[ "$LATEST_TAG" == "$TAG" ]] \
  || fail "postcondition failed: latest release is ${LATEST_TAG}, expected ${TAG} (the promotion metadata was applied — inspect the release before retrying)"
echo "Verified: ${TAG} is the stable latest."
