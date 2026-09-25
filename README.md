# APEX-1 · Litoral Guidance

A software-first **flight telemetry** system for a sounding rocket: a
software-in-the-loop (SITL) simulator that stands in for the on-board flight
computer, a live **ground-control dashboard**, and an Iron Man-style cockpit
display (the HUD) in `web/`.

The simulator models both the *flight* and the *sensors* (BMP280 barometer,
MPU6050 IMU) with realistic noise, and streams telemetry over UDP — exactly the
link the real ESP32 will use over Wi-Fi. The dashboard is hardware-agnostic:
when the physical flight computer arrives, it starts sending the same JSON and
**nothing in the GCS changes**.

---

## Open the display

The HUD is a plain web page, no install needed. Open it in any browser:

**https://skor17.github.io/APEX-1/**

What you'll see: within about a second of loading, the page is already playing
a **recorded mission** (badge `REPLAY · CAPTURED FLIGHT`) — launch, boost,
apogee, parachute, landing — looping automatically. The `?` button in the top
bar opens an in-page help overlay (it opens by itself once, the first time,
and never again). The public page is hosted on GitHub Pages and
**auto-deploys on every push to `main`**, so the link always shows the latest
HUD.

URL shortcuts:

- `?replay=1` — force replay of the recorded mission, without even trying the
  live link.
- `?demo=1` — force the synthetic demo (a flight generated in your browser,
  amber `DEMO · SYNTHETIC TELEMETRY` badge), no network at all.

Want the full system on your own machine? The quick start in
["Running it on your PC"](#running-it-on-your-pc) streams real simulated
telemetry into the same HUD at `http://localhost:8501`.

## How a frame gets on screen

```
  [ rocket ESP32 / PC simulator ]   UDP 5551 (JSON)   [ gcs.py ]    WebSocket    [ browser HUD ]
       the vehicle  --------------->  the ground station  --------->  web/ (this page)
```

- **The vehicle** — the rocket's flight computer (or `simulator.py` standing
  in for it) sends one small JSON frame 50 times a second over UDP to port
  5551: mission time, flight number, status, altitude, velocity, acceleration,
  air pressure, temperature.
- **The ground station** — `gcs.py` receives those UDP frames, keeps a rolling
  buffer, and pushes them to any connected browser over WebSocket. It also
  serves the HUD's files.
- **The browser HUD** — `web/index.html` + `app.js` + `style.css`. It draws
  each frame on the instruments (gauge arcs, altitude reactor, attitude,
  radar, charts). It can also run without any of the above: the recorded
  mission file in `web/missions/` is played directly, and a synthetic demo
  engine can generate frames on its own.

The wire carries only the **noisy sensor readings** — what a real ground
station ever sees. Ground truth is kept internal and logged, so a later phase
can compare measured-vs-true and build filters/estimation.

## The four modes

| Mode    | What it is                                                        | You see on screen                                    |
|---------|-------------------------------------------------------------------|------------------------------------------------------|
| **LIVE**   | Real data from the rocket, or from `simulator.py` via `gcs.py`.  | green `● LIVE` link pill (top right)                 |
| **REPLAY** | Playing a captured flight file, frame for frame at 50 Hz. The public page's default. | cyan `REPLAY · CAPTURED FLIGHT` badge |
| **DEMO**   | Synthetic flight generated in your browser (no network). Used when nothing else is connected. | amber `DEMO · SYNTHETIC TELEMETRY` badge |
| **REC**    | You are capturing whatever is currently on screen.               | `● REC` button glows red (MISSION LOG panel)         |

Rules: the first source to deliver a frame wins at boot — a live WebSocket
frame starts LIVE, a fetched mission file starts REPLAY (typically within a
second on the public page); if the fetch fails, the HUD waits 5 s for a live
frame and then falls back to DEMO. A live frame always beats an auto-replay
mid-session (forced `?demo=1` / `?replay=1` never get interrupted).

## Recording a flight

