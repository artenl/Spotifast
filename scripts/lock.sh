#!/usr/bin/env bash
# Regenerate requirements.lock (pinned + hashed, for x86_64 and arm64 Linux).
set -euo pipefail
cd "$(dirname "$0")/.."
uv pip compile requirements.in --universal --python-version 3.12 \
  --generate-hashes --no-header -o requirements.lock
