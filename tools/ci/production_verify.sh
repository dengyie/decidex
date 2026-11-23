#!/usr/bin/env bash
# DecideX production verification (CI P2-1)
# =========================================
# Verifies the public production site serves exactly the assets this
# commit ships. Extracted from the previously inlined logic in
# .github/workflows/ci.yml (Production Verify job) so it can also be
# exercised locally/against a stub server by tests/test_production_verify.py.
#
# Usage: bash tools/ci/production_verify.sh <BASE>
#   <BASE>  URL base of the deployment to verify, e.g. https://balatro.mangoqwq.com
#
# Environment overrides (defaults keep repo-relative behaviour identical to CI):
#   PRODUCTION_VERIFY_MANIFEST  path to release-assets.json
#                               (default: <repo-root>/tools/balatro_wasm/release-assets.json,
#                                repo root derived from this script's location)
#   PRODUCTION_VERIFY_APPJS     path to app.js carrying the version triple
#                               (default: <repo-root>/tools/balatro_wasm/web/app.js)
#   PRODUCTION_VERIFY_WAIT_SECONDS  bounded wait (default 0) for /play to
#                               advertise the new version before failing.
#                               CI sets this: on push the verify job races
#                               the pull-side reconciler (decidex-deploy-
#                               sync.timer fires every 15 min), and an
#                               immediate check would false-red for up to
#                               one full reconciler period.
#
# Checks performed:
#   1. /api/health reports ok:true AND wasm_available:true
#   2. Version triple (ASSET_VERSION / EXPECTED_LOVE_SIZE / EXPECTED_WASM_SIZE
#      parsed from app.js) matches what /play advertises
#   3. Content-Length of balatro.love.zip / love.wasm.bin matches the triple
#   4. sha256 of fully-downloaded balatro.love.zip / love.wasm.bin matches
#      release-assets.json entries
#
# Exits non-zero on any assertion failure; prints PASS lines on success.

set -euo pipefail

BASE="${1:-}"
if [ -z "$BASE" ]; then
  echo "usage: $0 <BASE>   (e.g. https://balatro.mangoqwq.com)" >&2
  exit 2
fi
# Trim trailing slash so "$BASE/api/health" never doubles up.
BASE="${BASE%/}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MANIFEST="${PRODUCTION_VERIFY_MANIFEST:-$REPO_ROOT/tools/balatro_wasm/release-assets.json}"
APPJS="${PRODUCTION_VERIFY_APPJS:-$REPO_ROOT/tools/balatro_wasm/web/app.js}"
WAIT_SECONDS="${PRODUCTION_VERIFY_WAIT_SECONDS:-0}"

fail() { echo "❌ $*" >&2; exit 1; }

[ -f "$MANIFEST" ] || fail "manifest not found: $MANIFEST"
[ -f "$APPJS" ] || fail "app.js not found: $APPJS"

# The version triple is the release contract: whatever this commit's
# app.js/index.html advertise is what production must serve. Parse it
# from the checked-out sources so a wrong or stale deployment fails
# here with a concrete diff.
LOCAL_VER=$(grep -oE 'ASSET_VERSION = "v[0-9]+"' "$APPJS" | grep -oE 'v[0-9]+') \
  || fail "could not parse ASSET_VERSION from $APPJS"
LOCAL_LOVE_SIZE=$(grep -oE 'EXPECTED_LOVE_SIZE = [0-9]+' "$APPJS" | grep -oE '[0-9]+') \
  || fail "could not parse EXPECTED_LOVE_SIZE from $APPJS"
LOCAL_WASM_SIZE=$(grep -oE 'EXPECTED_WASM_SIZE = [0-9]+' "$APPJS" | grep -oE '[0-9]+') \
  || fail "could not parse EXPECTED_WASM_SIZE from $APPJS"
echo "local contract: $LOCAL_VER love=$LOCAL_LOVE_SIZE wasm=$LOCAL_WASM_SIZE"

# 1. Service health (public): service up AND WASM payload actually present.
HEALTH_JSON=$(curl -fsS --max-time 30 "$BASE/api/health") \
  || fail "/api/health request failed"
echo "$HEALTH_JSON" | grep -q '"ok": *true' \
  || fail "/api/health not ok"
echo "✅ /api/health ok"
echo "$HEALTH_JSON" | grep -q '"wasm_available": *true' \
  || fail "/api/health reports wasm_available != true"
echo "✅ /api/health wasm_available=true"

