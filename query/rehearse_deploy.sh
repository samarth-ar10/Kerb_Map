#!/usr/bin/env bash
# Deploy rehearsal: build deploy/app, install ONLY its requirements in a fresh venv, run `python main.py`
# from inside deploy/app on PORT=8080 behind a local /app prefix proxy, and check the pages and API.
# Touches nothing shared: local SQLite in a temp folder, TEST rows only, test-only embedder, no cluster.
#
#   bash query/rehearse_deploy.sh
# Options (env): PYTHON (default python3, else python), APP_PORT (8080), PROXY_PORT (8090),
#   REHEARSAL_BACKEND (vastdb|local, which requirements to install; the run itself is always local),
#   PIP_WHEEL (path to a pip .whl, used when the system python has no ensurepip, e.g. the lab VM),
#   and pip's own PIP_FIND_LINKS / PIP_NO_INDEX for offline wheels.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
APP_PORT="${APP_PORT:-8080}"
PROXY_PORT="${PROXY_PORT:-8090}"
PY="${PYTHON:-$(command -v python3 >/dev/null 2>&1 && echo python3 || echo python)}"
BASE="http://127.0.0.1:${PROXY_PORT}/app"
FAILS=0
PIDS=()

cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2>/dev/null; done; sleep 1; rm -rf "$WORK" 2>/dev/null; }
trap cleanup EXIT
fail() { echo "FAIL $*"; FAILS=$((FAILS + 1)); }
ok() { echo "ok   $*"; }

echo "== build"
"$PY" "$REPO/query/build_deploy.py" --backend "${REHEARSAL_BACKEND:-vastdb}" || { echo "FAIL: build_deploy"; exit 1; }

echo "== fresh venv + deploy/app/requirements.txt only"
if "$PY" -m venv "$WORK/venv" >/dev/null 2>&1; then :; else
  rm -rf "$WORK/venv"
  "$PY" -m venv --without-pip "$WORK/venv" || { echo "FAIL: venv"; exit 1; }
fi
VPY="$WORK/venv/bin/python"; [ -x "$VPY" ] || VPY="$WORK/venv/Scripts/python.exe"
if ! "$VPY" -m pip --version >/dev/null 2>&1; then
  [ -n "${PIP_WHEEL:-}" ] || { echo "FAIL: venv has no pip; set PIP_WHEEL=/path/to/pip-*.whl"; exit 1; }
  "$VPY" "$PIP_WHEEL/pip" install -q pip || { echo "FAIL: pip bootstrap"; exit 1; }
fi
"$VPY" -m pip install -q --disable-pip-version-check -r "$REPO/deploy/app/requirements.txt" \
  || { echo "FAIL: pip install -r deploy/app/requirements.txt"; exit 1; }

echo "== seed TEST rows (local SQLite in $WORK)"
export PYTHONDONTWRITEBYTECODE=1 HOME="$WORK" USERPROFILE="$WORK" KERB_DB=local KERB_SQLITE="$WORK/kerb.sqlite" KERB_EMBEDDER=hash \
       KERB_JSONL_DIRS="$WORK/no-jsonl" KERB_LABEL_NOTES="$WORK/notes.jsonl" PORT="$APP_PORT"
(cd "$REPO" && "$VPY" -m query.seed_local) || { echo "FAIL: seed"; exit 1; }

echo "== start app (python main.py in deploy/app) and /app proxy"
(cd "$REPO/deploy/app" && exec "$VPY" main.py) >"$WORK/app.log" 2>&1 &
PIDS+=($!)
"$VPY" "$REPO/query/prefix_proxy.py" --listen "$PROXY_PORT" --target "$APP_PORT" >"$WORK/proxy.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 60); do curl -sf "$BASE/health" >/dev/null 2>&1 && break; sleep 1; done

check() {  # check NAME URL [substring] [method] [json-body]
  local name="$1" url="$2" want="${3:-}" method="${4:-GET}" body="${5:-}" code
  if [ "$method" = POST ]; then
    code=$(curl -s -o "$WORK/out" -w "%{http_code}" -X POST -H "Content-Type: application/json" -d "$body" "$url")
  else
    code=$(curl -s -o "$WORK/out" -w "%{http_code}" "$url")
  fi
  if [ "$code" != 200 ]; then fail "$name -> HTTP $code"; return; fi
  if [ -n "$want" ] && ! grep -qF -- "$want" "$WORK/out"; then fail "$name -> missing '$want'"; return; fi
  ok "$name"
}

