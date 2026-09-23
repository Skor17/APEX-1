# APEX-1 · Litoral Guidance

## Try the live HUD — no install needed

Open this link in any browser: **https://skor17.github.io/APEX-1/?demo=1**

It runs a simulated rocket flight in real time on an Iron Man-style cockpit
display — launch, boost, apogee, parachute, landing, and an automatic
relaunch — with no Python, no setup, no account.

- The site is hosted on GitHub Pages and **auto-deploys on every push to
  `main`**, so the link always shows the latest HUD.
- `?demo=1` forces demo (synthetic telemetry) mode. Without it, the same page
  looks for a live ground station first; after ~5 s with no live feed it
  opens on a **recorded flight (replay)** from `web/missions/`, auto-falling
  back to the synthetic demo only if that mission file is missing. When a
  real GCS *is* reachable, the HUD switches to live telemetry on its own
  (and a live feed interrupts an auto-replay on the spot).
- Want the full system? See the quick start below — `python simulator.py` +
  `python gcs.py` streams real simulated telemetry into the same HUD at
  `http://localhost:8501`.

A software-first **flight telemetry** system: a software-in-the-loop (SITL)
simulator that stands in for the on-board flight computer, and a live
**ground-control dashboard** that tracks the vehicle in real time.

The simulator models both the *flight* and the *sensors* (BMP280 barometer,
MPU6050 IMU) with realistic noise, and streams telemetry over UDP — exactly the
link the real ESP32 will use over Wi-Fi. The dashboard is hardware-agnostic:
when the physical flight computer arrives, it starts sending the same JSON and
**nothing in the GCS changes**.

## Architecture

```
  +----------------------+        UDP / JSON         +---------------------------+
  |  simulator.py        |  --------------------->   |  gcs.py (FastAPI)         |
  |  "the vehicle"        |   telemetry frames        |  "the ground station"     |
  |  (ESP32 stand-in)     |   127.0.0.1:5551          |  WebSocket -> web/ HUD    |
  +----------------------+                            +---------------------------+
        |  also logs ground truth + sensor CSV
        +----> data/apex1_<timestamp>.csv   (feeds the analytics phase)
```

- **`protocol.py`** — the wire contract (port + frame schema). Single source of
  truth; both ends import it.
- **`simulator.py`** — the vehicle. Integrates the flight, models the sensors,
  streams telemetry, and logs ground truth + sensor readings.
- **`gcs.py`** — the ground station. Receives UDP telemetry, keeps a rolling
  buffer, and pushes frames to the browser over WebSocket. Serves the HUD.
- **`web/`** — the HUD itself: `index.html` + `style.css` (the dark cockpit
  look) and `app.js` (WebSocket client, gauges, data stream).
- **`firmware/`** — the real vehicle: an ESP32 flight computer (C++) that reads
  the BMP280 + MPU6050 and publishes the same JSON over UDP/Wi-Fi. A drop-in
  replacement for the telemetry link; the GCS is unchanged. (One behavior
  differs: the simulator auto-launches/relaunches, while the firmware waits
  for the launch button or `L` on serial before each flight.)

The wire carries only the **noisy sensor readings** — what a real ground station
ever sees. Ground truth is kept internal and logged, so a later phase can compare
measured-vs-true and build filters/estimation.

## Quick start

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

Run the two processes in separate terminals:

```bash
# Terminal 1 — the vehicle (streams telemetry, logs CSV)
python simulator.py

# Terminal 2 — the ground station (live HUD at http://localhost:8501)
python gcs.py
```

The HUD shows **AWAITING TELEMETRY** until the simulator starts, then flips to
**LIVE** and tracks the flight: status banner, MET / flight readouts, and live
gauges for altitude, velocity, acceleration, pressure, and temperature, plus a
scrolling telemetry data stream. Kill the simulator and the HUD drops to
**LINK STALE**; restart it and the link recovers (the WebSocket auto-reconnects
and re-sends recent history).

### Simulator options

```bash
python simulator.py --once          # a single flight, then exit
python simulator.py --gcs 192.168.1.50 --port 5551   # stream to a remote GCS
python simulator.py --no-log        # skip CSV logging
```

## Telemetry frame

One JSON object per sample, sent over UDP:

