#!/usr/bin/env bash
# Construye la app de escritorio en dist/ (ver scripts/build_app.py).
set -euo pipefail
cd "$(dirname "$0")/.."
uv run python scripts/build_app.py "$@"
