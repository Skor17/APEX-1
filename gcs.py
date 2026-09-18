"""APEX-1 Ground Control Station — FastAPI server + live Iron Man HUD.

Receives UDP/JSON telemetry from the vehicle (the simulator now, the real
ESP32 later), keeps a rolling buffer, and pushes it to the browser over
WebSocket. Serves the HUD from web/. Hardware-agnostic: it only knows the
protocol, so swapping the data source requires no changes here.

Run:  python gcs.py        (HUD at http://localhost:8501)
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import protocol as proto

WEB_DIR = Path(__file__).parent / "web"
BUFFER_MAX = 1200
PUSH_HZ = 50.0

# Shared state, fed by the UDP receiver thread, read by the WebSocket loop.
state: dict = {"latest": None, "buffer": deque(maxlen=BUFFER_MAX), "last_rx": 0.0}


def _udp_receiver() -> None:
    """Blocking UDP receiver (runs in a daemon thread)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", proto.PORT))
    except OSError as e:
        print(f"[gcs] cannot bind UDP port {proto.PORT}: {e}\n"
              f"[gcs] another process (an old GCS?) is holding it — stop it and restart.",
              file=sys.stderr)
        return
    s.settimeout(0.5)
    while True:
        try:
            raw, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            frame = proto.TelemetryFrame.from_json(raw)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue
        state["buffer"].append(frame)
        state["latest"] = frame
        state["last_rx"] = time.time()


@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_udp_receiver, daemon=True).start()
    yield


app = FastAPI(title="APEX-1 GCS", lifespan=lifespan)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    # Send recent history so the HUD's charts are populated on connect.
    for frame in list(state["buffer"])[-400:]:
        await websocket.send_text(frame.to_json())
    last_sent = state["latest"]
    interval = 1.0 / PUSH_HZ
    while True:
        try:
            latest = state["latest"]
            if latest is not last_sent:
                await websocket.send_text(latest.to_json())
                last_sent = latest
            await asyncio.sleep(interval)
        except WebSocketDisconnect:
            break
        except Exception as e:
            # A non-disconnect error must not kill the handler silently.
            print(f"[gcs] ws client error ({type(e).__name__}: {e}) — closing",
                  file=sys.stderr)
            try:
                await websocket.close()
            except Exception:
                pass
            break


# Serve the HUD from web/ as the site root: GET / -> index.html, GET /app.js,
# GET /style.css. Mounted AFTER the /ws route (and at "/") so it can never
# shadow it. The same relative layout also works on GitHub Pages, which serves
# web/ as the static site root.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="hud")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8501)
