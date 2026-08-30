#!/usr/bin/env bash
# Start the FinSight API. Run from the repository root.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/Scripts/python.exe -m uvicorn app.main:app \
  --app-dir backend --host 127.0.0.1 --port 8000 --reload
