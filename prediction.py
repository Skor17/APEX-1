"""APEX-1 landing predictor — GCS-side inference for the `pred` wire field.

Loads the Phase 3 model artifacts once at import time:

    model/apex1_landing_v1.pkl    three HistGradientBoosting estimators
                                  (eta_s, v_landing_m_s, apogee_m)
    model/metadata.json           feature order, distributions, test MAE
                                  report, per-status eta MAE profile

`LandingPredictor.predict(frame, history)` takes the latest `TelemetryFrame`
plus a short rolling history (the GCS buffer) and returns:

    {"eta": <s>, "v_impact": <m/s>, "apogee": <m>, "conf": <0..1>}

or `None` when it has nothing to say:

  * status PRE-LAUNCH or LANDED — no landing in the future to predict,
  * the model file is missing/corrupt — one clear warning is logged and the
    predictor degrades to `None` forever (the GCS must never crash on it).

History usage (documented per spec): the model is trained on a 21-column
feature vector built from the latest frame PLUS the GCS rolling buffer of
the same flight (see tools/train_model.py FEATURE_NAMES — the trainer and
this module MUST be changed together):

  * `alt_hist` / `vel_hist` / `acc_hist` — mean altitude / velocity /
    specific force over a trailing 0.5 s window (25 frames @ 50 Hz).
    Smoothing matters: the baro altitude is the noisiest input (~8 m per
    frame) and the biggest error source for the apogee target (a 0.5 s
    mean drops that to ~1.6 m), and the 0.3 m/s^2 accelerometer noise
    alone swamps the single-frame drag signal.
  * `k_active` = -acc_hist / vel_hist^2 — the active quadratic drag
    coefficient (sign-consistent climbing and falling).
  * `climb_prior` / `climb_h` — analytic vacuum-with-current-drag estimates
    of the time / altitude still remaining to apogee while the rocket is
    (climbing in BOOST/ASCENT), 0 otherwise. With `alt_hist_max` (the
    running max of the same-flight buffer) they anchor the apogee target
    in every phase.
  * `t_ff` — the vacuum-freefall ETA prior from the smoothed altitude.

At serving time all of these are reconstructed from the most recent
history frames of the SAME flight (the GCS passes the last ~25 of its
1200-frame rolling buffer); a partial window (flight start) is exactly
what the trainer saw, so train and serve stay identical. The window is
short enough that it adds no perceptible lag to the countdown.

One documented approximation: the training value of `t_since_boost_end` is
`t - boost_time`, and the GCS does not transmit `boost_time`. At serving
time the predictor uses the vehicle's configured burn time
(BOOST_TIME_PRIOR = 2.0 s — the fixed burn in the simulator defaults and
`firmware/config.h`). While BOOST is reported the value is exactly 0 (as in
training); afterwards it carries at most a ±0.5 s offset versus the value
seen in training (the generation distribution is 1.5–2.5 s), which the tree
ensemble absorbs (it also sees `t` and the status one-hot). For the real
vehicle the offset is exactly 0, because the burn time is fixed.

Confidence — honest, derived from the test set, not a constant:
`conf` maps the *measured* eta error profile of the test flights onto a
0..1 scale. The trainer records, per status, the mean absolute eta error
observed on held-out rows (`report.status_mae_eta`). We take the MAE for
the current status, fall back to the overall test MAE, and convert

    conf = 1 / (1 + status_mae_eta_s)

i.e. a 0.3 s typical error (DESCENT/PARACHUTE — landing time is well
determined by altitude + phase) gives conf ~0.77, while a multi-second
typical error (ASCENT — the outcome still depends on the whole remaining
trajectory: apogee, chute deploy, drag) gives a proportionally lower
confidence. The mapping is monotone and unit-honest: `conf` is "1 s of
expected error per unit of confidence" — a simple, explainable choice that
decays exactly where the test data says the prediction is weakest.

Self-check:  python prediction.py
    loads the model and prints a prediction for a hand-crafted mid-descent
    frame (t=12 s, ~120 m, ~-30 m/s) plus one PRE-LAUNCH frame (None).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PICKLE_PATH = ROOT / "model" / "apex1_landing_v1.pkl"
META_PATH = ROOT / "model" / "metadata.json"

GRAVITY = 9.81
# Burn-time prior for the `t_since_boost_end` serving feature (see docstring).
BOOST_TIME_PRIOR = 2.0
ETA_TARGET = "eta_s"
VIMPACT_TARGET = "v_landing_m_s"
APOGEE_TARGET = "apogee_m"


class LandingPredictor:
    """Loads model + metadata once; `predict(frame, history) -> dict | None`.

    Graceful degradation: if the artifacts are missing or corrupt, the
    constructor logs ONE clear warning (stderr) and every predict() returns
    None — the GCS keeps serving telemetry with `pred` simply absent.
    """

    def __init__(self, pkl_path: Path = PICKLE_PATH, meta_path: Path = META_PATH):
        self._models: dict | None = None
        self._status_mae: dict[str, float] = {}
        self._overall_eta_mae: float = 1.0
        self._ok = False
        self._warned = False
        try:
            import joblib  # local import: sklearn must not be needed to *read* telemetry
            payload = joblib.load(pkl_path)
            self._models = payload["models"]
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            self._status_mae = dict(meta["report"]["status_mae_eta"])
            self._overall_eta_mae = float(meta["report"]["overall_eta_mae"])
            self._eta = self._models[ETA_TARGET]
            self._vimpact = self._models[VIMPACT_TARGET]
            self._apogee = self._models[APOGEE_TARGET]
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
        """21 columns, in FEATURE_NAMES order (see tools/train_model.py).

        `history` is a list of recent frames (oldest -> newest) from the GCS
        buffer. The trailing 0.5 s window (25 frames @ 50 Hz) and the running
        flight maxima are reconstructed from the same-flight frames preceding
        the latest one; at flight start they are partial, exactly as in
        training.
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
        ]

    # --- confidence ----------------------------------------------------------
    def _confidence(self, status: str) -> float:
        # conf = 1 / (1 + measured per-status eta MAE on the test flights).
        # See module docstring for the rationale; the numbers come from
        # metadata.json, never a hard-coded constant.
        mae = self._status_mae.get(status, self._overall_eta_mae)
        return 1.0 / (1.0 + max(mae, 0.0))

    def predict(self, frame, history: list | None = None) -> dict | None:
        """Predict landing for the latest frame. `history` (recent same-flight
        frames, oldest -> newest) supplies the 0.5 s alt/vel window — see the
        module docstring.
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
        except Exception as e:
            if not self._warned:
                self._warned = True
                print(f"[prediction] WARNING: model inference failed "
                      f"({type(e).__name__}: {e}) — predictions disabled.",
                      file=sys.stderr)
            return None
        return {
            "eta": max(0.0, eta),
            "v_impact": v_impact,
            "apogee": max(0.0, apogee),
            "conf": min(1.0, max(0.0, self._confidence(frame.status))),
        }


if __name__ == "__main__":
    import protocol as proto
    from simulator import FlightParams, FlightSimulator

    p = LandingPredictor()

    def _drive_to(sim: FlightSimulator, t_target: float):
        """Run a fresh default-profile flight up to t_target and return
        (frame, history) — a REAL mid-flight snapshot: the frame is built
        from the simulator's own sensor model at t_target, and history is
        the full same-flight buffer the GCS would have (50 Hz frames).
        """
        history = []
        # the PRE-LAUNCH frame (what the GCS buffer holds from the launch)
        s0 = sim.sensor_readings()
        history.append(proto.TelemetryFrame(
            t=0.0, flight=1, status="PRE-LAUNCH",
            altitude=s0["altitude"], velocity=s0["velocity"],
            accel=s0["accel"], pressure=s0["pressure"],
            temperature=s0["temperature"]))
        while sim.t < t_target and not sim.landed:
            sim.step()
            s = sim.sensor_readings()
            history.append(proto.TelemetryFrame(
                t=round(sim.t, 3), flight=1, status=sim.status(),
                altitude=s["altitude"], velocity=s["velocity"],
                accel=s["accel"], pressure=s["pressure"],
                temperature=s["temperature"]))
        # the "latest" frame: one more step, exactly as the wire would carry it
        sim.step()
        s = sim.sensor_readings()
        latest = proto.TelemetryFrame(
            t=round(sim.t, 3), flight=1, status=sim.status(),
            altitude=s["altitude"], velocity=s["velocity"],
            accel=s["accel"], pressure=s["pressure"],
            temperature=s["temperature"])
        return latest, history

    p2 = FlightSimulator(FlightParams())
    p2.flight = 1
    mid, mid_hist = _drive_to(p2, 12.0)
    out = p.predict(mid, mid_hist)
    print(f"self-check: real mid-flight frame (t={mid.t:.2f}s, "
          f"{mid.altitude:.0f} m, {mid.velocity:.1f} m/s, {mid.status}) "
          f"with {len(mid_hist)}-frame history")
    if out is None:
        print("  -> None (model not loaded — see warning above)")
        sys.exit(1)
    print(f"  eta={out['eta']:.2f} s   v_impact={out['v_impact']:.2f} m/s   "
          f"apogee={out['apogee']:.1f} m   conf={out['conf']:.2f}")
    # True values for the default profile: apogee 210.1 m @ ~9.5 s, landing
    # 18.50 s, impact -6.9 m/s; at t~12.05 s the rocket is ~109 m / -42.5 m/s
    # DESCENT (above the 50 m chute line) -> true eta ~6.4 s.
    assert 3.5 < out["eta"] < 9.0, f"eta {out['eta']} outside sanity band"
    assert -15.0 < out["v_impact"] < 0.0, f"v_impact {out['v_impact']} outside band"
    assert 180.0 < out["apogee"] < 240.0, f"apogee {out['apogee']} outside band"

    pre = proto.TelemetryFrame(
        t=0.0, flight=1, status="PRE-LAUNCH",
        altitude=0.0, velocity=0.0, accel=9.8,
        pressure=1013.2, temperature=15.0,
    )
    pre_out = p.predict(pre, [])
    print("self-check: PRE-LAUNCH frame ->", pre_out)
    assert pre_out is None

    # Early-flight frame: the apogee is still open (boost-time unknown) —
    # a wide, honest band, not a fake-precise one.
    p3 = FlightSimulator(FlightParams())
    p3.flight = 1
    early, early_hist = _drive_to(p3, 3.0)
    early_out = p.predict(early, early_hist)
    print(f"self-check: early frame (t={early.t:.2f}s, {early.status}) -> "
          f"eta={early_out['eta']:.2f} s apogee={early_out['apogee']:.0f} m "
          f"conf={early_out['conf']:.2f}" if early_out else "  -> None")
    assert early_out is not None
    assert 90.0 < early_out["apogee"] < 330.0, \
        f"early apogee {early_out['apogee']} outside band"
    assert 12.0 < early_out["eta"] < 17.0, \
        f"early eta {early_out['eta']} outside band"
    print("self-check: PASS")
