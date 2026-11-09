#!/bin/bash
set -eu
cd -- "$(dirname -- "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3.11+。Install Python 3.11+ from python.org.'
  read -r -p '按回车关闭 / Press Return to close.' ignored
  exit 1
fi
exec python3 app.py
