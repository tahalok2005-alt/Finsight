#!/usr/bin/env bash
# Full verification: tests, a clean acceptance run, and the evaluation report.
set -euo pipefail
cd "$(dirname "$0")/../backend"
PY=../.venv/Scripts/python.exe
export PYTHONIOENCODING=utf-8

echo "== test suite =="
$PY -m pytest -q

echo
echo "== OCR engines =="
$PY cli.py engines

echo
echo "== acceptance document =="
$PY cli.py ingest \
  ../data/benchmark/Media_Release_RIL_Q1_FY2026-27_Financial_and_Operational_Performance.pdf \
  --allow-duplicate

echo
echo "== evaluation =="
DOC=$($PY cli.py list --limit 1 | tail -1 | awk '{print $1}')
$PY cli.py evaluate "$DOC"
