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
  [ rocket ESP32 / PC simulator ]   UDP 5551 (JSON)   [ gcs.py ]                    [ browser HUD ]
       the vehicle  --------------->  the ground station  --------->  web/ (this page)
                                      |
                                      +-- [ LandingPredictor (Phase 4) ]  -- predicts eta / impact / apogee
                                          (model/apex1_landing_v2.pkl)       + landing site + confidence ellipse
                                                                              attached as `pred` at push time
```

- **The vehicle** — the rocket's flight computer (or `simulator.py` standing
  in for it) sends one small JSON frame 50 times a second over UDP to port
  5551: mission time, flight number, status, altitude, velocity, acceleration,
  air pressure, temperature.
- **The ground station** — `gcs.py` receives those UDP frames, keeps a rolling
  buffer, and pushes them to any connected browser over WebSocket. Before the
  WebSocket push it runs each frame through the **landing predictor** (Phase 3)
  and attaches the prediction as the optional `pred` field. It also serves the
  HUD's files.
- **The browser HUD** — `web/index.html` + `app.js` + `style.css`. It draws
  each frame on the instruments (gauge arcs, altitude reactor, attitude,
  radar, charts, and the LNDG PRED countdown). It can also run without any of
  the above: the recorded mission file in `web/missions/` is played directly,
  and a synthetic demo engine can generate frames on its own.

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

A *mission file* is **NDJSON** — one telemetry frame per line, the same keys
in the same order as the wire format (see the [frame table](#telemetry-frame)).
Recorded missions predate Phase 3 and carry the original 8 fields (no `pred`)
— the player and instruments handle both, since `pred` is optional.
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

## Phase 3 — Predictive landing (superseded by Phase 4, kept for history)

The HUD shows **LNDG PRED** (right column): a live countdown to the
predicted landing (`T-12.4s`), the predicted impact speed, the predicted
apogee, and a confidence bar.

**What it predicts, in plain English.** Given the telemetry the Ground
Station is watching *right now*, it predicts *when and how the rocket will
land*: seconds until touchdown, the speed at impact, and the top of the
flight. You can watch it work: start the simulator, and the countdown
starts counting down during descent and lands at ~`T-0.0s` at the pad.
There is also a small amber "LNDG PRED" tick on the altitude chart marking
the predicted landing time.

**How it was trained.** `tools/train_model.py` generates ~2000 simulated
flights by running the *same* simulator physics with randomized parameters
around the real profile (thrust 30–50 m/s², boost 1.5–2.5 s, body drag
0.0006–0.0015, chute drag 0.10–0.25, chute deploy 40–70 m) — plus the
simulator's own sensor noise, so the model trains on exactly what the GCS
sees. Each flight contributes one row every 5 frames (10 Hz); the targets
(seconds-to-landing, impact velocity, apogee) are computed from the
flight's *ground truth*. Gradient-boosted trees (one per target) are fit,
with 10% of the **flights** held out. Phase 3 test accuracy (full run,
seed 42) was **≈ 0.99 s** on the countdown, **≈ 0.7 m/s** on impact
velocity, **≈ 7.5 m** on apogee — and much tighter in the phases that
matter: ≈ 0.2 s on the countdown during PARACHUTE. The active model is now
**v2** (Phase 4, below): same vertical targets plus the landing *site*,
trained on 2000 flights with randomized wind and tilt.

A note on the countdown's ≈ 1 s error: it is **not** a model weakness —
it is the limit of the information the wire carries. Before the chute
opens, the landing time depends on the chute's drag (which the model has
never seen) and on exactly where it deploys; on the test set those vary
enough to spread true landing times by ~3 s between otherwise identical
flights. Once the chute is out (and after the burn) the prediction
narrows sharply. On the real vehicle — fixed burn, one real chute — the
countdown will sit comfortably inside half a second.

**Where the model lives.** `model/apex1_landing_v2.pkl` (joblib — the
active model; the v1 file was removed when v2 shipped, retraining
regenerates it) + `model/metadata.json` (feature order, distributions,
the MAE report, the confidence-ellipse table). The GCS loads both once at
startup; the HUD never runs the model — the prediction is computed on the
Ground Station and travels on the wire as an optional `pred` field, which
is why LNDG PRED is dimmed during replay and the in-browser demo.

**Retraining.** `pip install -r requirements.txt` (pulls in
`scikit-learn`), then:

```bash
python tools/train_model.py          # full run (~2000 flights)
python tools/train_model.py --quick  # 200 flights, for fast iteration
python prediction.py                 # sanity-check the model from the CLI
```

If `model/` is ever missing, the GCS starts normally and simply serves
frames without `pred` (the instrument stays dimmed) — it never crashes.

## Phase 4 — Predicting the landing site

The Phase 3 countdown told the jury *when* the rocket lands. Phase 4 tells
them ***where***: the GCS now predicts the **landing site** — the (x, y)
point on the ground the rocket will touch down at — and draws a
**confidence ellipse** around it that visibly *shrinks* as the flight
ends. That shrinking ellipse is the demo's money shot: you can watch the
uncertainty collapse from "somewhere in this big oval" to "right here".

**What it does, in plain English.** A rocket has no GPS in this project,
so the "flight computer" (the simulator) estimates its ground track from
its own physics: the pad, the wind, and the drift add up to a touchdown
point. The wind is a steady breeze (5 m/s by default) plus a light
sinusoidal gust, and it pushes the rocket off the launch line — freefall
drifts it one way, the chute mostly stops the drift in the air, and the
ground track rides the wind until touchdown. On the default profile the
rocket lands ≈ 19 m downwind. The model learns this pad → wind → drift →
touchdown mapping from the same live telemetry the GCS already receives:
`pred` now carries the predicted site (`land_x`, `land_y`) plus the
ellipse (`ell_a`, `ell_b`, `ell_ang` — 1σ semi-axes and the orientation of
the major axis, which the HUD rotates by `ell_ang`).

**Why wind matters.** With no GPS, the wind *is* the lateral dynamics:
it is the only thing that moves the rocket sideways, and it is
transmitted on the wire (`wind` field) so the model knows exactly what
it is flying into. Change the demo wind and the predicted — and the
actual — landing site moves with it.

**How the model learned it.** `tools/train_model.py` (v2) generates 2000
simulated flights with the vertical parameters randomized around the real
profile *and* the wind randomized too: speed 2–12 m/s, direction
0–360°, launch tilt 0–3°. Five gradient-boosted trees are fit — the three
v1 targets (eta, impact velocity, apogee) plus `land_x` and `land_y` —
with 10% of the flights held out. Test accuracy (full run, seed 42):
eta ≈ 0.94 s, impact ≈ 0.67 m/s, apogee ≈ 7.4 m, site ≈ 4.7/5.1 m. The
site error is honest physics, not model weakness: before the chute opens,
the touchdown point depends on the chute's drag and deploy altitude,
which nothing on the wire reveals until the chute is out — from a fixed
mid-descent state those two hidden parameters alone spread the touchdown
by ~37 m. Once the chute is out the model sits within ~1.5× of that
physical floor (the PARACHUTE-bucket site MAE is ≈ 2.8 m). On the real
vehicle — one real chute, fixed deploy altitude — that floor vanishes.

**The confidence ellipse.** After training, the model's own errors on the
held-out flights are bucketed by (status, altitude band) and the 2×2
residual covariance of the site errors is stored per bucket in
`model/metadata.json`. At serving time the GCS takes the covariance for
the current bucket and eigen-decomposes it: the 1σ ellipse is the
principal axes of that cloud. Watch a live flight: during DESCENT the
major axis is ~9 m wide; by PARACHUTE it is ~4.5 m — the ellipse shrinks
by half in the last phase of flight, exactly because the held-out data
says the model is much better there.

**Trying a different wind.** Run the simulator with:

```bash
python simulator.py --wind-speed 8 --wind-dir 90    # 8 m/s crosswind
python simulator.py --wind-speed 12 --wind-dir 135 --launch-tilt 2
```

The predicted site, the ellipse, and the ground track all follow the
wind. (The HUD rendering of the site + ellipse lands in the Phase 4B
frontend subtask; the wire and the model are complete now.)

**Retraining.** Same commands as Phase 3 — `python tools/train_model.py`
(full), `--quick` (200 flights), `python prediction.py` (sanity check).
Retraining regenerates `model/apex1_landing_v2.pkl` + `metadata.json`
(including the ellipse table).

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
  +----------------------+        UDP / JSON         +----------------------------------+
  |  simulator.py        |  --------------------->   |  gcs.py (FastAPI)                |
  |  "the vehicle"        |   telemetry frames        |  "the ground station"            |
  |  (ESP32 stand-in)     |   127.0.0.1:5551          |  +-- LandingPredictor (Phase 4)  |
  +----------------------+                            |  |    model/apex1_landing_v2.pkl |
        |  also logs ground truth + sensor CSV        |  +-- WebSocket -> web/ HUD       |
        +----> data/apex1_<timestamp>.csv             +----------------------------------+
              (feeds the analytics phase)
```

