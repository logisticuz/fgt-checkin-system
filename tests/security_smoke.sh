#!/usr/bin/env bash
# --target dev: GET-only checks on live-capable DEV; no provider/write requests.
# --target isolated: full routing checks against disposable, network-local stubs.
set -euo pipefail
export MSYS_NO_PATHCONV=1

target=""
while (($#)); do
    case "$1" in
        --target) target="${2:?--target needs dev or isolated}"; shift 2 ;;
        *) echo "Usage: $0 --target dev|isolated|http://127.0.0.1:18088" >&2; exit 2 ;;
    esac
done
case "$target" in
    dev) dev_base=http://127.0.0.1:8088 ;;
    http://127.0.0.1:8088|http://127.0.0.1:18088) dev_base=$target; target=dev ;;
    isolated) ;;
    *) echo "Specify isolated, dev, or the conductor's loopback shadow URL" >&2; exit 2 ;;
esac
root=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d "$root/tests/.security-smoke.XXXXXX")
containers=()
network=""
cleanup() {
    for container in "${containers[@]}"; do docker rm -f "$container" >/dev/null 2>&1 || true; done
    if [[ -n "$network" ]]; then docker network rm "$network" >/dev/null 2>&1 || true; fi
    # mktemp is constrained to this worktree's tests directory.
    case "$scratch" in "$root"/tests/.security-smoke.*) rm -rf -- "$scratch" ;; esac
}
trap cleanup EXIT

native_path() {
    if command -v cygpath >/dev/null; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
check_header() {
    tr -d '\r' < "$scratch/headers" | grep -Eiq "^$1: $2$" || {
        echo "Missing header $1: $2" >&2; cat "$scratch/headers" >&2; exit 1;
    }
}
security_headers() {
    check_header X-Content-Type-Options nosniff
    check_header Referrer-Policy same-origin
    check_header X-Frame-Options DENY
    check_header Content-Security-Policy-Report-Only '.+'
    if [[ "$target" == isolated ]]; then check_header Strict-Transport-Security 'max-age=31536000'; fi
}
request() {
    local expected=$1 host=$2 path=$3 method=${4:-GET}
    shift 4
    if [[ "$target" == dev && "$method" != GET ]]; then echo "DEV smoke forbids writes" >&2; exit 1; fi
    status=$(curl --noproxy '*' --path-as-is -ksS --connect-timeout 3 --max-time 8 \
        -D "$(native_path "$scratch/headers")" -o "$(native_path "$scratch/body")" -w '%{http_code}' \
        -H "Host: $host" -X "$method" "$@" "$base$path")
    [[ "$status" == "$expected" ]] || {
        echo "$target $method $host$path: expected $expected, got $status" >&2
        cat "$scratch/body" >&2; exit 1;
    }
}
denied=(/api/checkin/begin /api/integration/result /api/checkin/synthetic/member-status
    /api/notify/update /n8n/webhook /n8n/webhook/callback /docs /redoc /openapi.json
    /api/ebas/validate /api/player/member /api/player/games/extra
    /api/membership/internal /api/checkin/orchestrate/extra /auth/select-event /unknown
    /static/../api/checkin/begin //api/checkin/begin
    /api/checkin/x%2Fmember-status/status /auth/select%2Devent /auth/x/../select-event)

if [[ "$target" == dev ]]; then
    base=$dev_base
    for path in "${denied[@]}"; do request 404 localhost "$path" GET; security_headers; done
    for path in / /kiosk /register /register.html /health /admin/; do
        request 200 localhost "$path" GET; security_headers
    done
    # DEV retains application OAuth only; no real session or Basic credentials.
    for path in /_dash-layout /admin/auth/select-event; do
        status=$(curl --noproxy '*' -sS --connect-timeout 3 --max-time 8 \
            -D "$(native_path "$scratch/headers")" -o "$(native_path "$scratch/body")" -w '%{http_code}' "$base$path")
        if [[ "$status" == 302 ]]; then check_header Location /admin/auth/login
        elif [[ "$status" != 200 ]]; then echo "DEV $path: expected 200 or OAuth 302, got $status" >&2; exit 1; fi
        security_headers
    done
    # Root SSE is an explicitly approved DEV-only Dash exception; never send its token.
    request 401 localhost /api/events/stream GET
    if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Strict-Transport-Security:'; then
        echo 'DEV HTTP must not advertise HSTS' >&2; exit 1
    fi
    echo "PASS DEV $base: GET-only deny checks, public pages, admin and Dash layout"
    exit 0
fi

# The full-flow target cannot be redirected to DEV or PROD.
base=https://127.0.0.1:18443
network="build2-t044-smoke-$$-$RANDOM"
backend="$network-backend"
gateway="$network-nginx"
meetup="$network-meetup"
membercard="$network-membercard"
dev_gateway="$network-dev-nginx"
# An ordinary private bridge is needed for published loopback ports on Docker Desktop.
# Every upstream is our stub; no application env files or provider credentials are loaded.
docker network create "$network" >/dev/null
cat > "$scratch/upstream.py" <<'PY'
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"synthetic_stub": True, "path": self.path, "port": self.server.server_port}).encode()
        self.send_response(429 if self.path.endswith("?backend_rate=1") else 200)
        if self.path.endswith("?backend_rate=1"):
            self.send_header("Retry-After", "7")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_PATCH = do_GET

    def log_message(self, *args):
        pass


