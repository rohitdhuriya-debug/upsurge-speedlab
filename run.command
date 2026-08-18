#!/bin/bash
# UPSURGE SpeedLab - macOS/Linux launcher. Same app, same port as run.bat.
cd "$(dirname "$0")"

PY=python3
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

"$PY" -c "import fastapi, uvicorn, multipart" 2>/dev/null || {
  echo "Installing dependencies..."
  "$PY" -m pip install -r requirements.txt
}

( sleep 2; command -v open >/dev/null && open http://localhost:5070 || xdg-open http://localhost:5070 ) &
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 5070
