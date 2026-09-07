#!/usr/bin/env bash
# Single entry point for both suites. Stdlib unittest only — no pip.
# From repo root: ./run-tests.sh
set -euo pipefail
root=$(cd "$(dirname "$0")" && pwd)
python3 -m unittest discover -s "$root/mcp-shim/tests" -v
python3 -m unittest discover -s "$root/log-distill/tests" -v