echo "== checks through $BASE"
check "health"            "$BASE/health" '"ok":true'
check "home page /app/"   "$BASE/" 'Find parking'
check "find.html"         "$BASE/find.html" 'id="map"'
check "find.js"           "$BASE/find.js" 'feedback'
check "setup.html"        "$BASE/setup.html" 'Your drives'
check "explore.html"      "$BASE/explore.html" 'id="map"'
check "index.html"        "$BASE/index.html" 'api.js'
check "label page"        "$BASE/label" 'label.js'
check "label.html"        "$BASE/label.html" 'label.js'
check "board.html"        "$BASE/board.html" 'api.js'
check "measured.html"     "$BASE/measured.html" 'api.js'
check "api.js"            "$BASE/api.js" 'api/batches/claim'
check "app.js"            "$BASE/app.js" 'replay'
check "label.js"          "$BASE/label.js" 'claimBatch'
check "style.css"         "$BASE/style.css" '#map'
check "favicon.svg"       "$BASE/favicon.svg" 'kerb-b'
check "favicon.png"       "$BASE/favicon.png" ''
check "api/health"        "$BASE/api/health" '"backend":"local"'
check "api/spaces pin"    "$BASE/api/spaces?lat=43.6829&lon=-79.4204&radius_m=200" 'TEST_001'
check "api/spaces state"  "$BASE/api/spaces?state=free" '"count":2'
check "api/receipt"       "$BASE/api/receipt/TEST_002" '"evidence_start"'
check "api/search"        "$BASE/api/search?q=driveway" 'TEST_003'
check "api/overrule"      "$BASE/api/overrule" '"after":{"state":"occupied"}' POST                           '{"space_id":"TEST_004","new_state":"occupied","by":"rehearsal","note":"TEST"}'
check "api/feedback"      "$BASE/api/feedback" '"receipt_changed":false' POST '{"space_id":"TEST_001","vote":"down","reason":"taken","note":"TEST"}'
check "api/batches/claim" "$BASE/api/batches/claim" '"reasons":["driveway","junction","crossing","hydrant","bus_stop","travel_lane","too_short","other"]' POST '{"labeller":"rehearsal"}'
check "api/labels"        "$BASE/api/labels" '"label_id"' POST '{"item_id":"TEST_001","labeller":"rehearsal","label":"not_space","reason":"bus_stop","ms_taken":900}'
check "api/board"         "$BASE/api/board" '"labeller":"rehearsal"'
check "api/labels/summary" "$BASE/api/labels/summary" '"by_labeller"'
check "api/labels/export" "$BASE/api/labels/export?split=dev" '"frozen_sha256"'
check "api/replay/drives" "$BASE/api/replay/drives" '"drive_id"'
check "api/replay/status" "$BASE/api/replay/status" '"active":false'
check "api/spaces as_of"  "$BASE/api/spaces?as_of=0" '"mode":"replay"'
check "api/upload/samples" "$BASE/api/upload/samples" '"sample_id":"2"'
check "api/spaces job_id" "$BASE/api/spaces?job_id=0123456789" '"count":0'
code=$(curl -s -o /dev/null -w "%{http_code}" -F "sample_id=nope" -F "file=@$REPO/query/samples_gps.json" "$BASE/api/upload")
[ "$code" = 400 ] && ok "api/upload rejects an unknown sample (400)" || fail "api/upload unknown sample returned $code"
for path in api/upload/0123456789 api/measured preview-data.json api/live api/coverage api/geocode?q=x; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/$path")
  [ "$code" = 404 ] && ok "$path -> 404 (not shipped / not built yet)" || fail "$path returned $code, expected 404"
done
check "evidence frame"    "$BASE/api/evidence?uri=$("$VPY" -c 'import os,urllib.parse;print(urllib.parse.quote(os.path.join(os.path.expanduser("~"),"kerb-data","test_evidence","TEST_frame.svg")))')" 'TEST FRAME'

# Every asset the pages reference must load when resolved relative to /app/.
for page in "" setup.html find.html explore.html label board.html measured.html; do
  curl -s "$BASE/$page" -o "$WORK/page.html"
  refs=$("$VPY" - "$WORK/page.html" <<'EOF'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
for ref in re.findall(r'(?:src|href)="([^"#]+)"', html):
    if not ref.startswith(("http://", "https://", "data:")):
        print(ref)
EOF
)
  for ref in $refs; do
    url=$("$VPY" -c "import sys,urllib.parse;print(urllib.parse.urljoin(sys.argv[1], sys.argv[2]))" "$BASE/$page" "$ref")
    case "$url" in "$BASE"*) ;; *) fail "page '/app/$page' ref '$ref' resolves outside /app ($url)"; continue;; esac
    code=$(curl -s -o /dev/null -w "%{http_code}" "$url")
    [ "$code" = 200 ] && ok "asset $ref from /app/$page" || fail "asset $ref from /app/$page -> HTTP $code"
  done
done
# The bare /app URL must send the browser to /app/ before relative assets load.
curl -s "$BASE" | grep -q 'location.replace(location.pathname + "/"' && ok "/app redirects to /app/" || fail "/app has no trailing-slash redirect"
# Absolute URLs would miss the app: the host root is not ours.
code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PROXY_PORT}/api/spaces")
[ "$code" = 404 ] && ok "root /api is outside the app (404)" || fail "root /api returned $code"

if [ "$FAILS" -eq 0 ]; then
  echo "PASS: deploy rehearsal ($(ls "$REPO/deploy/app" | wc -l | tr -d ' ') files, python main.py on :$APP_PORT behind /app)"
else
  echo "--- app log"; tail -20 "$WORK/app.log"
  echo "FAIL: deploy rehearsal, $FAILS check(s) failed"
  exit 1
fi
