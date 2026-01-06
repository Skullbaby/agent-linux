#!/usr/bin/env bash
set -euo pipefail

echo "[agent-linux] Installing prerequisites..."
sudo apt-get update
sudo apt-get install -y git python3-venv python3-pip

echo "[agent-linux] Creating venv..."
python3 -m venv .venv
source .venv/bin/activate

echo "[agent-linux] Installing Python deps..."
python -m pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "OK."
echo "Next:"
echo "  cp agent.env.template agent.env"
echo "  nano agent.env"
echo "  source .venv/bin/activate && python app.py"
