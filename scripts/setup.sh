#!/usr/bin/env bash
# One-shot setup and run for Linux/macOS.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "creating .env from .env.example — fill in BOT_TOKEN before rerunning" >&2
  cp .env.example .env
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

command -v ffmpeg >/dev/null || echo "warning: FFmpeg not found — merging/converting/splitting will be unavailable" >&2

echo "== doctor =="
grabber doctor
echo
echo "run 'grabber run' to start the bot"