A *mission file* is **NDJSON** — one telemetry frame per line, the same 8 keys
in the same order as the wire format (see the [frame table](#telemetry-frame)).
One file is one complete flight (`PRE-LAUNCH` → `LANDED`, including the
~1.5 s on-pad hold), so the HUD's player can loop it cleanly. Three ways to
make one:

1. **In the browser — press ● REC** (MISSION LOG panel, center column).
   It saves the telemetry you're watching — live, replay, or demo. Press it
   again to stop and your browser downloads `apex1_flight_<timestamp>.ndjson`
   to your Downloads folder. The easiest way, works on the public page, needs
   no Python.
2. **On your PC — `python tools/record.py`** while `simulator.py` runs.
   It writes `missions/apex1_flight_NNN.ndjson` (one file per flight number,
   Ctrl+C to stop). **Stop `gcs.py` first**: the GCS owns UDP/5551 while it
   runs, so the recorder must run *in its place* — start `simulator.py` +
   `tools/record.py`, and each flight lands in its own file.
   (No-network variant: `python tools/record.py --demo --count 1 --seed 7`
   runs the flight in-process and writes the same NDJSON — this is how the
   committed mission was generated.)
3. **The simulator's own log** — `simulator.py` always writes
   `data/apex1_<timestamp>.csv` (ground truth + sensor readings; skip with
   `--no-log`). That's a log, not a mission file, but it's what the later
   analytics phase trains on.

Where the data lives: **your Downloads** (browser captures) · **`missions/`**
at the repo root (PC captures, git-ignored so it doesn't bloat the repo) ·
**`data/*.csv`** (PC ground-truth logs, git-ignored) · **`web/missions/`**
(committed — what the public website and the GCS serve at
`/missions/*.ndjson`).

## Publishing a mission to the website

1. Take a captured `.ndjson` (from Downloads, or `missions/`).
2. Copy it into `web/missions/` (e.g. `web/missions/apex1_flight_002.ndjson`).
3. `git add web/missions/ && git commit -m "..." && git push`
4. GitHub Pages redeploys on the push; the new mission is playable on the
   public page (select it in the MISSION LOG dropdown). The committed
   `apex1_flight_001.ndjson` is the default the page plays at boot.

## Running it on your PC

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

Open `http://localhost:8501`. The HUD opens instantly (it plays the recorded
mission in the meantime); when the simulator's first frame arrives it flips to
**LIVE** and tracks the flight: status banner, MET / flight readouts, live
gauges for altitude, velocity, acceleration, pressure, and temperature, and
the two scrolling charts. Kill the simulator and the HUD drops to
**LINK STALE**; restart it and the link recovers (the WebSocket auto-reconnects
and re-sends recent history).

```bash
python simulator.py --once          # a single flight, then exit
python simulator.py --gcs 192.168.1.50 --port 5551   # stream to a remote GCS
python simulator.py --no-log        # skip CSV logging
```

---

## Technical reference

### Components

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
  look) and `app.js` (WebSocket client, gauges, charts, replay player, session
  recorder, demo engine — all zero-dependency).
- **`tools/record.py`** — mission recorder (UDP capture or in-process demo
  flight) producing the NDJSON mission files.
- **`firmware/`** — the real vehicle: an ESP32 flight computer (C++) that reads
  the BMP280 + MPU6050 and publishes the same JSON over UDP/Wi-Fi. A drop-in
  replacement for the telemetry link; the GCS is unchanged. (One behavior
  differs: the simulator auto-launches/relaunches, while the firmware waits
  for the launch button or `L` on serial before each flight.)

### Telemetry frame

One JSON object per sample, sent over UDP (and used verbatim in NDJSON mission
files):

| Field         | Unit    | Source                          |
|---------------|---------|---------------------------------|
| `t`           | s       | mission elapsed time            |
| `flight`      | —       | flight number (auto-relaunch)   |
| `status`      | —       | PRE-LAUNCH / BOOST / ASCENT / DESCENT / PARACHUTE / LANDED |
| `altitude`    | m       | BMP280 (recovered from pressure)|
| `velocity`    | m/s     | MPU6050                         |
| `accel`       | m/s²    | MPU6050 — **specific force** (reads ~+9.81 at rest, ~0 in freefall; gauge + charts show it as received) |
| `pressure`    | hPa     | BMP280                          |
| `temperature` | °C      | BMP280                          |

### The sensor model

The simulator computes ground truth, then derives what the real parts would read:

- **BMP280** — pressure from altitude via the international barometric formula
  `P = 1013.25·(1 − 0.0065·h/288.15)^5.2558`, temperature via the tropospheric
  lapse rate `T = 15 − 0.0065·h`, each with Gaussian noise. Measured altitude is
  recovered from the (noisy) pressure.
- **MPU6050** — vertical acceleration and velocity, each with Gaussian noise.

### Flight computer firmware (Phase 2)

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

### Roadmap

- **Phase 1** — SITL simulator + live GCS dashboard. ✅
- **Phase 2** — ESP32 flight computer (C++) publishing the same JSON over UDP.
  Firmware written in `firmware/`; the GCS is unchanged. (Needs the hardware to
  flash and fly.)
- **Phase 3** — ML landing prediction + satellite map (folium/pydeck), trained on
  the logged telemetry.
