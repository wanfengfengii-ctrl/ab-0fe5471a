#!/usr/bin/env bash
# One-shot verification for the vector-clock audit service.
#
#   ./verify.sh
#
# Pipeline: validate compose config -> build image -> start stack -> wait
# until healthy -> run unit tests, build checks and API smoke tests against
# the healthy service -> report the result via the exit code (0 = pass).
set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
export APP_PORT

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "[verify] FAIL: docker compose is not available" >&2
    exit 1
fi

step() { printf '\n[verify] === %s ===\n' "$*"; }
fail() { printf '[verify] FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() { $COMPOSE down -v >/dev/null 2>&1 || true; }
trap cleanup EXIT

cd "$(dirname "$0")"

step "1/7 compose config check"
$COMPOSE config -q || fail "docker-compose.yml is invalid"

step "2/7 image build"
$COMPOSE build || fail "docker build failed"

step "3/7 start service (host port ${APP_PORT})"
$COMPOSE up -d || fail "docker compose up failed"

step "4/7 wait for /healthz to report healthy"
container_id="$($COMPOSE ps -q audit)"
[ -n "$container_id" ] || fail "could not locate the audit container"
healthy=""
for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
    if [ "$status" = "healthy" ]; then
        healthy="yes"
        break
    fi
    sleep 1
done
[ -n "$healthy" ] || fail "service did not become healthy in time"
echo "[verify] service is healthy"

step "5/7 code tests (unit tests inside the container)"
$COMPOSE exec -T audit python -m unittest discover -s tests -v \
    || fail "unit tests failed"

step "6/7 build checks (byte-compile sources, inspect image)"
$COMPOSE exec -T audit python -m compileall -q app tests \
    || fail "source byte-compile check failed"
docker image inspect vector-clock-audit:latest >/dev/null \
    || fail "built image is missing"

step "7/7 API smoke tests"
# In-container smoke against the service loopback.
$COMPOSE exec -T audit python tests/smoke.py http://127.0.0.1:8000 \
    || fail "in-container smoke tests failed"
# Host-side smoke through the APP_PORT mapping when a client is available.
if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" | grep -q '"ok"' \
        || fail "host-side healthz check failed on port ${APP_PORT}"
    echo "[verify] host-side healthz check passed on port ${APP_PORT}"
elif command -v python3 >/dev/null 2>&1; then
    python3 - "$APP_PORT" <<'PY' || fail "host-side healthz check failed"
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=5) as r:
    assert r.status == 200 and json.load(r)["status"] == "ok"
print("[verify] host-side healthz check passed on port %s" % port)
PY
else
    echo "[verify] no curl/python3 on host; skipped host-side port check"
fi

step "VERIFY PASSED"
exit 0
