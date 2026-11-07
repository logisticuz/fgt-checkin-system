#!/usr/bin/env bash
# Host-side rotation: no package or restart inside the nginx image.
set -euo pipefail
export MSYS_NO_PATHCONV=1
umask 077

root=$(cd "$(dirname "$0")/.." && pwd)
log_dir="$root/logs/nginx"
container=""
max_bytes=10485760             # 10 MiB per active file, checked by the size cron.
max_archive_bytes=52428800     # 50 MiB combined archives, possibly less than 14 days.
force=1
while (($#)); do
    case "$1" in
        --container) container=${2:?}; shift 2 ;;
        --log-dir) log_dir=${2:?}; shift 2 ;;
        --max-bytes) max_bytes=${2:?}; shift 2 ;;
        --max-archive-bytes) max_archive_bytes=${2:?}; shift 2 ;;
        --if-large) force=0; shift ;;
        *) echo 'Usage: rotate_nginx_logs.sh --container NAME [--log-dir DIR] [--if-large] [--max-bytes N] [--max-archive-bytes N]' >&2; exit 2 ;;
    esac
done
[[ -n "$container" && "$container" != -* ]] || { echo 'Explicit nginx container required' >&2; exit 2; }
[[ "$max_bytes" =~ ^[1-9][0-9]*$ && "$max_archive_bytes" =~ ^[1-9][0-9]*$ ]] || { echo 'Positive byte limits required' >&2; exit 2; }
log_dir=$(cd "$log_dir" && pwd -P)
lock="$log_dir/.rotate.lock"
mkdir "$lock" 2>/dev/null || { echo "Rotation already locked: $lock" >&2; exit 1; }
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
renamed=()
pending="$log_dir/.reopen.pending"
trap 'rmdir "$lock"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

recover_reopen() {
    [[ ! -L "$pending" ]] || { echo 'Refusing pending-marker symlink' >&2; return 1; }
    pending_stamp=$(cat "$pending")
    [[ "$pending_stamp" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9]+$ ]] || { echo 'Invalid pending marker; no archives pruned' >&2; return 1; }
    [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]] || { echo 'nginx is not running; reopen still pending' >&2; return 1; }
    for name in access error; do
        file="$log_dir/$name.log"
        [[ ! -L "$file" ]] || { echo "Refusing log symlink: $file" >&2; return 1; }
        if [[ -f "$file.$pending_stamp" ]]; then : >> "$file" || return 1; fi
    done
    if ! docker exec "$container" nginx -s reopen; then
        echo 'Reopen failed; pending marker retained and ALL archive pruning disabled' >&2
        return 1
    fi
    for name in access error; do
        file="$log_dir/$name.log"
        archive="$file.$pending_stamp"
        [[ -f "$archive" ]] || continue
        chmod 640 "$file" "$archive" || return 1
        # Expiry starts at the first request, rather than a later rename date.
        first_time=$(awk -F '"' '/^\{"time":"/ {print $4; exit}' "$archive")
        if [[ -n "$first_time" ]] && date -d "$first_time" >/dev/null 2>&1; then
            touch -d "$first_time" "$archive" || return 1
        fi
    done
    rm -f -- "$pending"
}

# Recovery is mandatory even when replacement files are empty/below the threshold.
if [[ -e "$pending" || -L "$pending" ]]; then recover_reopen || exit 1; fi
for name in access error; do
    file="$log_dir/$name.log"
    [[ ! -L "$file" ]] || { echo "Refusing log symlink: $file" >&2; exit 1; }
    [[ -f "$file" ]] || continue
    size=$(wc -c < "$file")
    if ((force || size >= max_bytes)); then
        if ((${#renamed[@]} == 0)); then
            [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]] || { echo 'nginx container is not running' >&2; exit 1; }
        fi
        if ((${#renamed[@]} == 0)); then
            marker_tmp=$(mktemp "$log_dir/.reopen.pending.XXXXXX")
            printf '%s\n' "$stamp" > "$marker_tmp"
            mv -- "$marker_tmp" "$pending"
        fi
        archive="$file.$stamp"
        [[ ! -e "$archive" ]] || { echo "Archive collision: $archive" >&2; exit 1; }
        mv -- "$file" "$archive"
        renamed+=("$file")
        # Pre-create without truncation: Docker Desktop can retain a negative
        # dentry for the host-renamed name, so nginx's O_CREAT otherwise fails.
        : >> "$file"
    fi
done
if ((${#renamed[@]})); then recover_reopen || exit 1; fi
# A pending recovery must never fall through to retention or size pruning.
[[ ! -e "$pending" ]] || { echo 'Reopen remains pending; pruning disabled' >&2; exit 1; }

# Only regular, explicitly named nginx archives are eligible for deletion.
archive_files() {
    find "$log_dir" -maxdepth 1 -type f \( -name 'access.log.[0-9]*' -o -name 'error.log.[0-9]*' \) "$@"
}
while IFS= read -r -d '' file; do rm -f -- "$file"; done < <(archive_files -mtime +13 -print0)
mapfile -d '' -t archives < <(archive_files -printf '%T@ %p\0' | sort -z -n)
total=0
for record in "${archives[@]}"; do file=${record#* }; total=$((total + $(wc -c < "$file"))); done
for record in "${archives[@]}"; do
    ((total > max_archive_bytes)) || break
    file=${record#* }
    size=$(wc -c < "$file")
    rm -f -- "$file"
    total=$((total - size))
done
echo "nginx logs: rotated ${#renamed[@]} file(s); archives $total/$max_archive_bytes bytes; retention at most 14 days"
