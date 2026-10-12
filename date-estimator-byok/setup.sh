#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
echo "Setup complete. Run python3 run.py or open Start.command."
