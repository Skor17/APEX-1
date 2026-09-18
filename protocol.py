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
    """
    t: float
    flight: int
    status: str
    altitude: float
    velocity: float
    accel: float
    pressure: float
    temperature: float

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | bytes) -> "TelemetryFrame":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return cls(**json.loads(raw))
