#!/usr/bin/env bash
# One-shot verification for the vector-clock audit service.
#
#   1. validate the Compose file
#   2. run gofmt/vet/unit tests in Docker (Dockerfile "test" stage)
#   3. build the production image (build check)
#   4. start the stack and wait until /healthz reports healthy
#   5. run API smoke tests (valid batch, determinism, every rejection class)
#
# Exit code 0 means every step passed; any failure exits non-zero.
set -euo pipefail
cd "$(dirname "$0")"

APP_PORT="${APP_PORT:-8080}"
export APP_PORT
BASE="http://127.0.0.1:${APP_PORT}"
TMP="$(mktemp -d)"

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  echo "FAIL: docker compose (v2) or docker-compose is required" >&2
  exit 1
fi

cleanup() {
  $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

step() { printf '\n== %s ==\n' "$1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

post() { # $1: body file -> sets CODE and BODY
  CODE="$(curl -sS -o "$TMP/resp" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' --data-binary "@$1" \
    "$BASE/api/events/audit")"
  BODY="$(cat "$TMP/resp")"
}

check() { # $1: expected HTTP status, $2: required substring, $3: label
  if [ "$CODE" != "$1" ]; then
    fail "$3: expected HTTP $1, got $CODE: $BODY"
  fi
  if ! printf '%s' "$BODY" | grep -qF "$2"; then
    fail "$3: response missing '$2': $BODY"
  fi
  echo "ok - $3"
}

step "1/5 compose config check"
$COMPOSE config -q || fail "docker-compose.yml is invalid"
echo "ok - compose file valid"

step "2/5 unit tests (gofmt, go vet, go test in Docker)"
docker build --target test -t vector-clock-audit-test . || fail "unit tests / static checks failed"
echo "ok - tests passed"

step "3/5 build check"
docker build -t vector-clock-audit . || fail "docker build failed"
echo "ok - image built"

step "4/5 start service and wait for healthy"
$COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
$COMPOSE up -d --build >/dev/null || fail "compose up failed"
healthy=""
for _ in $(seq 1 60); do
  cid="$($COMPOSE ps -q app 2>/dev/null || true)"
  if [ -n "$cid" ]; then
    status="$(docker inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
      healthy=1
      break
    fi
  fi
  sleep 1
done
[ -n "$healthy" ] || fail "service did not become healthy within 60s"
CODE="$(curl -sS -o "$TMP/resp" -w '%{http_code}' "$BASE/healthz")"
BODY="$(cat "$TMP/resp")"
check 200 '"status":"ok"' "GET /healthz"

step "5/5 API smoke tests"

cat > "$TMP/valid.json" <<'JSON'
{
  "participants": ["alpha", "beta"],
  "events": [
    {"id":"a1","participant":"alpha","seq":1,"clock":{"alpha":1,"beta":0}},
    {"id":"b1","participant":"beta","seq":1,"clock":{"alpha":0,"beta":1}},
    {"id":"a2","participant":"alpha","seq":2,"clock":{"alpha":2,"beta":1}},
    {"id":"b2","participant":"beta","seq":2,"clock":{"alpha":2,"beta":2}},
    {"id":"a3","participant":"alpha","seq":3,"clock":{"alpha":3,"beta":2}}
  ]
}
JSON

cat > "$TMP/valid_shuffled.json" <<'JSON'
{
  "participants": ["beta", "alpha"],
  "events": [
    {"id":"a3","participant":"alpha","seq":3,"clock":{"beta":2,"alpha":3}},
    {"id":"b2","participant":"beta","seq":2,"clock":{"beta":2,"alpha":2}},
    {"id":"a2","participant":"alpha","seq":2,"clock":{"beta":1,"alpha":2}},
    {"id":"b1","participant":"beta","seq":1,"clock":{"beta":1,"alpha":0}},
    {"id":"a1","participant":"alpha","seq":1,"clock":{"beta":0,"alpha":1}}
  ]
}
JSON

cat > "$TMP/regression.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":1}},
  {"id":"b1","participant":"b","seq":1,"clock":{"a":0,"b":1}},
  {"id":"a2","participant":"a","seq":2,"clock":{"a":2,"b":0}}]}