ThreadingHTTPServer.request_queue_size = 256
for port in (8000, 8002, 8050):
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", port), Stub).serve_forever, daemon=True).start()
threading.Event().wait()
PY
: > "$scratch/openssl.cnf"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=synthetic.invalid \
    -config "$(native_path "$scratch/openssl.cnf")" \
    -keyout "$(native_path "$scratch/key.pem")" -out "$(native_path "$scratch/cert.pem")" \
    > "$scratch/openssl.log" 2>&1 || { cat "$scratch/openssl.log" >&2; exit 1; }
for host in checkin meetup membercard; do
    mkdir -p "$scratch/certs/live/$host.fgctrollhattan.se"
    cp "$scratch/key.pem" "$scratch/certs/live/$host.fgctrollhattan.se/privkey.pem"
    cp "$scratch/cert.pem" "$scratch/certs/live/$host.fgctrollhattan.se/fullchain.pem"
done
printf 'synthetic:%s\n' "$(openssl passwd -apr1 synthetic-password)" > "$scratch/htpasswd"
start_stub() {
    local name=$1; shift
    containers+=("$name")
    docker run -d --name "$name" --network "$network" "$@" \
        --mount "type=bind,src=$(native_path "$scratch/upstream.py"),dst=/upstream.py,readonly" \
        python:3.11.10-slim-bookworm python -B /upstream.py >/dev/null
}
start_stub "$backend" --network-alias backend --network-alias fgt_dashboard
mkdir -p "$scratch/logs"
containers+=("$gateway")
start_gateway() {
    docker run -d --name "$gateway" --network "$network" -p 127.0.0.1:18443:443 \
        -e ADMIN_AUTH_COOKIE_TOKEN=synthetic-cookie \
        --mount "type=bind,src=$(native_path "$root/nginx.conf"),dst=/etc/nginx/templates/default.conf.template,readonly" \
        --mount "type=bind,src=$(native_path "$scratch/certs"),dst=/etc/letsencrypt,readonly" \
        --mount "type=bind,src=$(native_path "$scratch/htpasswd"),dst=/etc/nginx/.htpasswd,readonly" \
        --mount "type=bind,src=$(native_path "$scratch/logs"),dst=/var/log/nginx/fgt" \
        nginx:1.29.4@sha256:c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec >/dev/null
    for ((attempt=0; attempt<30; attempt++)); do
        if curl --noproxy '*' -ksSf --max-time 2 -H 'Host: www.checkin.fgctrollhattan.se' "$base/health" >/dev/null 2>&1; then return; fi
        sleep 0.2
    done
    echo 'Disposable nginx did not become ready' >&2; exit 1
}
start_gateway

containers+=("$dev_gateway")
docker run -d --name "$dev_gateway" --network "$network" \
    --mount "type=bind,src=$(native_path "$root/nginx.dev.conf"),dst=/etc/nginx/conf.d/default.conf,readonly" \
    --mount "type=bind,src=$(native_path "$scratch/logs"),dst=/var/log/nginx/fgt" \
    nginx:1.29.4@sha256:c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec >/dev/null
docker exec "$dev_gateway" nginx -t
# Official entrypoint has rendered envsubst before either syntax test or reload.
docker exec "$gateway" nginx -t
docker exec "$gateway" nginx -s reload
public=www.checkin.fgctrollhattan.se
admin=admin.fgctrollhattan.se
for host in "$public" "$admin"; do request 204 "$host" /favicon.ico GET; done
if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
    echo 'Anonymous favicon must not issue an admin bypass cookie' >&2; exit 1
