#!/usr/bin/env bash
# Disposable regression for admin bypass-cookie issuance; no DEV/PROD requests.
set -euo pipefail
export MSYS_NO_PATHCONV=1
target=isolated
if [[ "${1:-}" != --target || "${2:-}" != isolated || $# != 2 ]]; then
    echo "Usage: $0 --target isolated" >&2; exit 2
fi
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
start_stub "$backend" --network-alias backend --network-alias fgt_dashboard --network-alias meetup --network-alias member-card
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
admin=admin.fgctrollhattan.se
no_bypass_cookie() {
    if tr -d '\r' < "$scratch/headers" | grep -Eiq '^Set-Cookie: fgc_admin_auth='; then
        echo 'Unverified Basic credentials must never mint an admin bypass cookie' >&2; exit 1
    fi
}
for path in / /_dash-layout; do
    request 401 "$admin" "$path" GET --user synthetic:wrong-password
    no_bypass_cookie
    check_header WWW-Authenticate '.+'
done
# These endpoints are open in the hotfix base; even a 200 must not mint the cookie.
for path in /auth/login /auth/callback /auth/callback/finalize /auth/logout /auth/me /auth/select-event /auth/select-event/child /api/events/stream; do
    request 200 "$admin" "$path" GET --user synthetic:wrong-password
    no_bypass_cookie
done
request 200 "$admin" / GET --user synthetic:synthetic-password
check_header Set-Cookie 'fgc_admin_auth=synthetic-cookie;.*'
request 200 "$admin" / GET --cookie fgc_admin_auth=synthetic-cookie
request 401 "$admin" / GET --cookie fgc_admin_auth=wrong-cookie
no_bypass_cookie
echo 'PASS standalone cookie hotfix: wrong Basic and open OAuth/SSE cannot issue bypass cookies; valid Basic/cookie still work'
