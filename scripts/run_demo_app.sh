#!/usr/bin/env bash
# Launch the interactive demo of the best pipeline (synthetic data, localhost only).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
"$PY" -c "import streamlit, plotly, networkx" 2>/dev/null || {
  echo "Demo extras missing. Install with:  pip install -e '.[demo]'   (or: uv pip install -e '.[demo]')" >&2
  exit 1
}
exec "$PY" -m streamlit run apps/demo/app.py \
  --server.address 127.0.0.1 --server.port "${PORT:-8501}" \
  --server.headless true --browser.gatherUsageStats false
