#!/usr/bin/env bash
# Start the FinSight dashboard. Run from the repository root.
set -euo pipefail
cd "$(dirname "$0")/../frontend"
exec npm run dev
