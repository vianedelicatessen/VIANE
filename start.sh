#!/usr/bin/env bash
set -e
# carga .env si existe
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$DIR/.env"
  set +a
fi
cd "$DIR"
# activa venv si existe
if [ -f "$DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$DIR/.venv/bin/activate"
fi
# asegura carpeta de uploads
mkdir -p static/uploads
# arranca en background y guarda logs/pid
nohup python app.py > server.log 2>&1 & echo $! > app.pid
echo "App arrancada, PID=$(cat app.pid), logs en server.log"
