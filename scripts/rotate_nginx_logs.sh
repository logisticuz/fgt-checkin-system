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
reopened=0
cleanup() {
    if ((!reopened)); then
        for file in "${renamed[@]}"; do
            if [[ ! -e "$file" && -f "$file.$stamp" ]]; then mv -- "$file.$stamp" "$file"; fi
        done
    fi
    rmdir "$lock"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for name in access error; do
    file="$log_dir/$name.log"
    [[ ! -L "$file" ]] || { echo "Refusing log symlink: $file" >&2; exit 1; }
    [[ -f "$file" ]] || continue
    size=$(wc -c < "$file")
    if ((force || size >= max_bytes)); then
        if ((${#renamed[@]} == 0)); then
            [[ $(docker inspect --format '{{.State.Running}}' "$container") == true ]] || { echo 'nginx container is not running' >&2; exit 1; }
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
if ((${#renamed[@]})); then
    if ! docker exec "$container" nginx -s reopen; then
        # Keep both files if the master may already have opened the new one.
        for file in "${renamed[@]}"; do
            if [[ ! -e "$file" ]]; then mv -- "$file.$stamp" "$file"; fi
        done
        echo 'Reopen failed; retained archives and active files; retry reopen after resolving the failure' >&2
        exit 1
    fi
    reopened=1
    for file in "${renamed[@]}"; do
        for ((attempt=0; attempt<50; attempt++)); do
            [[ -f "$file" ]] && break
            sleep 0.1
        done
        [[ -f "$file" ]] || { echo "nginx did not recreate $file; archives retained" >&2; exit 1; }
        chmod 640 "$file" "$file.$stamp"
        # Expire from the first retained request, not from a later rename date.
        # With weekly rotation this avoids retaining the oldest rows for 21 days.
        first_time=$(awk -F '"' '/^\{"time":"/ {print $4; exit}' "$file.$stamp")
        if [[ -n "$first_time" ]] && date -d "$first_time" >/dev/null 2>&1; then
            touch -d "$first_time" "$file.$stamp"
        fi
    done
fi

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
