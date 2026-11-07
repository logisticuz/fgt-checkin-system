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
    status=$(curl --noproxy '*' -ksS --connect-timeout 3 --max-time 8 \
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
    /api/membership/internal /api/checkin/orchestrate/extra /auth/select-event /unknown)

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
containers+=("$gateway")
docker run -d --name "$gateway" --network "$network" -p 127.0.0.1:18443:443 \
    -e ADMIN_AUTH_COOKIE_TOKEN=synthetic-cookie \
    --mount "type=bind,src=$(native_path "$root/nginx.conf"),dst=/etc/nginx/templates/default.conf.template,readonly" \
    --mount "type=bind,src=$(native_path "$scratch/certs"),dst=/etc/letsencrypt,readonly" \
    --mount "type=bind,src=$(native_path "$scratch/htpasswd"),dst=/etc/nginx/.htpasswd,readonly" \
    nginx:1.27.5 >/dev/null
for ((attempt=0; attempt<30; attempt++)); do
    if curl --noproxy '*' -ksSf --max-time 2 -H 'Host: www.checkin.fgctrollhattan.se' "$base/health" >/dev/null 2>&1; then break; fi
    sleep 0.2
done
# Official entrypoint has rendered envsubst before either syntax test or reload.
docker exec "$gateway" nginx -t
docker exec "$gateway" nginx -s reload
public=www.checkin.fgctrollhattan.se
admin=admin.fgctrollhattan.se
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
for path in / /auth/select-event /auth/select-event/child /auth/select-event-extra; do
    request 401 "$admin" "$path" GET; security_headers; check_header WWW-Authenticate '.+'
done
request 401 "$admin" /auth/select-event GET --user synthetic:wrong-password
if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
    echo 'Failed Basic authentication must not issue an admin bypass cookie' >&2; exit 1
fi
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

# Flood only disposable stubs. Spoofed forwarding headers cannot evade the venue key.
rejected=0
for ((attempt=0; attempt<45; attempt++)); do
    status=$(curl --noproxy '*' -ksS --max-time 5 -D "$(native_path "$scratch/headers")" -o "$(native_path "$scratch/body")" -w '%{http_code}' \
        -H "Host: $public" -H "X-Forwarded-For: 192.0.2.$attempt" -X POST "$base/api/checkin/orchestrate")
    if [[ "$status" == 429 ]]; then
        rejected=$((rejected+1)); check_header Retry-After 60; security_headers
    elif [[ "$status" != 200 ]]; then echo "Unexpected flood status: $status" >&2; exit 1; fi
done
((rejected > 0)) || { echo 'Check-in flood was not rate limited' >&2; exit 1; }
request 200 "$public" /api/membership/start POST
request 200 "$public" /api/membership/verify POST
request 200 "$public" /api/membership/resend POST
for ((attempt=0; attempt<50; attempt++)); do request 200 "$public" /api/membership/synthetic/status GET; done
for path in /api/membership/start /api/membership/verify /api/membership/resend /member/confirm/synthetic /api/ebas/register; do
    limited=0
    for ((attempt=0; attempt<28; attempt++)); do
        status=$(curl --noproxy '*' -ksS --max-time 5 -D "$(native_path "$scratch/headers")" -o "$(native_path "$scratch/body")" -w '%{http_code}' \
            -H "Host: $public" -X POST "$base$path")
        if [[ "$status" == 429 ]]; then limited=$((limited+1)); check_header Retry-After 60; security_headers
        elif [[ "$status" != 200 ]]; then echo "Unexpected $path flood status: $status" >&2; exit 1; fi
    done
    ((limited > 0)) || { echo "$path flood was not rate limited" >&2; exit 1; }
done
echo "PASS isolated nginx 1.27.5: routing, headers, Basic/cookie/OAuth, sibling DNS, 429/Retry-After ($rejected check-in rejections)"