fi
request 204 "$admin" /favicon.ico GET --user synthetic:wrong-password \
    --cookie-jar "$(native_path "$scratch/favicon-cookies")"
if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
    echo 'Favicon must not mint an admin cookie from invalid Basic headers' >&2; exit 1
fi
request 401 "$admin" / GET --cookie "$(native_path "$scratch/favicon-cookies")"
for path in "${denied[@]}" /api/events/stream /admin/ /_dash-layout /assets/style.css; do
    request 404 "$public" "$path" GET; security_headers
done
for path in / /kiosk /register /register.html /health /status/Synthetic /static/js/membership_v2.js \
    /api/participant/Synthetic/status '/api/checkin/synthetic/status?t=synthetic' \
    /api/membership/synthetic/status /member/confirm/synthetic; do
    request 200 "$public" "$path" GET; security_headers
done
# The routing union includes legacy registration and ordinary check-in for off mode.
# Backend MEMBERSHIP_FLOW enforcement is covered by the application tests, not these stubs.
for path in /api/checkin/orchestrate /api/ebas/register /api/membership/start /api/membership/verify /api/membership/resend; do
    request 200 "$public" "$path" POST -H 'Content-Type: application/json' --data '{}'
done
request 429 "$public" '/api/membership/synthetic/status?backend_rate=1' GET
check_header Retry-After 7
[[ $(tr -d '\r' < "$scratch/headers" | grep -ic '^Retry-After:') == 1 ]]
request 200 "$public" /api/player/games PATCH -H 'Content-Type: application/json' --data '{}'
for path in / /auth/select-event /auth/select-event/child /auth/select-event-extra /auth/select%2Devent /auth/x/../select-event; do
    request 401 "$admin" "$path" GET; security_headers; check_header WWW-Authenticate '.+'
done
for path in /auth/select-event /auth/select%2Devent /auth/x/../select-event; do
    request 401 "$admin" "$path" GET --user synthetic:wrong-password
    if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
        echo 'Failed Basic authentication must not issue an admin bypass cookie' >&2; exit 1
    fi
done
request 200 "$admin" /auth/select-event GET --user synthetic:synthetic-password
security_headers
check_header Set-Cookie 'fgc_admin_auth=synthetic-cookie;.*'
request 200 "$admin" /auth/select-event GET --cookie fgc_admin_auth=synthetic-cookie
request 401 "$admin" / GET --cookie fgc_admin_auth=wrong-cookie
for path in /auth/login /auth/callback /auth/callback/finalize /auth/logout /auth/me; do
    request 200 "$admin" "$path" GET --user synthetic:wrong-password
    if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
        echo 'Open OAuth routes must not mint an admin cookie from unverified Basic headers' >&2; exit 1
    fi
done
request 200 "$admin" /api/events/stream GET --user synthetic:wrong-password
if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
    echo 'Token-authenticated SSE must not mint an admin cookie from Basic headers' >&2; exit 1
fi
request 200 "$admin" /_dash-layout GET --user synthetic:synthetic-password
# Missing optional DNS records must not prevent check-in, syntax tests or reloads.
request 502 meetup.fgctrollhattan.se / GET
request 502 membercard.fgctrollhattan.se / GET
request 200 "$public" /health GET
start_stub "$meetup" --network-alias meetup
start_stub "$membercard" --network-alias member-card
docker exec "$gateway" nginx -t
docker exec "$gateway" nginx -s reload
sleep 0.5
request 200 meetup.fgctrollhattan.se '/synthetic/child?probe=1' GET
grep -q '/synthetic/child?probe=1' "$scratch/body"
request 200 membercard.fgctrollhattan.se / GET
docker rm -f "$meetup" >/dev/null
docker exec "$gateway" nginx -t
docker exec "$gateway" nginx -s reload
sleep 0.5
request 200 membercard.fgctrollhattan.se / GET
request 200 "$public" /health GET
meetup="$network-meetup-restored"
start_stub "$meetup" --network-alias meetup
docker exec "$gateway" nginx -s reload
sleep 0.5

# One client container supplies ONE source IP for all thirty concurrent players.
# Run inside the disposable Python container so Windows curl process startup cannot
# turn the intended rush into a slow trickle. All upstreams remain synthetic stubs.
docker exec -i "$backend" python -B - "$gateway" "$dev_gateway" <<'PY'
import concurrent.futures
import http.client
import ssl
import sys
import threading
import time
from collections import Counter

