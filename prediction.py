"""APEX-1 landing predictor v2 — GCS-side inference for the `pred` wire field.

Loads the Phase 4 model artifacts once at import time:

    model/apex1_landing_v2.pkl   five HistGradientBoosting estimators
                                 (eta_s, v_landing_m_s, apogee_m, land_x, land_y)
    model/metadata.json          feature order, distributions, test MAE
                                 report, per-status + per-(status, altitude
                                 band) eta MAE, and the landing-site
                                 residual covariance table (the ellipse)

`LandingPredictor.predict(frame, history)` takes the latest `TelemetryFrame`
plus a short rolling history (the GCS buffer) and returns:

    {"eta": <s>, "v_impact": <m/s>, "apogee": <m>, "conf": <0..1>,
     "land_x": <m>, "land_y": <m>,
     "ell_a": <m>, "ell_b": <m>, "ell_ang": <rad>}

or `None` when it has nothing to say:

  * status PRE-LAUNCH or LANDED — no landing in the future to predict,
  * the model file is missing/corrupt — one clear warning is logged and the
    predictor degrades to `None` forever (the GCS must never crash on it).

The schema is EXACT — these 9 keys, all floats, no more.

Landing site + confidence ellipse (Phase 4):
  * `land_x` / `land_y` — the predicted touchdown site [m] (downwind /
    crosswind, same axes as the frame's `x`/`y`).
  * `ell_a` / `ell_b` / `ell_ang` — the 1-sigma confidence ellipse around
    that point. The trainer stored, per (status, altitude-band) bucket of
    the held-out test flights, the 2x2 residual covariance of the site
    prediction. Here we take the covariance for the current bucket and
    eigen-decompose the 2x2 analytically: the eigenvalues are the
    variances along the PRINCIPAL AXES of the residual cloud, so
    ell_a = sqrt(lambda_max), ell_b = sqrt(lambda_min) (ell_a >= ell_b),
    and ell_ang is the angle of the eigenvector of the LARGEST eigenvalue
    (radians, wrapped to [-pi/2, pi/2)). The HUD rotates the ellipse by
    ell_ang to draw it; an empty bucket falls back to the OVERALL
    covariance. The ellipse SHRINKS as the flight ends (the DESCENT
    buckets are far tighter than the PARACHUTE/ASCENT ones) — that is the
    "watch the uncertainty shrink" demo moment.
  * `conf` — clamp(1 - eta_MAE_bucket / 2, 0, 1) where
    eta_MAE_bucket is the measured eta error of the held-out rows in the
    current (status, altitude-band) bucket (fallback: the current status's
    MAE, then the overall MAE). A 0.2 s typical error gives ~0.9; a 1.2 s
    one gives 0.4. Honest, from the test set, bucket-aware.

History usage: the model is trained on a 29-column feature vector built
from the latest frame PLUS the GCS rolling buffer of the same flight (see
tools/train_model.py FEATURE_NAMES — the trainer and this module MUST be
changed together): the 21 v1 columns, unchanged, plus 8 lateral columns
(`x, y, vx, vy_lat, wind_speed, wx, wy, t_wind_phase`). At serving time:

  * `x` / `y` come straight from the frame (simulated ground truth — the
    vehicle's estimated track, no GPS on board);
  * `vx` / `vy_lat` by finite difference of the last two same-flight
    frames' lateral positions (dt = the frame spacing; the GCS buffer
    holds every 50 Hz frame, exactly as in training);
  * `wind_speed`, `wx`, `wy` from the frame's `wind` dict + the same
    deterministic gust law the simulator uses,
    w(t) = wind_speed * (1 + 0.15 * sin(2*pi*t/7));
  * `t_wind_phase` = 2*pi*t / 7 (computable from t alone).

LEGACY FRAMES (x / y / wind all None — e.g. the ESP32 firmware, old NDJSON
missions): the lateral features are zeroed (x=0, y=0, vx=vy=0, wind=0).
The vertical targets (eta / v_impact / apogee) remain valid — the 21 v1
columns are untouched — while land_x / land_y degrade to the model's
answer for a windless flight (a reduced-meaning site, documented; the
vertical countdown is the part to trust on legacy links). The ellipse and
conf are still reported from the bucket table (they are properties of the
model, not of the frame).

One documented approximation (unchanged from v1): the training value of
`t_since_boost_end` is `t - boost_time`, and the GCS does not transmit
`boost_time`. At serving time the predictor uses the vehicle's configured
burn time (BOOST_TIME_PRIOR = 2.0 s — the fixed burn in the simulator
defaults and `firmware/config.h`). While BOOST is reported the value is
exactly 0 (as in training); afterwards it carries at most a ±0.5 s offset
versus the value seen in training, which the tree ensemble absorbs (it
also sees `t` and the status one-hot). For the real vehicle the offset is
exactly 0, because the burn time is fixed.

Self-check:  python prediction.py
    drives a REAL simulated flight to mid-DESCENT and mid-PARACHUTE,
    prints the predictions (incl. the ellipse), and asserts the ellipse
    is sane (ell_a >= ell_b >= 0.3 m, ell_ang in [-pi/2, pi/2)); a legacy
    frame (no x/y/wind) still predicts; PRE-LAUNCH -> None.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKLE_PATH = ROOT / "model" / "apex1_landing_v2.pkl"
META_PATH = ROOT / "model" / "metadata.json"

GRAVITY = 9.81
# Burn-time prior for the `t_since_boost_end` serving feature (see docstring).
BOOST_TIME_PRIOR = 2.0
# Gust law (must match simulator.GUST_PERIOD / GUST_AMP — the wind model is
# deterministic, so train/serve can always recompute it from t).
GUST_PERIOD = 7.0
GUST_AMP = 0.15
ETA_TARGET = "eta_s"
VIMPACT_TARGET = "v_landing_m_s"
APOGEE_TARGET = "apogee_m"
LAND_X_TARGET = "land_x"
LAND_Y_TARGET = "land_y"
# Altitude bands (m) — must match tools/train_model.py ALT_BANDS.
ALT_BANDS = [(0.0, 100.0, "0-100"), (100.0, 200.0, "100-200"), (200.0, float("inf"), "200-300")]
# Column of the raw baro altitude in FEATURE_NAMES (v1 layout, unchanged).
ALT_COL = 1


def _band_of(alt: float) -> str:
    """Altitude-band label for ellipse/confidence bucketing."""
    for lo, hi, label in ALT_BANDS:
        if lo <= alt < hi:
            return label
    return ALT_BANDS[-1][2]


def _eig2(cxx: float, cxy: float, cyy: float) -> tuple[float, float, float]:
    """Eigen-decomposition of a symmetric 2x2 [[cxx, cxy], [cxy, cyy]].

    Returns (lambda_max, lambda_min, theta) where theta is the angle of
    the eigenvector of the LARGEST eigenvalue, wrapped to [-pi/2, pi/2)
    (an ellipse axis has no sign, so a pi-periodic angle suffices).
    """
    if cxy == 0.0:
        lam_max, lam_min = max(cxx, cyy), min(cxx, cyy)
        theta = 0.0 if cxx >= cyy else math.pi / 2.0
        return lam_max, lam_min, theta
    tr = cxx + cyy
    det = cxx * cyy - cxy * cxy
    disc = math.sqrt(max((cxx - cyy) ** 2 + 4.0 * cxy * cxy, 0.0))
    lam_max = 0.5 * (tr + disc)
    lam_min = 0.5 * (tr - disc)
    # eigenvector of lam_max: (cxx - lam_max, cxy) (or (cxy, cyy - lam_max))
    theta = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    # wrap to [-pi/2, pi/2)
    while theta >= math.pi / 2.0:
        theta -= math.pi
    while theta < -math.pi / 2.0:
        theta += math.pi
    return max(lam_max, 0.0), max(lam_min, 0.0), theta


class LandingPredictor:
    """Loads model + metadata once; `predict(frame, history) -> dict | None`.

    Graceful degradation: if the artifacts are missing or corrupt, the
    constructor logs ONE clear warning (stderr) and every predict() returns
    None — the GCS keeps serving telemetry with `pred` simply absent.
    """

    def __init__(self, pkl_path: Path = PICKLE_PATH, meta_path: Path = META_PATH):
        self._models: dict | None = None
        self._status_mae: dict[str, float] = {}
        self._bucket_mae: dict[str, float] = {}
        self._overall_eta_mae: float = 1.0
        self._ellipse: dict[str, list] = {}
        self._ok = False
        self._warned = False
        try:
            import joblib  # local import: sklearn must not be needed to *read* telemetry
            payload = joblib.load(pkl_path)
            self._models = payload["models"]
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._status_mae = dict(meta["report"]["status_mae_eta"])
            self._bucket_mae = dict(meta["report"].get("bucket_eta_mae", {}))
            self._overall_eta_mae = float(meta["report"]["overall_eta_mae"])
            self._ellipse = {k: [list(row) for row in v]
                             for k, v in meta.get("ellipse", {}).items()}
            self._eta = self._models[ETA_TARGET]
            self._vimpact = self._models[VIMPACT_TARGET]
            self._apogee = self._models[APOGEE_TARGET]
            self._land_x = self._models[LAND_X_TARGET]
            self._land_y = self._models[LAND_Y_TARGET]
            self._ok = True
        except Exception as e:
            # Missing/corrupt model, no metadata, version mismatch, ...
            print(f"[prediction] WARNING: landing model unavailable "
                  f"({type(e).__name__}: {e}) — predictions disabled; "
                  f"run `python tools/train_model.py` to (re)build it.",
                  file=sys.stderr)

    @property
    def loaded(self) -> bool:
        """True once the model + metadata are loaded and usable."""
        return self._ok

    # --- feature builder (must stay identical to tools/train_model.py) ------
    @staticmethod
    def _features(frame, history: list | None) -> list[float]:
        """29 columns, in FEATURE_NAMES order (see tools/train_model.py).

        `history` is a list of recent frames (oldest -> newest) from the
        GCS buffer. The trailing 0.5 s window (25 frames @ 50 Hz), the
        running flight maxima, and the finite-difference lateral velocities
        are reconstructed from the same-flight frames preceding the latest
        one; at flight start they are partial, exactly as in training.
        Legacy frames (x/y/wind None) zero the 8 lateral columns — see the
        module docstring.
        """
        st = frame.status
        hist = history or []
        # only same-flight, older-or-equal frames (skip flight 0 / relaunches)
        same = [f for f in hist
                if f is not frame and f.flight == frame.flight and f.t <= frame.t]
        same.sort(key=lambda f: f.t)
        prev = same[-25:]   # 0.5 s window, same length as the trainer's HIST_LEN
        alts = [f.altitude for f in prev] + [frame.altitude]
        vels = [f.velocity for f in prev] + [frame.velocity]
        accs = [f.accel for f in prev] + [frame.accel]
        m_alt = sum(alts) / len(alts)
        m_vel = sum(vels) / len(vels)
        m_acc = sum(accs) / len(accs)
        # running max of the smoothed-ish altitude over the same flight
        # (the GCS buffer IS the flight's record; partial at flight start)
        alt_hist_max = max([f.altitude for f in same] + [frame.altitude])
        k_active = max(0.0, -m_acc) / max(m_vel ** 2, 1.0)
        # analytic climb priors (identical math to tools/train_model.py)
        in_climb = st in ("BOOST", "ASCENT")
        v_climb = max(m_vel, 0.0) if in_climb else 0.0
        if k_active < 1e-9 or not in_climb:
            climb_t = v_climb / GRAVITY if in_climb else 0.0
            climb_h = (v_climb ** 2) / (2.0 * GRAVITY) if in_climb else 0.0
        else:
            climb_t = 1.0 / k_active * math.acos(1.0 / max(1.0 + k_active * v_climb / GRAVITY, 1.0))
            climb_h = 1.0 / k_active * math.log(1.0 + k_active * v_climb / GRAVITY)
        # --- Phase 4 lateral columns (zeroed for legacy frames) ------------
        fx = frame.x if frame.x is not None else 0.0
        fy = frame.y if frame.y is not None else 0.0
        if same:
            last = same[-1]
            dt = frame.t - last.t
            if dt > 1e-9 and last.x is not None and last.y is not None:
                vx = (fx - last.x) / dt
                vy_lat = (fy - last.y) / dt
            else:  # first lateral frame or legacy pair: no difference possible
                vx = vy_lat = 0.0
        else:
            vx = vy_lat = 0.0
        wind = frame.wind if isinstance(frame.wind, dict) else {}
        w_speed = float(wind.get("speed", 0.0))
        w_dir = math.radians(float(wind.get("dir", 0.0)))
        gust = 1.0 + GUST_AMP * math.sin(2.0 * math.pi * frame.t / GUST_PERIOD)
        wx = w_speed * gust * math.cos(w_dir)
        wy = w_speed * gust * math.sin(w_dir)
        return [
            frame.t,
            frame.altitude,
            frame.velocity,
            frame.accel,
            frame.pressure,
            frame.temperature,
            m_alt,
            m_vel,
            m_acc,
            k_active,   # active quadratic drag coefficient
            1.0 if st == "PRE-LAUNCH" else 0.0,
            1.0 if st == "BOOST" else 0.0,
            1.0 if st == "ASCENT" else 0.0,
            1.0 if st == "DESCENT" else 0.0,
            1.0 if st == "PARACHUTE" else 0.0,
            1.0 if st == "LANDED" else 0.0,
            # t_since_boost_end with the documented burn-time prior (0 while
            # BOOST is reported, exactly as in training; see module docstring).
            max(0.0, frame.t - BOOST_TIME_PRIOR),
            # t_ff (vacuum-freefall ETA prior) from the SMOOTHED altitude,
            # exactly as the trainer does.
            (2.0 * max(m_alt, 0.0) / GRAVITY) ** 0.5,
            climb_t,
            climb_h,
            alt_hist_max,
            # Phase 4 lateral (order = FEATURE_NAMES tail).
            fx, fy, vx, vy_lat, w_speed, wx, wy,
            2.0 * math.pi * frame.t / GUST_PERIOD,
        ]

    # --- confidence + ellipse ----------------------------------------------
    def _bucket(self, status: str, altitude: float) -> str:
        return f"{status}:{_band_of(altitude)}"

    def _confidence(self, status: str, altitude: float) -> float:
        # conf = clamp(1 - eta_MAE_bucket / 2, 0, 1): the measured eta error
        # of the held-out rows in the current (status, altitude-band) bucket,
        # falling back to the per-status MAE, then the overall MAE. See the
        # module docstring; the numbers come from metadata.json, never a
        # hard-coded constant.
        mae = self._bucket_mae.get(self._bucket(status, altitude))
        if mae is None:
            mae = self._status_mae.get(status, self._overall_eta_mae)
        return min(1.0, max(0.0, 1.0 - max(mae, 0.0) / 2.0))

    def _ellipse_axes(self, status: str, altitude: float) -> tuple[float, float, float]:
        """1-sigma ellipse (ell_a >= ell_b, ell_ang in [-pi/2, pi/2)) from
        the stored residual covariance of the current bucket, falling back
        to the OVERALL covariance (see the module docstring)."""
        cov = self._ellipse.get(self._bucket(status, altitude)) \
            or self._ellipse.get("OVERALL")
        if not cov:
            # No metadata at all (should not happen with a loaded model):
            # a small neutral ellipse rather than a crash.
            return 1.0, 1.0, 0.0
        cxx, cxy = cov[0][0], cov[0][1]
        cyy = cov[1][1]
        lam_max, lam_min, theta = _eig2(cxx, cxy, cyy)
        return math.sqrt(lam_max), math.sqrt(lam_min), theta

    def predict(self, frame, history: list | None = None) -> dict | None:
        """Predict landing for the latest frame. `history` (recent same-flight
        frames, oldest -> newest) supplies the 0.5 s alt/vel window and the
        lateral velocity difference — see the module docstring.
        """
        if not self._ok:
            return None
        if frame.status in ("PRE-LAUNCH", "LANDED"):
            return None
        try:
            x = self._features(frame, history)
            eta = float(self._eta.predict([x])[0])
            v_impact = float(self._vimpact.predict([x])[0])
            apogee = float(self._apogee.predict([x])[0])
            land_x = float(self._land_x.predict([x])[0])
            land_y = float(self._land_y.predict([x])[0])
        except Exception as e:
            if not self._warned:
                self._warned = True
                print(f"[prediction] WARNING: model inference failed "
                      f"({type(e).__name__}: {e}) — predictions disabled.",
                      file=sys.stderr)
            return None
        ell_a, ell_b, ell_ang = self._ellipse_axes(frame.status, frame.altitude)
        return {
            "eta": max(0.0, eta),
            "v_impact": v_impact,
            "apogee": max(0.0, apogee),
            "conf": self._confidence(frame.status, frame.altitude),
            "land_x": land_x,
            "land_y": land_y,
            "ell_a": ell_a,
            "ell_b": ell_b,
            "ell_ang": ell_ang,
        }


if __name__ == "__main__":
    import random
    import protocol as proto
    from simulator import FlightParams, FlightSimulator

    p = LandingPredictor()

    def _drive_to(sim: FlightSimulator, t_target: float):
        """Run a fresh default-profile flight up to t_target and return
        (frame, history) — a REAL mid-flight snapshot: the frames are built
        from the simulator's own sensor model (incl. the Phase 4 lateral
        x/y/wind fields, as the wire now carries them), and history is the
        full same-flight buffer the GCS would have (50 Hz frames).
        """
        history = []

        def _frame(s: dict, t: float, status: str) -> proto.TelemetryFrame:
            return proto.TelemetryFrame(
                t=round(t, 3), flight=sim.flight, status=status,
                altitude=s["altitude"], velocity=s["velocity"],
                accel=s["accel"], pressure=s["pressure"],
                temperature=s["temperature"],
                x=round(sim.x, 3), y=round(sim.y_lat, 3),
                wind={"speed": sim.p.wind_speed, "dir": sim.p.wind_dir_deg},
            )

        # the PRE-LAUNCH frame (what the GCS buffer holds from the launch)
        s0 = sim.sensor_readings()
        history.append(_frame(s0, 0.0, "PRE-LAUNCH"))
        while sim.t < t_target and not sim.landed:
            sim.step()
            s = sim.sensor_readings()
            history.append(_frame(s, sim.t, sim.status()))
        # the "latest" frame: one more step, exactly as the wire would carry it
        sim.step()
        s = sim.sensor_readings()
        latest = _frame(s, sim.t, sim.status())
        return latest, history

    def _check(out: dict, label: str, eta_lo: float, eta_hi: float) -> None:
        assert out is not None, f"{label}: predict() returned None"
        assert eta_lo < out["eta"] < eta_hi, \
            f"{label}: eta {out['eta']:.2f} outside [{eta_lo}, {eta_hi}]"
        assert out["ell_a"] >= out["ell_b"] >= 0.3, \
            f"{label}: ellipse semi-axes {out['ell_a']:.2f}/{out['ell_b']:.2f} " \
            f"not sane (need a >= b >= 0.3 m)"
        assert -math.pi / 2.0 <= out["ell_ang"] < math.pi / 2.0, \
            f"{label}: ell_ang {out['ell_ang']:.3f} outside [-pi/2, pi/2)"
        print(f"  {label}: eta={out['eta']:.2f} s  v_impact={out['v_impact']:.1f} m/s  "
              f"apogee={out['apogee']:.1f} m  conf={out['conf']:.2f}")
        print(f"    site ({out['land_x']:.1f}, {out['land_y']:.1f}) m   "
              f"ellipse a={out['ell_a']:.2f} b={out['ell_b']:.2f} m "
              f"ang={math.degrees(out['ell_ang']):.1f} deg")

    random.seed(7)
    p2 = FlightSimulator(FlightParams())
    p2.flight = 1
    mid, mid_hist = _drive_to(p2, 12.0)
    out = p.predict(mid, mid_hist)
    print(f"self-check: real mid-flight frame (t={mid.t:.2f}s, "
          f"{mid.altitude:.0f} m, {mid.velocity:.1f} m/s, {mid.status}, "
          f"x={mid.x:.1f} m) with {len(mid_hist)}-frame history")
    _check(out, "DESCENT ", 3.5, 9.0)

    p4 = FlightSimulator(FlightParams())
    p4.flight = 1
    par, par_hist = _drive_to(p4, 16.0)
    par_out = p.predict(par, par_hist)
    print(f"self-check: real mid-flight frame (t={par.t:.2f}s, "
          f"{par.altitude:.0f} m, {par.velocity:.1f} m/s, {par.status}, "
          f"x={par.x:.1f} m) with {len(par_hist)}-frame history")
    assert par.status == "PARACHUTE", f"expected PARACHUTE, got {par.status}"
    _check(par_out, "PARACHUTE", 0.5, 5.0)
    # (The live DESCENT -> PARACHUTE shrink is asserted over whole phases in
    # the GCS E2E; a single-frame comparison here is too noisy to gate on.)
    print(f"    ellipse a: DESCENT {out['ell_a']:.2f} m -> PARACHUTE "
          f"{par_out['ell_a']:.2f} m")

    # Legacy frame: no x/y/wind — vertical targets stay valid, the site
    # degrades to the windless answer (documented reduced meaning).
    legacy = proto.TelemetryFrame(
        t=12.0, flight=1, status="DESCENT",
        altitude=110.0, velocity=-38.0, accel=0.5,
        pressure=868.0, temperature=7.9,
        x=None, y=None, wind=None,
    )
    legacy_out = p.predict(legacy, [])
    print("self-check: legacy frame (x/y/wind = None) -> "
          f"eta={legacy_out['eta']:.2f} s apogee={legacy_out['apogee']:.1f} m "
          f"site=({legacy_out['land_x']:.1f}, {legacy_out['land_y']:.1f}) m "
          f"(degraded: windless features, vertical targets valid)")
    assert legacy_out is not None
    assert 2.0 < legacy_out["eta"] < 12.0
    keys = set(legacy_out)
    assert keys == {"eta", "v_impact", "apogee", "conf",
                    "land_x", "land_y", "ell_a", "ell_b", "ell_ang"}, \
        f"pred schema changed: {sorted(keys)}"

    pre = proto.TelemetryFrame(
        t=0.0, flight=1, status="PRE-LAUNCH",
        altitude=0.0, velocity=0.0, accel=9.8,
        pressure=1013.2, temperature=15.0,
    )
    pre_out = p.predict(pre, [])
    print("self-check: PRE-LAUNCH frame ->", pre_out)
    assert pre_out is None

    print("self-check: PASS")
