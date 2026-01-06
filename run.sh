#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Missing .venv. Run ./install.sh first."
  exit 1
fi

source .venv/bin/activate
exec python app.py