host = sys.argv[1]
context = ssl._create_unverified_context()
public = "www.checkin.fgctrollhattan.se"
barrier = threading.Barrier(30)
started = time.monotonic()
assets = [
    "/static/logo.png", "/static/logo-transp.png", "/static/logo-cropped.png",
    "/static/swish-QR.png", "/static/js/validation.js",
    "/static/js/checkin_hygiene.js", "/static/js/membership_v2.js",
    "/static/logo.png?v=2", "/static/js/validation.js?v=2",
    "/static/js/checkin_hygiene.js?v=2",
]


def connect():
    return http.client.HTTPSConnection(host, 443, timeout=8, context=context)


def send(connection, path, method="GET", spoof=None, host_header=None):
    headers = {"Host": host_header or public, "Content-Type": "application/json"}
    if spoof is not None:
        headers["X-Forwarded-For"] = f"192.0.2.{spoof}"
    connection.request(method, path, body="{}" if method != "GET" else None, headers=headers)
    response = connection.getresponse()
    response.read()
    if response.status == 429:
        assert response.getheader("Retry-After") == "60"
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("X-Frame-Options") == "DENY"
    return response.status


def player(index):
    connection = connect()
    statuses = []
    barrier.wait(timeout=8)
    try:
        statuses.append(send(connection, "/kiosk"))
        statuses.extend(send(connection, path) for path in assets)
        statuses.append(send(connection, "/api/checkin/orchestrate", "POST"))
        statuses.append(send(connection, "/register"))
        statuses.append(send(connection, "/api/membership/start", "POST"))
        statuses.append(send(connection, "/api/membership/verify", "POST"))
        statuses.append(send(connection, "/api/player/games", "PATCH"))
        statuses.append(send(connection, f"/status/Synthetic{index}"))
        for tick in (0, 10, 20):
            time.sleep(max(0, started + tick - time.monotonic()))
            for path in (f"/api/checkin/id{index}/status", f"/api/participant/Synthetic{index}/status", f"/api/membership/id{index}/status"):
                statuses.append(send(connection, path))
        return statuses
    finally:
        connection.close()


with concurrent.futures.ThreadPoolExecutor(max_workers=30) as executor:
    results = list(executor.map(player, range(30)))
statuses = [status for result in results for status in result]
elapsed = time.monotonic() - started
assert len(statuses) == 780, len(statuses)
assert set(statuses) == {200}, Counter(statuses)
assert elapsed < 30, elapsed
print(f"PASS venue rush: 30 players, ONE source IP, 780 requests in {elapsed:.2f}s, ZERO 429s", flush=True)

connection = connect()
try:
    probe_started = time.monotonic()
    legacy = [send(connection, "/api/ebas/register", "POST") for _ in range(100)]
    probe_elapsed = time.monotonic() - probe_started
    assert probe_elapsed < 60
    assert set(legacy) == {200, 429}, Counter(legacy)
    # Exhausted eBas quota cannot starve a second group of thirty game saves.
    assert [send(connection, "/api/player/games", "PATCH") for _ in range(30)] == [200] * 30
    print(f"PASS probe: 100 legacy registrations in {probe_elapsed:.2f}s, {legacy.count(429)} rejections; games quota independent", flush=True)
    checkin = [send(connection, "/api/checkin/orchestrate", "POST", spoof=index) for index in range(100)]
    assert 429 in checkin, Counter(checkin)
    for path in ("/api/membership/start", "/api/membership/verify", "/api/membership/resend"):
        assert send(connection, path, "POST") == 200, path
    assert [send(connection, "/api/membership/synthetic/status") for _ in range(30)] == [200] * 30
    for path in ("/api/membership/start", "/api/membership/verify", "/api/membership/resend", "/member/confirm/synthetic"):
        limited = [send(connection, path, "POST") for _ in range(100)]
        assert 429 in limited and set(limited) <= {200, 429}, (path, Counter(limited))
    pages = [send(connection, "/kiosk") for _ in range(200)]
    assert 429 in pages, Counter(pages)
    for sibling in ("meetup.fgctrollhattan.se", "membercard.fgctrollhattan.se"):
        assert send(connection, "/", host_header=sibling) == 200, sibling
    print(f"PASS page isolation: {pages.count(429)} check-in page rejections, both siblings still 200", flush=True)
finally:
    connection.close()

