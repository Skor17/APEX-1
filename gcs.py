"""APEX-1 Ground Control Station — FastAPI server + live Iron Man HUD.

Receives UDP/JSON telemetry from the vehicle (the simulator now, the real
ESP32 later), keeps a rolling buffer, and pushes it to the browser over
WebSocket. Serves the HUD from web/. Hardware-agnostic: it only knows the
protocol, so swapping the data source requires no changes here.

Phase 3: before the WebSocket push (and only there), the GCS enriches each
frame with the ML landing prediction (`pred` — see protocol.py). The UDP
receive path, the buffered frame objects, the replay size, and the push
rate are all unchanged; enrichment is computed at push time on a copy. If
the model is missing, frames push un-enriched exactly as before.

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
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

import protocol as proto
import prediction

WEB_DIR = Path(__file__).parent / "web"
BUFFER_MAX = 1200
PUSH_HZ = 50.0

# Shared state, fed by the UDP receiver thread, read by the WebSocket loop.
state: dict = {"latest": None, "buffer": deque(maxlen=BUFFER_MAX), "last_rx": 0.0}

# Phase 3 predictor: loaded once in `lifespan`; None-tolerant by design.
predictor: "prediction.LandingPredictor | None" = None


def _enriched_json(frame: proto.TelemetryFrame) -> str:
    """One frame as push-time JSON, with the ML landing `pred` attached.

    Enrichment happens HERE, at push time, on a copy — the buffered frame
    objects are never mutated, so the 400-frame replay and the live push
    cannot double-stamp or leak predictions into the UDP path. `predict` is
    a pure function of the frame, so recomputing it is idempotent. PRE-LAUNCH,
    LANDED, and model-unavailable all simply serialize WITHOUT the key
    (pre-Phase-3 bytes), which is what old clients and the NDJSON recorder
    already expect.
    """
    pred = None
    if predictor is not None:
        # The predictor needs the whole same-flight buffer: its 0.5 s
        # alt/vel window comes from the last 25 frames, but the running
        # flight-apogee feature (alt_hist_max) needs EVERY frame since the
        # launch — exactly as the trainer maintains it. predict() is a pure
        # function of (frame, history), so recomputing per push is
        # idempotent; the buffer itself is never mutated.
        history = list(state["buffer"])
        try:
            pred = predictor.predict(frame, history)
        except Exception as e:  # a prediction bug must never take down the link
            print(f"[gcs] prediction error ({type(e).__name__}: {e})", file=sys.stderr)
            pred = None
    if pred is None:
        return frame.to_json()
    return replace(frame, pred=pred).to_json()


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
    global predictor
    # Phase 3: load the landing model once. A missing/corrupt model only
    # logs a warning (inside the predictor) and disables predictions — the
    # GCS itself always starts.
    t0 = time.time()
    predictor = prediction.LandingPredictor()
    if predictor.loaded:
        print(f"[gcs] landing predictor ready ({time.time() - t0:.2f}s)")
    else:
        print("[gcs] landing predictor disabled — frames push without `pred`")
    threading.Thread(target=_udp_receiver, daemon=True).start()
    yield


app = FastAPI(title="APEX-1 GCS", lifespan=lifespan)


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    # Send recent history so the HUD's charts are populated on connect.
    # Enriched at push time like the live stream (copy — buffer untouched).
    for frame in list(state["buffer"])[-400:]:
        await websocket.send_text(_enriched_json(frame))
    last_sent = state["latest"]
    interval = 1.0 / PUSH_HZ
    while True:
        try:
            latest = state["latest"]
            if latest is not last_sent:
                await websocket.send_text(_enriched_json(latest))
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


# Mission log: recorded flights (NDJSON) served at /missions/*.ndjson so the
# HUD's MISSION LOG panel also works when the GCS serves the page. Same
# relative path the HUD uses from GitHub Pages (web/missions/).
MISSIONS_DIR = Path(__file__).parent / "missions"
MISSIONS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/missions", StaticFiles(directory=MISSIONS_DIR, check_dir=False), name="missions")

# Serve the HUD from web/ as the site root: GET / -> index.html, GET /app.js,
# GET /style.css. Mounted AFTER the /ws route and /missions (and at "/") so it
# can never shadow them. The same relative layout also works on GitHub Pages,
# which serves web/ as the static site root.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="hud")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8501)
