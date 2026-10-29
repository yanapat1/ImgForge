#!/usr/bin/env bash
# The whole suite: everything but the GPU tests in parallel workers (CPU only, see tests/conftest.py), then the GPU
# tests in one process, so a single CUDA context is ever open. Extra arguments go to the parallel pass.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-.venv/bin/python}
"$PY" -m pytest -q -n auto -m "not gpu" tests "$@"
"$PY" -m pytest -q -m gpu tests
