#!/bin/bash

# Virtual display for headed browser login
Xvfb :99 -screen 0 1280x800x24 -ac &
sleep 1

# VNC server on the virtual display
x11vnc -display :99 -forever -nopw -quiet -localhost &
sleep 1

# noVNC WebSocket proxy (serves VNC over browser on port 6080)
websockify --web=/usr/share/novnc 6080 localhost:5900 &
echo "[start] noVNC ready on port 6080"

# FastAPI app
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