- **`protocol.py`** — the wire contract (port + frame schema). Single source of
  truth; both ends import it. Phase 4 adds three optional fields (`x`, `y`,
  `wind`) after the original eight, byte-compatible with the legacy 8-key
  frames the ESP32 sends.
- **`simulator.py`** — the vehicle. Integrates the flight (2-D since Phase 4:
  vertical + wind-driven lateral drift, bit-identical vertical physics),
  models the sensors, streams telemetry, and logs ground truth + sensor
  readings (the CSV gained `x, vx, y_lat, wy` columns after the original 11).
- **`gcs.py`** — the ground station. Receives UDP telemetry, keeps a rolling
  buffer, and pushes frames to the browser over WebSocket. Before the push it
  enriches each frame with the landing prediction (`pred`). Serves the HUD.
- **`prediction.py`** — the landing predictor (Phase 4). Loads
  `model/apex1_landing_v2.pkl` + `metadata.json` once;
  `LandingPredictor.predict(frame, history)` returns
  `{"eta", "v_impact", "apogee", "conf", "land_x", "land_y", "ell_a",
  "ell_b", "ell_ang"}` or `None` (PRE-LAUNCH, LANDED, or model unavailable).
  Stateless per frame; the confidence and the confidence ellipse come from
  the model's measured per-(status, altitude-band) test errors, not
  constants. Legacy frames (no `x`/`y`/`wind`) still predict — vertical
  targets stay valid, the site degrades to the windless answer.
