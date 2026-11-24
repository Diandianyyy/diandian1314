#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
git pull --ff-only origin main
python3 scripts/sync_cloud.py
git add -- Packages Packages.bz2 Packages.gz Packages.xz Packages.zst
if ! git diff --cached --quiet; then
    git commit -m "Update cloud package index $(date +%T)"
fi
git push origin main
