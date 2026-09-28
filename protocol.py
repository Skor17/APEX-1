"""Wire protocol for APEX-1 telemetry.

Single source of truth for the UDP/JSON contract shared by the vehicle
(`simulator.py`, the ESP32 stand-in) and the ground station (`gcs.py`). Both
import this module so they always agree on the port and the frame schema —
change a field here and both ends update together.
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

    `pred` (optional, Phase 3) — GCS-side landing prediction attached to
    the frame *before the WebSocket push only*. Keys, when present:
    `eta` [s until landing], `v_impact` [m/s], `apogee` [m], `conf` [0..1].
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
    pred: dict | None = None

    def to_json(self) -> str:
        d = asdict(self)
        if d["pred"] is None:
            d.pop("pred")          # keep old-style frames lean & byte-identical
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TelemetryFrame":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        d = json.loads(raw)
        pred = d.pop("pred", None)  # missing key -> None (pre-Phase-3 frames)
        if pred is not None and not isinstance(pred, dict):
            pred = None             # tolerate malformed enrichment
        return cls(**d, pred=pred)
