#!/bin/bash
# IMSLP Date Estimator — Double-click to start
cd "$(dirname "$0")"

echo "======================================"
echo "  IMSLP Date Estimator"
echo "======================================"
echo ""

if ! xcode-select -p &>/dev/null; then
    echo "Installing required tools..."
    echo "A popup will appear — click Install and wait for it to finish."
    xcode-select --install
    echo ""
    echo "After installation finishes, double-click this file again."
    read -p "Press Enter to close."
    exit 0
fi

if ! command -v python3 &>/dev/null; then
    if command -v brew &>/dev/null; then
        echo "Installing Python..."
        brew install python3
    else
        echo "Please install Python 3 from: https://www.python.org/downloads/"
        echo "Then double-click this file again."
        read -p "Press Enter to close."
        exit 1
    fi
fi

echo "Starting..."
echo "(Close this window to stop the app)"
echo ""
python3 run.py
