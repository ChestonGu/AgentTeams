#!/bin/bash
# base.sh - Shared utilities for HiClaw startup scripts
# Source this file: source /opt/hiclaw/scripts/lib/base.sh

set -e

# is_utf8_name <name> — returns 0 when name is valid UTF-8, 1 otherwise.
# S3 object keys must be valid UTF-8, so names failing this check cannot be
# pushed to object storage.
is_utf8_name() {
    printf '%s' "$1" | python3 -c 'import sys; sys.stdin.buffer.read().decode("utf-8")' >/dev/null 2>&1
}

# collect_nonutf8_files <dir> — prints, one per line, the <dir>-relative paths
# of files whose names are not valid UTF-8. Use the output to build
# `mc mirror --exclude` arguments: a single bad name would otherwise fail the
# whole push (mc mirror aborts on the first invalid S3 key).
collect_nonutf8_files() {
    local dir="$1"
    find "${dir}/" -type f -print0 2>/dev/null |
        while IFS= read -r -d '' _f; do
            if ! is_utf8_name "${_f##*/}"; then
                printf '%s\n' "${_f#"${dir}"/}"
            fi
        done
}

# Wait for a TCP service to become available
# Usage: waitForService "ServiceName" "host" port [timeout_seconds]
waitForService() {
    local name="$1"
    local host="$2"
    local port="$3"
    local timeout="${4:-120}"
    local elapsed=0

    echo "[hiclaw] Waiting for ${name} at ${host}:${port}..."
    while ! curl -sf "http://${host}:${port}/" > /dev/null 2>&1 && \
          ! nc -z "${host}" "${port}" 2>/dev/null; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[hiclaw] ERROR: ${name} did not become available within ${timeout}s"
            exit 1
        fi
    done
    echo "[hiclaw] ${name} is ready (took ${elapsed}s)"
}

# Wait for an HTTP endpoint to return 200
# Usage: waitForHTTP "ServiceName" "url" [timeout_seconds]
waitForHTTP() {
    local name="$1"
    local url="$2"
    local timeout="${3:-120}"
    local elapsed=0

    echo "[hiclaw] Waiting for ${name} HTTP at ${url}..."
    while [ "$(curl -sf -o /dev/null -w '%{http_code}' "${url}" 2>/dev/null)" != "200" ]; do
        sleep 2
        elapsed=$((elapsed + 2))
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[hiclaw] ERROR: ${name} HTTP not ready within ${timeout}s"
            exit 1
        fi
    done
    echo "[hiclaw] ${name} HTTP is ready (took ${elapsed}s)"
}

# Generate a cryptographically secure random key
# Usage: generateKey [length_bytes]
generateKey() {
    local bytes="${1:-32}"
    openssl rand -hex "${bytes}"
}

# Log with timestamp
# Usage: log "message"
log() {
    echo "[hiclaw $(date '+%Y-%m-%d %H:%M:%S')] $1"
}
