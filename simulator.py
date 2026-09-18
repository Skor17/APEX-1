"""APEX-1 flight simulator — the "vehicle" / ESP32 stand-in.

Computes ground-truth flight physics, models the on-board sensors (BMP280
barometer + MPU6050 IMU) with realistic noise, streams telemetry over UDP to
the GCS, and logs ground truth + sensor readings to CSV for later analytics.

The wire carries only the noisy *sensor* readings; ground truth is kept
internal and logged, so a later phase can compare measured-vs-true and build
filters/estimation. This mirrors a real flight exactly.

Run:
    python simulator.py                    # continuous flights (auto-relaunch)
    python simulator.py --once             # a single flight, then exit
    python simulator.py --once --selftest  # one flight, then sanity checks
"""
from __future__ import annotations

import argparse
import csv
import random
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import protocol as proto

# Columns written to the ground-truth + sensor log (feeds Phase 3 analytics).
LOG_FIELDS = [
    "t", "flight", "status",
    "y_true", "v_true", "a_true",
    "alt_meas", "vel_meas", "accel_meas", "pressure", "temperature",
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
        self.chute = False
        self.landed = False

    # --- physics ----------------------------------------------------------
    def _accel(self) -> float:
        p = self.p
        thrust = p.thrust_accel if (self.t < p.boost_time and not self.landed) else 0.0
        k = p.chute_drag_k if self.chute else p.drag_k
        drag = -k * self.v * abs(self.v)
        return thrust - p.gravity + drag

    def step(self) -> None:
        """Advance ground truth by one time step (semi-implicit Euler)."""
        if self.landed:
            return
        p = self.p
        self.a = self._accel()
        self.v += self.a * p.dt
        self.y += self.v * p.dt
        self.t += p.dt
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
        )
        tx.sendto(frame.to_json().encode(), addr)
        if record is not None:
            record.append({
                "t": self.t, "status": self.status(),
                "y": self.y, "v": self.v, "a": self.a,
                "altitude": s["altitude"], "velocity": s["velocity"],
                "accel": s["accel"], "pressure": s["pressure"],
                "temperature": s["temperature"],
            })
        if writer is not None:
            writer.writerow({
                "t": round(self.t, 4), "flight": self.flight, "status": self.status(),
                "y_true": round(self.y, 4), "v_true": round(self.v, 4), "a_true": round(self.a, 4),
                "alt_meas": round(s["altitude"], 4), "vel_meas": round(s["velocity"], 4),
                "accel_meas": round(s["accel"], 4), "pressure": round(s["pressure"], 4),
                "temperature": round(s["temperature"], 4),
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
