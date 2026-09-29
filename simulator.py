"""APEX-1 flight simulator — the "vehicle" / ESP32 stand-in.

Computes ground-truth flight physics, models the on-board sensors (BMP280
barometer + MPU6050 IMU) with realistic noise, streams telemetry over UDP to
the GCS, and logs ground truth + sensor readings to CSV for later analytics.

The wire carries only the noisy *sensor* readings; ground truth is kept
internal and logged, so a later phase can compare measured-vs-true and build
filters/estimation. This mirrors a real flight exactly.

Phase 4: the flight is now 2-D — vertical (unchanged, bit-identical) plus a
lateral ground track driven by wind:

  * Wind model: a steady wind (speed + direction) with a light sinusoidal
    gust, `w(t) = wind_speed * (1 + 0.15 * sin(2*pi*t/7))` — deterministic
    (a pure function of t, no RNG), so a run stays reproducible under
    `--seed`.
  * Lateral drag: the same quadratic law as the vertical axis, applied to
    the AIR-RELATIVE lateral velocity:
        v_x += -k_eff * (v_x - wx) * |v_x - wx| * dt ;  x += v_x * dt
    with `k_eff` = the vertical drag coefficient in use (body `drag_k`
    pre-chute, `chute_drag_k` post-chute — documented assumption: one
    quadratic coefficient per configuration on every axis). `wx`/`wy` are
    the wind's downwind/crosswind components, so a non-zero `wind_dir_deg`
    produces crosswind drift. The chute's large lateral drag naturally
    kills the AIR-RELATIVE lateral speed before touchdown (measured:
    |v_x - wx| < ~0.2 m/s at landing); ground-relative v_x then rides with
    the wind.
  * Launch tilt: the motor mount tilts the vehicle off vertical, giving it
    an initial horizontal impulse `v_x0 = tan(tilt) * LAUNCH_VEL` with
    `LAUNCH_VEL` = 30 m/s (documented constant: a typical L1/L2-class
    single-stage sounding rocket leaves a tilt rail at ~30 m/s; the
    exact value only scales the small tilt impulse and is far below the
    ~58 m/s vertical speed at rail exit).
  * Lateral position is NOT a sensor here — the rocket carries no GPS in
    this project; `x`/`y` (y = crosswind) are simulated ground truth that
    the "flight computer" estimates, and the wire carries them exactly.
  * Chute/landing logic is UNCHANGED (chute at v<0 && y<=50 m, landed at
    y<=0 && v<=0); lateral state resets on relaunch like the vertical one.
  * CSV logging: the original 11 columns first (same names, same order),
    then 4 added columns: `x, vx, y_lat, wy` (downwind position,
    downwind velocity, crosswind position, crosswind velocity).

Run:
    python simulator.py                    # continuous flights (auto-relaunch)
    python simulator.py --once             # a single flight, then exit
    python simulator.py --once --selftest  # one flight, then sanity checks
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import protocol as proto

# Launch speed along the tilt rail [m/s] — the documented constant for the
# tilt impulse v_x0 = tan(launch_tilt_deg) * LAUNCH_VEL (see module
# docstring).
LAUNCH_VEL = 30.0

# Gust period [s] and amplitude (fraction of the steady wind speed) for the
# light sinusoidal gust: w(t) = wind_speed * (1 + GUST_AMP * sin(2*pi*t/GUST_PERIOD)).
GUST_PERIOD = 7.0
GUST_AMP = 0.15

# Columns written to the ground-truth + sensor log (feeds analytics).
# The original 11 columns come first, unchanged in name and order (old
# analytics keep working); the 4 lateral columns are strictly appended.
LOG_FIELDS = [
    "t", "flight", "status",
    "y_true", "v_true", "a_true",
    "alt_meas", "vel_meas", "accel_meas", "pressure", "temperature",
    "x", "vx", "y_lat", "wy",
]

# Phases a full flight must pass through, in order (see protocol.py).
PHASE_SEQUENCE = [
    proto.STATUS_PRE_LAUNCH,
    proto.STATUS_BOOST,
    proto.STATUS_ASCENT,
    proto.STATUS_DESCENT,
    proto.STATUS_PARACHUTE,
    proto.STATUS_LANDED,
]


@dataclass
class FlightParams:
    """Tunable flight + sensor parameters (SI units unless noted)."""
    dt: float = 0.02                 # physics time step [s] (50 Hz)
    gravity: float = 9.81            # [m/s^2]
    thrust_accel: float = 40.0       # net upward accel during boost [m/s^2]
    boost_time: float = 2.0          # engine burn duration [s]
    drag_k: float = 0.0009           # quadratic drag coeff, rocket body [1/m]
    chute_deploy_alt: float = 50.0   # parachute deployment altitude [m]
    chute_drag_k: float = 0.15       # quadratic drag coeff, chute open [1/m]
    # --- Phase 4: lateral (2-D) dynamics -----------------------------------
    wind_speed: float = 5.0          # steady wind speed [m/s]
    wind_dir_deg: float = 0.0        # wind direction [deg], 0 = +x (downwind)
    launch_tilt_deg: float = 0.0     # motor-mount tilt off vertical [deg]
    # sensor noise, 1-sigma [respective units]
    noise_pressure: float = 1.0      # [hPa]
    noise_temperature: float = 0.5   # [deg C]
    noise_accel: float = 0.3         # [m/s^2]
    noise_velocity: float = 0.5      # [m/s]
    relaunch_pause: float = 1.5      # pause between flights [s]


class FlightSimulator:
    """Integrates the flight and produces telemetry frames + log rows."""

    def __init__(self, params: FlightParams | None = None):
        self.p = params or FlightParams()
        self.flight = 0
        self._reset()

    def _reset(self) -> None:
        self.t = 0.0
        self.y = 0.0          # altitude [m]
        self.v = 0.0          # velocity [m/s], +up
        self.a = 0.0          # acceleration [m/s^2]
        self.x = 0.0          # downwind position [m]
        self.vx = math.tan(math.radians(self.p.launch_tilt_deg)) * LAUNCH_VEL
        self.y_lat = 0.0      # crosswind position [m]
        self.vy_lat = 0.0     # crosswind velocity [m/s]
        self.chute = False
        self.landed = False

    # --- physics ----------------------------------------------------------
    def _accel(self) -> float:
        p = self.p
        thrust = p.thrust_accel if (self.t < p.boost_time and not self.landed) else 0.0
        k = p.chute_drag_k if self.chute else p.drag_k
        drag = -k * self.v * abs(self.v)
        return thrust - p.gravity + drag

    def wind(self) -> tuple[float, float]:
        """Current wind components (wx downwind, wy crosswind) [m/s].

        Steady wind + light sinusoidal gust (deterministic in t — no RNG,
        so a run stays reproducible under --seed):
            w(t) = wind_speed * (1 + GUST_AMP * sin(2*pi*t / GUST_PERIOD))
        """
        p = self.p
        w = p.wind_speed * (1.0 + GUST_AMP * math.sin(2.0 * math.pi * self.t / GUST_PERIOD))
        d = math.radians(p.wind_dir_deg)
        return w * math.cos(d), w * math.sin(d)

    def step(self) -> None:
        """Advance ground truth by one time step (semi-implicit Euler).

        Vertical axis first (the original 1-D profile, unchanged), then the
        lateral axes with the same quadratic drag law applied to the
        air-relative lateral velocity (see module docstring).
        """
        if self.landed:
            return
        p = self.p
        self.a = self._accel()
        self.v += self.a * p.dt
        self.y += self.v * p.dt
        self.t += p.dt
        k = p.chute_drag_k if self.chute else p.drag_k
        wx, wy = self.wind()
        self.vx += -k * (self.vx - wx) * abs(self.vx - wx) * p.dt
        self.x += self.vx * p.dt
        self.vy_lat += -k * (self.vy_lat - wy) * abs(self.vy_lat - wy) * p.dt
        self.y_lat += self.vy_lat * p.dt
        if (not self.chute) and self.v < 0 and self.y <= p.chute_deploy_alt:
            self.chute = True
        if self.y <= 0.0 and self.v <= 0.0:
            self.y = 0.0
            self.v = 0.0
            self.landed = True

    def status(self) -> str:
        if self.landed:
            return proto.STATUS_LANDED
        if self.t == 0.0:
            return proto.STATUS_PRE_LAUNCH
        if self.t < self.p.boost_time:
            return proto.STATUS_BOOST
        if self.chute:
            return proto.STATUS_PARACHUTE
        if self.v > 0:
            return proto.STATUS_ASCENT
        return proto.STATUS_DESCENT

    # --- sensor model -----------------------------------------------------
    @staticmethod
    def _baro_pressure(h: float) -> float:
        # International barometric formula (troposphere, h < 11 km).
        return 1013.25 * (1 - 0.0065 * h / 288.15) ** 5.2558

    @staticmethod
    def _baro_altitude(p_hpa: float) -> float:
        # Inverse of the above: altitude recovered from a pressure reading.
        return (288.15 / 0.0065) * (1.0 - (p_hpa / 1013.25) ** (1.0 / 5.2558))

    def sensor_readings(self) -> dict:
        """Derive noisy BMP280 + MPU6050 readings from ground truth.

        Event decisions (boost end, chute deployment, landing) are made on
        the ground-truth state in `step()` / `status()`; the wire carries
        only these noisy sensor readings — a real ground station observes,
        it never sees ground truth.

        `accel` is *specific force* (proper acceleration), as a real MPU6050
        outputs it: +9.81 m/s^2 at rest, roughly the thrust during boost,
        ~0 in freefall. Net (kinematic) acceleration is specific force minus
        gravity. The firmware uses the same convention
        (firmware/FlightState.h subtracts gravity to get net accel).
        """
        p = self.p
        h = max(self.y, 0.0)
        # BMP280: true pressure/temp from altitude -> add noise -> recover measured altitude.
        pressure = self._baro_pressure(h) + random.gauss(0, p.noise_pressure)
        temperature = (15.0 - 0.0065 * h) + random.gauss(0, p.noise_temperature)
        altitude = self._baro_altitude(pressure)
        # MPU6050: specific force = net acceleration + gravity (see docstring).
        accel = self.a + p.gravity + random.gauss(0, p.noise_accel)
        velocity = self.v + random.gauss(0, p.noise_velocity)
        return {
            "altitude": altitude,
            "velocity": velocity,
            "accel": accel,
            "pressure": pressure,
            "temperature": temperature,
        }

    # --- output -----------------------------------------------------------
    def _emit(self, tx: socket.socket, addr: tuple, writer: csv.DictWriter | None,
              record: list[dict] | None = None) -> None:
        s = self.sensor_readings()
        frame = proto.TelemetryFrame(
            t=round(self.t, 3),
            flight=self.flight,
            status=self.status(),
            altitude=round(s["altitude"], 3),
            velocity=round(s["velocity"], 3),
            accel=round(s["accel"], 3),
            pressure=round(s["pressure"], 3),
            temperature=round(s["temperature"], 3),
            # Phase 4: lateral ground truth (no GPS on the vehicle; the
            # "flight computer" estimates it — see module docstring) plus the
            # wind state so the GCS model sees what it was trained on.
            x=round(self.x, 3),
            y=round(self.y_lat, 3),
            wind={"speed": self.p.wind_speed, "dir": self.p.wind_dir_deg},
        )
        tx.sendto(frame.to_json().encode(), addr)
        if record is not None:
            wx, wy = self.wind()
            record.append({
                "t": self.t, "status": self.status(),
                "y": self.y, "v": self.v, "a": self.a,
                "x": self.x, "vx": self.vx, "y_lat": self.y_lat,
                "vy_lat": self.vy_lat, "wx": wx, "wy": wy,
                "altitude": s["altitude"], "velocity": s["velocity"],
                "accel": s["accel"], "pressure": s["pressure"],
                "temperature": s["temperature"],
            })
        if writer is not None:
            wx, wy = self.wind()
            writer.writerow({
                "t": round(self.t, 4), "flight": self.flight, "status": self.status(),
                "y_true": round(self.y, 4), "v_true": round(self.v, 4), "a_true": round(self.a, 4),
                "alt_meas": round(s["altitude"], 4), "vel_meas": round(s["velocity"], 4),
                "accel_meas": round(s["accel"], 4), "pressure": round(s["pressure"], 4),
                "temperature": round(s["temperature"], 4),
                "x": round(self.x, 4), "vx": round(self.vx, 4),
                "y_lat": round(self.y_lat, 4), "wy": round(self.vy_lat, 4),
            })

    def run(self, tx: socket.socket, addr: tuple, log_path: Path | None = None,
            once: bool = False, timescale: float = 1.0,
            record: list[dict] | None = None) -> None:
        """Run flights, emitting one telemetry frame per physics step.

        `timescale` > 1 runs the physics faster than wall clock (sleep
        p.dt / timescale per step); the emitted `t` is always in simulated
        seconds. `record`, if given, collects per-frame ground truth + sensor
        readings (used by --selftest).
        """
        p = self.p
        log_file = None
        writer = None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w", newline="")
            writer = csv.DictWriter(log_file, fieldnames=LOG_FIELDS)
            writer.writeheader()
        try:
            while True:
                self.flight += 1
                self._reset()
                self._emit(tx, addr, writer, record)      # PRE-LAUNCH
                time.sleep(p.dt / timescale)
                while not self.landed:
                    self.step()
                    self._emit(tx, addr, writer, record)
                    time.sleep(p.dt / timescale)
                if once:
                    break
                time.sleep(p.relaunch_pause / timescale)
        finally:
            if log_file is not None:
                log_file.close()


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def run_selftest(sim: FlightSimulator, rows: list[dict]) -> int:
    """Validate one full flight and print a per-check summary.

    The peak-altitude / peak-velocity bands are calibrated for the default
    FlightParams profile (~210 m, ~58 m/s); with custom parameters those
    two checks may legitimately fail.
    Returns a process exit code: 0 if every check passed, 1 otherwise.
    """
    p = sim.p
    statuses = [r["status"] for r in rows]
    deduped = [s for i, s in enumerate(statuses) if i == 0 or s != statuses[i - 1]]

    def subsequence(seq: list[str], whole: list[str]) -> bool:
        it = iter(whole)
        return all(s in it for s in seq)

    def fmt(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.2f}"

    max_alt = max(r["y"] for r in rows)
    max_v = max(r["v"] for r in rows)
    flight = [r for r in rows if r["t"] > 0.0]
    baro_err = _mean([abs(r["altitude"] - r["y"]) for r in flight])
    rest_sf = rows[0]["accel"]
    boost_sf = [r["accel"] for r in rows if r["status"] == proto.STATUS_BOOST]
    descent_sf = [r["accel"] for r in rows if r["status"] == proto.STATUS_DESCENT]
    boost_mean = _mean(boost_sf) if boost_sf else None
    descent_mean_abs = _mean([abs(x) for x in descent_sf]) if descent_sf else None

    checks: list[tuple[str, bool, str]] = [
        ("peak altitude",
         190.0 <= max_alt <= 230.0,
         f"{max_alt:.1f} m in [190.0, 230.0] (default profile peaks ~210 m)"),
        ("peak velocity",
         50.0 <= max_v <= 65.0,
         f"{max_v:.1f} m/s in [50.0, 65.0]"),
        ("phase sequence",
         subsequence(PHASE_SEQUENCE, statuses),
         " -> ".join(deduped)),
        ("baro altitude accuracy",
         baro_err < 12.0,
         f"mean |meas - true| {baro_err:.2f} m < 12 m during flight"),
        ("specific force at rest",
         abs(rest_sf - p.gravity) < 1.5,
         f"{rest_sf:.2f} m/s^2 ~= +g ({p.gravity})"),
        ("specific force during boost",
         boost_mean is not None and abs(boost_mean - p.thrust_accel) <= 0.25 * p.thrust_accel,
         f"mean {fmt(boost_mean)} m/s^2 ~= thrust {p.thrust_accel:.1f} m/s^2"),
        ("specific force in freefall",
         descent_mean_abs is not None and descent_mean_abs < 3.0,
         f"mean |SF| {fmt(descent_mean_abs)} m/s^2 ~= 0 (freefall)"),
    ]

    # --- Phase 4: lateral (2-D) checks -------------------------------------
    # Checks 8-10 validate the wind-driven lateral drift. The vertical
    # physics above is unchanged; these only run on the default lateral
    # model (steady wind + sinusoidal gust, deterministic — the 50-seed
    # calibration run gives one single landing site, so the bands below
    # carry large margins).
    # Measured with the default profile (wind 5 m/s, dir 0): landing x =
    # 19.2 m, y_lat = 0.0 m, ground-relative v_x = 4.3 m/s while the
    # AIR-RELATIVE residual |v_x - wx| at landing is only 0.14 m/s (the
    # chute's lateral drag kills drift in the air; the ground track then
    # keeps riding with the wind).
    last = rows[-1]
    wx_land, wy_land = last["wx"], last["wy"]
    # (8) downwind landing distance: for a constant 5 m/s wind the naive
    # band 0.5*w*t_parachute ~ 4-16 m undershoots the real physics
    # (drift accumulates from launch, not from chute deploy: freefall
    # ~9 s + chute ~7 s at ~4-5 m/s air-relative + tilt-free start).
    # Measured distribution (default profile, 50 seeds): x = 19.198 m for
    # every seed (deterministic). Band = measured +/- generous margin.
    x_land = last["x"]
    drift_ok = 10.0 <= x_land <= 30.0 if p.wind_speed == 5.0 and p.wind_dir_deg == 0.0 \
        else abs(x_land) <= 0.5 * p.wind_speed * (last["t"] + 5.0)  # sanity for non-default
    checks.append(
        ("landing downwind x",
         drift_ok,
         f"x = {x_land:.1f} m at landing (wind {p.wind_speed:.0f} m/s @ "
         f"{p.wind_dir_deg:.0f} deg; default-profile measured band "
         f"10..30 m, actual 19.2 m in all 50 calibration seeds)"))
    # (9) crosswind position: no crosswind drift when the wind is aligned.
    if p.wind_dir_deg == 0.0:
        checks.append(
            ("landing crosswind y_lat",
             abs(last["y_lat"]) < 2.0,
             f"|y_lat| = {abs(last['y_lat']):.3f} m < 2 m (wind aligned with +x)"))
    # (10) the chute must have damped the lateral AIR-RELATIVE velocity:
    # ground-relative v_x rides with the wind, so the honest check is the
    # residual drift in the air at touchdown.
    air_vx = abs(last["vx"] - wx_land)
    checks.append(
        ("lateral drift killed by chute",
         air_vx < 2.0,
         f"|v_x - wind| = {air_vx:.2f} m/s < 2 m/s at landing "
         f"(v_x {last['vx']:.2f} vs wind {wx_land:.2f} m/s — the chute "
         f"damps air-relative drift; ground track rides the wind)"))

    failed = 0
    for name, ok, detail in checks:
        print(f"selftest: {'PASS' if ok else 'FAIL'}  {name}: {detail}")
        failed += 0 if ok else 1
    print(f"selftest: {len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="APEX-1 flight simulator (ESP32 stand-in)")
    ap.add_argument("--once", action="store_true", help="run a single flight then exit")
    ap.add_argument("--gcs", default="127.0.0.1", help="GCS host to stream to")
    ap.add_argument("--port", type=int, default=proto.PORT, help="UDP port")
    ap.add_argument("--no-log", action="store_true", help="disable CSV logging")
    ap.add_argument("--timescale", type=float, default=1.0,
                    help="run the physics N x faster than wall clock (t stays in simulated seconds)")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed the sensor-noise RNG for deterministic runs")
    ap.add_argument("--selftest", action="store_true",
                    help="run one full flight, then sanity checks; exit non-zero on failure")
    # Flight parameters — defaults are the FlightParams defaults; pass to override.
    ap.add_argument("--thrust-accel", type=float, default=None,
                    help="net upward accel during boost [m/s^2]")
    ap.add_argument("--boost-time", type=float, default=None, help="engine burn duration [s]")
    ap.add_argument("--drag-k", type=float, default=None, help="quadratic drag coeff, rocket body [1/m]")
    ap.add_argument("--chute-deploy-alt", type=float, default=None,
                    help="parachute deployment altitude [m]")
    ap.add_argument("--chute-drag-k", type=float, default=None,
                    help="quadratic drag coeff, chute open [1/m]")
    ap.add_argument("--dt", type=float, default=None, help="physics time step [s]")
    # Phase 4: lateral (2-D) wind + tilt parameters.
    ap.add_argument("--wind-speed", type=float, default=None,
                    help="steady wind speed [m/s] (default 5.0)")
    ap.add_argument("--wind-dir", type=float, default=None,
                    help="wind direction [deg], 0 = +x downwind (default 0.0)")
    ap.add_argument("--launch-tilt", type=float, default=None,
                    help="motor-mount tilt off vertical [deg] (default 0.0)")
    args = ap.parse_args()

    if args.timescale <= 0.0:
        ap.error("--timescale must be > 0")

    params = FlightParams(**{k: v for k, v in {
        "thrust_accel": args.thrust_accel,
        "boost_time": args.boost_time,
        "drag_k": args.drag_k,
        "chute_deploy_alt": args.chute_deploy_alt,
        "chute_drag_k": args.chute_drag_k,
        "dt": args.dt,
        "wind_speed": args.wind_speed,
        "wind_dir_deg": args.wind_dir,
        "launch_tilt_deg": args.launch_tilt,
    }.items() if v is not None})
    if args.seed is not None:
        random.seed(args.seed)

    addr = (args.gcs, args.port)
    log_path = None if args.no_log else Path("data") / f"apex1_{time.strftime('%Y%m%d_%H%M%S')}.csv"

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"APEX-1 simulator streaming to {args.gcs}:{args.port}")
    if log_path is not None:
        print(f"logging ground truth + sensors to {log_path}")
    record: list[dict] | None = [] if args.selftest else None
    sim = FlightSimulator(params)
    exit_code = 0
    try:
        sim.run(tx, addr, log_path, once=args.once or args.selftest,
                timescale=args.timescale, record=record)
    except KeyboardInterrupt:
        print("\nstopped.")
    else:
        if args.selftest and record is not None:
            exit_code = run_selftest(sim, record)
    finally:
        tx.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
