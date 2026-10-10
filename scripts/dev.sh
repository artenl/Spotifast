#!/usr/bin/env bash
# Local development server: plain http on 127.0.0.1, data under .dev/.
set -euo pipefail
cd "$(dirname "$0")/.."
export SPOTIFAST_PUBLIC_ORIGIN="${SPOTIFAST_PUBLIC_ORIGIN:-http://127.0.0.1:8000}"
export SPOTIFAST_COOKIE_SECURE=0
export SPOTIFAST_DATA_DIR="$PWD/.dev/data"
export SPOTIFAST_WORK_DIR="$PWD/.dev/work"
export SPOTIFAST_SUNNIFY_BIN="$PWD/.venv/bin/sunnify"
export SPOTIFAST_MIN_FREE_DISK_MB=200
export PATH="$PWD/.venv/bin:$PATH"
if [[ "${1:-}" == "admin" ]]; then shift; exec spotifast-admin "$@"; fi
exec uvicorn --factory spotifast.main:create_app --host 127.0.0.1 --port 8000