- **`tools/train_model.py`** — the Phase 4 trainer. Drives `FlightSimulator`
  over randomized parameter distributions (vertical + wind speed 2–12 m/s,
  wind direction 0–360°, tilt 0–3°), samples features at 10 Hz, fits five
  HistGradientBoosting regressors, computes the per-bucket landing-site
  residual covariances, and writes `model/`.
- **`model/`** — the committed artifacts: `apex1_landing_v2.pkl` (the five
  estimators, joblib) and `metadata.json` (feature order, distributions,
  seeds, per-target test MAE/R², per-status + per-bucket eta MAE profiles,
  and the confidence-ellipse covariance table).
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
| `x`           | m       | **optional** (Phase 4): downwind position — the vehicle's estimated ground track (no GPS on board); the ESP32 firmware never sends it |
| `y`           | m       | **optional** (Phase 4): crosswind position (same as `x`) |
| `wind`        | m/s,°   | **optional** (Phase 4): `{"speed": m/s, "dir": deg}` — the wind the vehicle is flying in (0° = +x downwind) |
| `pred`        | —       | **optional** (Phase 3/4): GCS-side landing prediction, attached at WebSocket push time — `{"eta": s, "v_impact": m/s, "apogee": m, "conf": 0..1, "land_x": m, "land_y": m, "ell_a": m, "ell_b": m, "ell_ang": rad}` (all keys optional floats; `ell_a`/`ell_b`/`ell_ang` = 1σ confidence ellipse around the predicted site, major-axis angle in [-π/2, π/2)); the vehicle never sends it, and it is omitted entirely when absent (old frames, replay, ESP32, PRE-LAUNCH/LANDED) |

The original 8 fields are byte-compatible with the pre-Phase-4 wire format
(names, order, types, units unchanged); `x`/`y`/`wind` are strictly
additive — each is omitted from the JSON when absent, so legacy frames
(ESP32, old NDJSON missions) parse and re-serialize unchanged.

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
- **Phase 3** — ML predictive landing. ✅ The GCS runs a gradient-boosted
  model trained on simulated flights and streams the prediction on the wire
  as the optional `pred` field; the HUD's LNDG PRED instrument counts down
  to the predicted landing (see ["Phase 3 — Predictive landing"](#phase-3--predictive-landing)).
- **Phase 4A** — 2-D flight + landing-SITE prediction (backend). ✅ The
  simulator is wind-driven 2-D, the wire carries `x`/`y`/`wind`, and the
  active model (v2) predicts the landing site + a 1σ confidence ellipse
  that shrinks as the flight ends (see
  ["Phase 4 — Predicting the landing site"](#phase-4--predicting-the-landing-site)).
  Remaining: the HUD rendering of the site + ellipse (Phase 4B frontend)
  and a regenerated mission file with the new fields.