dev_connection = http.client.HTTPConnection(sys.argv[2], 80, timeout=8)
try:
    assert send(dev_connection, "/favicon.ico", host_header="localhost") == 204
    pages = [send(dev_connection, "/kiosk", host_header="localhost") for _ in range(200)]
    assert 429 in pages, Counter(pages)
    callbacks = [send(dev_connection, "/_dash-update-component", "POST", host_header="localhost") for _ in range(200)]
    assert callbacks == [200] * 200, Counter(callbacks)
    print(f"PASS DEV Dash isolation: {pages.count(429)} page rejections, 200 callbacks at 200", flush=True)
finally:
    dev_connection.close()
PY
echo 'PASS isolated nginx 1.29.4: routing, headers, Basic/cookie/OAuth, sibling DNS, venue rush and 429/Retry-After'

# Restore the missing-sibling fixture after T-044's sibling isolation checks.
docker rm -f "$meetup" >/dev/null

# T-049: prove persisted rows cannot retain client details or capabilities.
request 200 "$public" '/api/participant/SyntheticPrivate/status?t=synthetic-private-query' GET \
    --cookie session=synthetic-private-cookie --user synthetic-private-user:synthetic-private-password
request 200 "$public" '/status/SyntheticPrivate?t=synthetic-private-query' GET
request 200 "$public" '/member/confirm/synthetic-private-capability?t=synthetic-private-query' GET
request 404 "$public" '/unknown/synthetic-private-capability?t=synthetic-private-query' GET
request 502 meetup.fgctrollhattan.se '/api/participant/SyntheticPrivate/status?t=synthetic-private-error-query' GET
# stderr remains useful for live debugging; none of this native context may enter files.
docker logs "$gateway" > "$scratch/docker.stdout" 2> "$scratch/docker.stderr"
grep -q 'synthetic-private-error-query' "$scratch/docker.stderr"
docker run --rm -i --network none \
    --mount "type=bind,src=$(native_path "$scratch/logs"),dst=/logs,readonly" \
    python:3.11.10-slim-bookworm python -B - <<'PY'
import json
import re
from pathlib import Path

access = Path("/logs/access.log").read_text()
errors = Path("/logs/error.log").read_text()
for text in (access, errors):
    assert "SyntheticPrivate" not in text
    assert "synthetic-private" not in text
    assert "backend_rate=" not in text
rows = [json.loads(line) for line in access.splitlines()]
error_rows = [json.loads(line) for line in errors.splitlines()]
assert rows and error_rows
for row in rows:
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.0/24", row["client_ip"]), row
    assert "?" not in row["uri"]
    assert set(row) == {"time", "client_ip", "host", "method", "uri", "route_group", "status", "bytes", "request_time", "upstream_status", "upstream_response_time", "limit_req_status"}
assert any(row["uri"] == "/api/participant/*/status" for row in rows)
assert any(row["uri"] == "/status/*" for row in rows)
assert any(row["uri"] == "/member/confirm/*" for row in rows)
assert any(row["status"] == 429 and row["limit_req_status"] == "REJECTED" for row in rows)
assert all(500 <= row["status"] < 600 for row in error_rows)
print(f"PASS persistent privacy: {len(rows)} JSON access rows and {len(error_rows)} sanitized 5xx rows")
PY
if ! bash "$root/scripts/rotate_nginx_logs.sh" --container "$gateway" --log-dir "$scratch/logs"; then
    docker logs "$gateway" 2>&1 | tail -n 16 >&2
    docker exec "$gateway" ls -la /var/log/nginx/fgt >&2
    ls -la "$scratch/logs" >&2
    exit 1
fi
request 200 "$public" '/health?t=synthetic-private-after-reopen' GET
# Recreate only this test's gateway; the same host bind must retain the files.
docker rm -f "$gateway" >/dev/null
start_gateway
docker exec "$gateway" nginx -t
docker run --rm -i --network none \
    --mount "type=bind,src=$(native_path "$scratch/logs"),dst=/logs,readonly" \
    python:3.11.10-slim-bookworm python -B - <<'PY'
import json
from pathlib import Path

logs = Path("/logs")
archives = list(logs.glob("access.log.*"))
assert len(archives) == 1
assert len([json.loads(line) for line in archives[0].read_text().splitlines()]) > 780
assert list(logs.glob("error.log.*"))
active = [json.loads(line) for line in (logs / "access.log").read_text().splitlines()]
assert len([row for row in active if row["uri"] == "/health"]) >= 2
assert "synthetic-private" not in (logs / "access.log").read_text()
print("PASS persistent rotation: archived records retained, new file receives requests after reopen and container recreation")
PY
