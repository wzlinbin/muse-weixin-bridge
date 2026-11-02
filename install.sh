#!/usr/bin/env bash
# Install the Muse <-> WeChat bridge (text-only v1).
# Creates .venv, installs qrcode + pillow, prepares .secrets/.
# Credentials are NOT created here: run `bridge.py login` and scan the QR.
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet qrcode pillow
mkdir -p .secrets
chmod 700 .secrets
echo "Installed. Next steps:"
echo "  1. .venv/bin/python bridge.py login   # scan the QR in WeChat"
echo "  2. .venv/bin/python bridge.py run     # start the bridge"