# 2. /play advertises the same version. Bounded polling: the pull-side
#    reconciler deploys up to 15 min after push, so an immediate check
#    races the deployment. Poll until the advertised version matches or
#    the wait budget runs out (WAIT_SECONDS=0 -> single attempt, i.e. the
#    deterministic behaviour the tests rely on).
SERVED=""
attempt=0
deadline=$(( $(date +%s) + WAIT_SECONDS ))
while :; do
  attempt=$((attempt + 1))
  SERVED=$(curl -fsS --max-time 30 "$BASE/play" | grep -oE 'app.js\?v=[0-9]+' | head -1) \
    || SERVED=""
  EXPECTED="app.js?v=${LOCAL_VER#v}"
  if [ "$SERVED" = "$EXPECTED" ]; then
    echo "served: $SERVED (expected: $EXPECTED)"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "served: $SERVED (expected: $EXPECTED)"
    fail "/play serves $SERVED, expected $EXPECTED (waited ${WAIT_SECONDS}s, ${attempt} attempt(s))"
  fi
  echo "⏳ /play serves ${SERVED:-nothing}, expected $EXPECTED — waiting for reconciler (attempt $attempt)..."
  sleep 30
done
echo "✅ /play advertises $LOCAL_VER"

# 3. Asset sizes match this commit's expectations (HEAD, no download —
#    reconciler lag beyond 15 min shows up here).
LOVE_LEN=$(curl -fsSI --max-time 30 "$BASE/balatro.love.zip?$LOCAL_VER" \
  | tr -d '\r' | grep -i '^content-length:' | grep -oE '[0-9]+') \
  || fail "no Content-Length on balatro.love.zip HEAD"
WASM_LEN=$(curl -fsSI --max-time 30 "$BASE/love.wasm.bin?$LOCAL_VER" \
  | tr -d '\r' | grep -i '^content-length:' | grep -oE '[0-9]+') \
  || fail "no Content-Length on love.wasm.bin HEAD"
echo "served: love=$LOVE_LEN wasm=$WASM_LEN"
[ "$LOVE_LEN" = "$LOCAL_LOVE_SIZE" ] \
  || fail "love.zip is $LOVE_LEN, expected $LOCAL_LOVE_SIZE"
[ "$WASM_LEN" = "$LOCAL_WASM_SIZE" ] \
  || fail "love.wasm.bin is $WASM_LEN, expected $LOCAL_WASM_SIZE"
echo "✅ asset sizes match (reconciler has caught up)"

# 4. Full-download sha256 against release-assets.json. Content-Length can
#    only prove size, not content; this catches a corrupted or swapped
#    payload of the right length. Downloaded to a temp file with --retry +
#    resume: large single-stream downloads from the edge can die mid-body
#    (curl 18 observed repeatedly from residential networks), and a
#    truncated stream piped straight into sha256sum would silently hash a
#    partial payload into a plausible-looking mismatch.
TMPDIR_V="${TMPDIR:-/tmp}"
LOVE_TMP="$TMPDIR_V/pv-love.$$"
WASM_TMP="$TMPDIR_V/pv-wasm.$$"
trap 'rm -f "$LOVE_TMP" "$WASM_TMP"' EXIT
curl -fsS --max-time 300 --retry 5 --retry-all-errors -C - -o "$LOVE_TMP" \
  "$BASE/balatro.love.zip?$LOCAL_VER" \
  || fail "balatro.love.zip download failed"
curl -fsS --max-time 300 --retry 5 --retry-all-errors -C - -o "$WASM_TMP" \
  "$BASE/love.wasm.bin?$LOCAL_VER" \
  || fail "love.wasm.bin download failed"
LOVE_SHA=$(sha256sum "$LOVE_TMP" | awk '{print $1}')
WASM_SHA=$(sha256sum "$WASM_TMP" | awk '{print $1}')
EXPECT_LOVE_SHA=$(python3 - "$MANIFEST" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for entry in manifest["assets"]:
    if entry["filename"] == "balatro.love":
        print(str(entry["sha256"]).lower())
PY
) || fail "could not read balatro.love sha256 from $MANIFEST"
EXPECT_WASM_SHA=$(python3 - "$MANIFEST" <<'PY'
import json, sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
for entry in manifest["assets"]:
    if entry["filename"] == "love.wasm":
        print(str(entry["sha256"]).lower())
PY
) || fail "could not read love.wasm sha256 from $MANIFEST"
[ -n "$EXPECT_LOVE_SHA" ] || fail "release-assets.json has no balatro.love sha256 entry"
[ -n "$EXPECT_WASM_SHA" ] || fail "release-assets.json has no love.wasm sha256 entry"
echo "served: love_sha=$LOVE_SHA"
echo "served: wasm_sha=$WASM_SHA"
[ "$LOVE_SHA" = "$EXPECT_LOVE_SHA" ] \
  || fail "balatro.love.zip sha256 $LOVE_SHA != manifest $EXPECT_LOVE_SHA"
[ "$WASM_SHA" = "$EXPECT_WASM_SHA" ] \
  || fail "love.wasm.bin sha256 $WASM_SHA != manifest $EXPECT_WASM_SHA"
echo "✅ asset sha256 match release-assets.json"

echo "✅ DecideX production verification completed successfully!"