| Field         | Unit    | Source                          |
|---------------|---------|---------------------------------|
| `t`           | s       | mission elapsed time            |
| `flight`      | —       | flight number (auto-relaunch)   |
| `status`      | —       | PRE-LAUNCH / BOOST / ASCENT / DESCENT / PARACHUTE / LANDED |
| `altitude`    | m       | BMP280 (recovered from pressure)|
| `velocity`    | m/s     | MPU6050                         |
| `accel`       | m/s²    | MPU6050 — specific force (reads ~+9.81 at rest, ~0 in freefall) |
| `pressure`    | hPa     | BMP280                          |
| `temperature` | °C      | BMP280                          |

## Mission log

The HUD has a **MISSION LOG** panel (center column, under the reactor):
**● REC** records the session you're currently watching — live, demo, or
replay — and **▶ PLAY** replays a captured mission frame-for-frame at its
native 50 Hz.

A *mission file* is NDJSON — one telemetry frame per line, the same 8 keys
in the same order as the wire format above. One file is one complete flight
(`PRE-LAUNCH` → `LANDED`, including the ~1.5 s on-pad hold), so the player
can loop it cleanly. The public page ships one committed mission at
`web/missions/apex1_flight_001.ndjson` and plays it by default; the
selector is there for future multi-mission support.

Capture one (run alongside `simulator.py`):

```bash
python tools/record.py --help
```

- **Live capture (default):** `python tools/record.py` binds UDP/5551 and
 writes each valid frame to `missions/apex1_flight_NNN.ndjson`, opening a
 new file at every flight-number boundary (Ctrl+C to stop). **Stop the GCS
 first** — `gcs.py` owns UDP/5551 while it runs, so the recorder must run
 in its place: start `simulator.py` + `tools/record.py`, watch the console
 (or open the HUD at the GCS *after* you switch back), and each flight
 lands in its own file. (Alternatively skip the wire entirely: press ● REC
 in the HUD's MISSION LOG panel — the browser records any session — or use
 the in-process mode below.)
- **In-process capture:** `python tools/record.py --demo --count 1 --seed 7`
 runs `simulator.py`'s flight in-process (no GCS, no UDP) and writes the
 same NDJSON — this is how the committed mission was generated.

Local captures go to `missions/` at the repo root, which is git-ignored so
they don't bloat the repo; `web/missions/` is committed and served by both
GitHub Pages and the GCS (`/missions/*.ndjson`).

## The sensor model

The simulator computes ground truth, then derives what the real parts would read:

- **BMP280** — pressure from altitude via the international barometric formula
  `P = 1013.25·(1 − 0.0065·h/288.15)^5.2558`, temperature via the tropospheric
  lapse rate `T = 15 − 0.0065·h`, each with Gaussian noise. Measured altitude is
  recovered from the (noisy) pressure.
- **MPU6050** — vertical acceleration and velocity, each with Gaussian noise.

## Flight computer firmware (Phase 2)

`firmware/` is the on-board flight computer for the real vehicle. It reads the
BMP280 and MPU6050 over I2C and streams the **same** telemetry JSON over
UDP/Wi-Fi that `simulator.py` produces — so it is a drop-in replacement and the
GCS needs no changes.

- **`firmware/config.h`** — Wi-Fi SSID/password, GCS LAN IP, I2C pins, launch
  button pin, and flight parameters (defaults match the simulator).
- **`firmware/Sensors.h`** — thin wrapper over the Adafruit BMP280 + MPU6050
  libraries.
- **`firmware/FlightState.h`** — the flight state machine + vertical physics,
  mirroring `simulator.py` so the status sequence and units match.
- **`firmware/firmware.ino`** — the sketch: Wi-Fi + I2C setup, the 50 Hz
  telemetry loop, launch handling, and UDP transmission.

**Build & flash** (Arduino IDE): install the *esp32* board package and the
*Adafruit BMP280* + *Adafruit MPU6050* libraries, edit `config.h` (Wi-Fi + GCS
IP), select your ESP32, and upload. Wiring: both sensors share I2C
(SDA→GPIO 21, SCL→GPIO 22, VCC→3V3, GND→GND); launch button GPIO 4→GND.
Launch by pressing the button or typing `L` in the Serial Monitor (115200).

**Known limitations** (see roadmap): velocity is obtained by integrating IMU
acceleration, which drifts over time (sensor fusion is planned); altitude is
barometric (~8 m noise), so landing detection is approximate.

## Roadmap

- **Phase 1** — SITL simulator + live GCS dashboard. ✅
- **Phase 2** — ESP32 flight computer (C++) publishing the same JSON over UDP.
  Firmware written in `firmware/`; the GCS is unchanged. (Needs the hardware to
  flash and fly.)
- **Phase 3** — ML landing prediction + satellite map (folium/pydeck), trained on
  the logged telemetry.