JSON

cat > "$TMP/future.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":5}},
  {"id":"b1","participant":"b","seq":1,"clock":{"a":0,"b":1}}]}
JSON

cat > "$TMP/gap.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":0}},
  {"id":"e3","participant":"a","seq":3,"clock":{"a":3,"b":0}}]}
JSON

cat > "$TMP/cycle.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"a1","participant":"a","seq":1,"clock":{"a":1,"b":1}},
  {"id":"b1","participant":"b","seq":1,"clock":{"a":1,"b":1}}]}
JSON

cat > "$TMP/causality.json" <<'JSON'
{"participants":["p","q","r"],"events":[
  {"id":"e1","participant":"p","seq":1,"clock":{"p":1,"q":1,"r":0}},
  {"id":"f1","participant":"q","seq":1,"clock":{"p":0,"q":1,"r":1}},
  {"id":"r1","participant":"r","seq":1,"clock":{"p":0,"q":0,"r":1}}]}
JSON

cat > "$TMP/own_mismatch.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"e1","participant":"a","seq":1,"clock":{"a":2,"b":0}}]}
JSON

cat > "$TMP/bad_clock.json" <<'JSON'
{"participants":["a","b"],"events":[
  {"id":"e1","participant":"a","seq":1,"clock":{"a":1,"b":-1}}]}
JSON

printf '{"participants": [' > "$TMP/malformed.json"

head -c 1100000 /dev/zero | tr '\0' 'x' > "$TMP/oversize.json"

post "$TMP/valid.json"
check 200 '"eventCount":5' "valid batch accepted"
printf '%s' "$BODY" | grep -qF '"layerCount":4' || fail "valid batch: layerCount != 4: $BODY"
printf '%s' "$BODY" | grep -qF '"concurrentPairs":1' || fail "valid batch: concurrentPairs != 1: $BODY"
printf '%s' "$BODY" | grep -qF '"order":["a1","b1","a2","b2","a3"]' || fail "valid batch: unexpected merge order: $BODY"
VALID_BODY="$BODY"
echo "ok - valid batch layers, concurrency count and merge order"

post "$TMP/valid_shuffled.json"
check 200 '"eventCount":5' "shuffled batch accepted"
[ "$BODY" = "$VALID_BODY" ] || fail "input permutation changed the result"
echo "ok - input permutation does not change the result"

post "$TMP/regression.json";   check 422 '"code":"CLOCK_REGRESSION"'    "clock regression rejected"
post "$TMP/future.json";       check 422 '"code":"FUTURE_REFERENCE"'    "future reference rejected"
post "$TMP/gap.json";          check 422 '"code":"SEQUENCE_GAP"'        "sequence gap rejected"
post "$TMP/cycle.json";        check 422 '"code":"CYCLE_DETECTED"'      "cyclic dependency rejected"
post "$TMP/causality.json";    check 422 '"code":"CAUSALITY_VIOLATION"' "causality violation rejected"
post "$TMP/own_mismatch.json"; check 422 '"code":"CLOCK_OWN_MISMATCH"'  "own-component mismatch rejected"
post "$TMP/bad_clock.json";    check 422 '"code":"INVALID_CLOCK"'       "out-of-range clock rejected"
post "$TMP/malformed.json";    check 400 '"code":"MALFORMED_JSON"'      "malformed JSON rejected"
post "$TMP/oversize.json";     check 413 '"code":"PAYLOAD_TOO_LARGE"'   "body over 1 MiB rejected"

CODE="$(curl -sS -o /dev/null -w '%{http_code}' "$BASE/api/events/audit")"
[ "$CODE" = "405" ] || fail "GET /api/events/audit: expected 405, got $CODE"
echo "ok - wrong method rejected with 405"

printf '\nVERIFY OK\n'
