"""Wire protocol for APEX-1 telemetry.

Single source of truth for the UDP/JSON contract shared by the vehicle
(`simulator.py`, the ESP32 stand-in) and the ground station (`gcs.py`). Both
import this module so they always agree on the port and the frame schema —
change a field here and both ends update together.

Phase 4 (backward compatible, strict): the frame gains three NEW optional
fields — `x`, `y`, `wind` — APPENDED after the original eight (which keep
their exact names, order, and types) and BEFORE the optional `pred`:

  * `x`    [m]  downwind position (simulated ground truth; the vehicle has
                no GPS — the "flight computer" estimates it).
  * `y`    [m]  crosswind position.
  * `wind`      {"speed": m/s, "dir": deg} — the wind state the vehicle is
                flying in (needed by the GCS-side landing-site model).

Each is `None` when absent (legacy senders: the ESP32 firmware never sends
them — its 8-key snprintf contract is unchanged). `to_json` omits any of the
three when `None` (lean legacy frames, byte-identical to the pre-Phase-4
output); `from_json` maps missing keys to `None`. Old firmware JSON, old
NDJSON mission lines, and `pred`-less frames all parse unchanged, and the
original eight keys ALWAYS serialize first, in their original order.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# --- Link configuration ---------------------------------------------------
PORT = 5551                          # UDP port the vehicle transmits on

# Flight status phases, in the order a flight progresses through them.
STATUS_PRE_LAUNCH = "PRE-LAUNCH"
STATUS_BOOST = "BOOST"
STATUS_ASCENT = "ASCENT"
STATUS_DESCENT = "DESCENT"
STATUS_PARACHUTE = "PARACHUTE"
STATUS_LANDED = "LANDED"


@dataclass
class TelemetryFrame:
    """One telemetry sample as it crosses the wire.

    These are the *sensor* readings (noisy) — exactly what a real ground
    station ever sees. Units: t [s], altitude [m], velocity [m/s],
    accel [m/s^2], pressure [hPa], temperature [deg C].

    `accel` is *specific force* (proper acceleration), the raw vertical IMU
    reading: ~+9.81 m/s^2 at rest, ~thrust during boost, ~0 in freefall.
    Net (kinematic) acceleration is specific force minus gravity.

    Phase 4 lateral fields (see module docstring): `x` [m] downwind,
    `y` [m] crosswind, `wind` {"speed": m/s, "dir": deg}. All optional —
    `None` on legacy frames (the ESP32 firmware never sends them), omitted
    from the JSON when `None`, and mapped back to `None` when absent.

    `pred` (optional, Phase 3/4) — GCS-side landing prediction attached to
    the frame *before the WebSocket push only*. Keys, when present
    (all optional floats):
      eta      [s until landing]
      v_impact [m/s]
      apogee   [m]
      conf     [0..1]
      land_x   [m]  predicted landing point, downwind
      land_y   [m]  predicted landing point, crosswind
      ell_a    [m]  1-sigma confidence ellipse, major semi-axis
      ell_b    [m]  1-sigma confidence ellipse, minor semi-axis (a >= b)
      ell_ang  [rad] orientation of the major axis (in [-pi/2, pi/2);
                    the ellipse axes are the principal axes of the
                    landing-point residual covariance, so the HUD rotates
                    the ellipse by ell_ang to draw it)
    The vehicle (simulator, ESP32 firmware) NEVER emits it: `pred` is
    strictly additive. Old frames simply lack the key -> `None`; frames
    with `pred=None` serialize WITHOUT the key (lean wire, byte-identical
    to the pre-Phase-3 format).
    """
    t: float
    flight: int
    status: str
    altitude: float
    velocity: float
    accel: float
    pressure: float
    temperature: float
    x: float | None = None           # downwind position [m] (None = legacy sender)
    y: float | None = None           # crosswind position [m] (None = legacy sender)
    wind: dict | None = None         # {"speed": m/s, "dir": deg} (None = legacy)
    pred: dict | None = None

    def to_json(self) -> str:
        d = asdict(self)
        # Optional additive fields: omitted when None so legacy frames stay
        # lean (byte-identical to the pre-Phase-4 wire format). The original
        # 8 keys always come first, in their original order.
        for key in ("x", "y", "wind", "pred"):
            if d[key] is None:
                d.pop(key)
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TelemetryFrame":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        pred = d.pop("pred", None)  # missing key -> None (pre-Phase-3 frames)
        if pred is not None and not isinstance(pred, dict):
            pred = None             # tolerate malformed enrichment
        # Phase 4 additive fields: missing key -> None (legacy frames, old
        # NDJSON mission lines, ESP32 firmware JSON all parse unchanged).
        for key in ("x", "y", "wind"):
            d.setdefault(key, None)
        return cls(**d, pred=pred)
