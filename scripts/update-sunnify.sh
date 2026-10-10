#!/usr/bin/env bash
# Move the vendor/sunnify submodule to the latest upstream commit (still
# unmodified), showing what changed first. Commit and push the result,
# then `git pull --recurse-submodules` + rebuild on the VPS.
set -euo pipefail
cd "$(dirname "$0")/.."
git submodule update --init vendor/sunnify
cd vendor/sunnify

if ! git diff --quiet HEAD; then
  echo "vendor/sunnify has local changes; refusing (Sunnify must stay unmodified)" >&2
  exit 1
fi
before=$(git rev-parse --short HEAD)
git fetch --depth 50 origin main
echo "Changes ${before} -> $(git rev-parse --short FETCH_HEAD):"
git log --oneline "HEAD..FETCH_HEAD" || true
read -r -p "Update to this version? [y/N] " answer
[[ "${answer}" == [yY]* ]] || exit 0

git checkout --quiet FETCH_HEAD
after=$(git rev-parse --short HEAD)
cd ../..
git add vendor/sunnify
echo "Submodule now at ${after}. Next:"
echo "  git commit -m 'chore: bump Sunnify to ${after}' && git push"
