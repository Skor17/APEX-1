"""APEX-1 mission recorder — capture telemetry to NDJSON mission files.

A *mission file* is NDJSON: one telemetry frame per line with exactly the
same 8 keys, in the same order, as the wire format (see protocol.py):

    t, flight, status, altitude, velocity, accel, pressure, temperature

One file = one complete flight: it starts PRE-LAUNCH, ends LANDED, and
includes the ~1.5 s on-pad hold after landing, so the HUD's MISSION LOG
panel can replay it frame-for-frame and loop cleanly.

Two capture modes
-----------------
Mode A (default) — record the live UDP stream:

    python tools/record.py

Binds UDP/5551 and writes every valid frame to
``missions/apex1_flight_NNN.ndjson`` (NNN = the vehicle's flight number,
zero-padded). A *flight boundary* — the frame ``flight`` number changing —
closes the current file and starts the next one. Frames that fail key
validation are skipped with a warning. Ctrl+C shuts down cleanly (closes
the file and prints its path). Joining mid-flight captures from the first
frame you receive.

Note: ``gcs.py`` owns UDP/5551 while it runs, so stop the GCS first to
capture on the wire (or just press ● REC in the HUD's MISSION LOG panel —
the browser records any session, live, demo, or replay, with no network).

Mode B (``--demo``) — no network at all:

    python tools/record.py --demo --count 1 --seed 7

Runs the in-process ``FlightSimulator`` from ``simulator.py`` (reusing its
``step()`` / ``sensor_readings()`` — no duplicated physics) and writes the
same NDJSON. This is how the committed ``web/missions/apex1_flight_001.ndjson``
is generated.
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import sys
from dataclasses import asdict
from pathlib import Path
from typing import IO

# Allow `python tools/record.py` from anywhere: the repo root (protocol.py,
# simulator.py) must be importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import protocol as proto
from simulator import FlightSimulator

ROOT = Path(__file__).resolve().parent.parent


def frame_line(frame: proto.TelemetryFrame, nd: int = 3) -> str:
    """One frame as a compact NDJSON line (same 8 keys, same order as the wire)."""
    return json.dumps(asdict(frame), separators=(",", ":"))


def _frame_from_sim(sim: FlightSimulator, nd: int = 3) -> proto.TelemetryFrame:
    """Build a wire-format frame from the simulator's current state.

    Mirrors FlightSimulator._emit(): same sensor_readings(), same rounding
    (`nd` decimal places; 3 matches the wire exactly).
    """
    s = sim.sensor_readings()
    return proto.TelemetryFrame(
        t=round(sim.t, nd),
        flight=sim.flight,
        status=sim.status(),
        altitude=round(s["altitude"], nd),
        velocity=round(s["velocity"], nd),
        accel=round(s["accel"], nd),
        pressure=round(s["pressure"], nd),
        temperature=round(s["temperature"], nd),
    )


def record_demo(count: int, out_dir: Path, nd: int = 3) -> list[Path]:
    """Mode B: run `count` in-process flight(s), one NDJSON file each."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sim = FlightSimulator()
    written: list[Path] = []
    pad_steps = int(round(sim.p.relaunch_pause / sim.p.dt))
    for _ in range(count):
        sim.flight += 1
        sim._reset()
        path = out_dir / f"apex1_flight_{sim.flight:03d}.ndjson"
        lines = 0
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(frame_line(_frame_from_sim(sim, nd)) + "\n")
            lines += 1                                   # PRE-LAUNCH frame
            while not sim.landed:
                sim.step()
                fh.write(frame_line(_frame_from_sim(sim, nd)) + "\n")
                lines += 1
            # On-pad hold: keep streaming LANDED frames for relaunch_pause
            # seconds (t keeps advancing, state frozen) so a replay rests on
            # the pad before looping — same behavior as the HUD's demo engine.
            for _ in range(pad_steps):
                sim.t += sim.p.dt
                fh.write(frame_line(_frame_from_sim(sim, nd)) + "\n")
                lines += 1
        written.append(path)
        print(f"recorded flight {sim.flight:03d}: {lines} frames -> {path}")
    return written


def record_udp(out_dir: Path, port: int) -> list[Path]:
    """Mode A: bind UDP and capture the live stream, one file per flight."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"cannot bind UDP port {port}: {e}", file=sys.stderr)
        print("another process (gcs.py?) is holding it — stop it and retry.",
              file=sys.stderr)
        return []
    sock.settimeout(0.5)
    print(f"recording UDP/{port} -> {out_dir}   (Ctrl+C to stop)")

    cur_flight: int | None = None
    fh: IO[str] | None = None
    cur_path: Path | None = None
    total = 0
    skipped = 0
    closed: list[Path] = []
    try:
        while True:
            try:
                raw, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                frame = proto.TelemetryFrame.from_json(raw)
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                skipped += 1
                if skipped <= 5 or skipped % 100 == 0:
                    print(f"  [warn] skipped {skipped} bad frame(s), last: "
                          f"{raw[:64]!r}", file=sys.stderr)
                continue
            if frame.flight != cur_flight:  # flight boundary (or first frame)
                if fh is not None:
                    fh.close()
                    print(f"flight {cur_flight:03d} closed -> {cur_path}")
                    closed.append(cur_path)
                cur_flight = frame.flight
                cur_path = out_dir / f"apex1_flight_{cur_flight:03d}.ndjson"
                fh = open(cur_path, "w", encoding="utf-8")
                print(f"flight {cur_flight:03d} -> {cur_path}")
            fh.write(frame_line(frame) + "\n")
            fh.flush()  # a crash must not eat the capture
            total += 1
    except KeyboardInterrupt:
        print()
    finally:
        if fh is not None:
            fh.close()
            print(f"flight {cur_flight:03d} closed "
                  f"({total} frames this session) -> {cur_path}")
            closed.append(cur_path)
        sock.close()
    return closed


def main() -> None:
    ap = argparse.ArgumentParser(
        description="APEX-1 mission recorder — captures telemetry to NDJSON "
                    "mission files (see module docstring).",
    )
    ap.add_argument("--demo", action="store_true",
                    help="record in-process simulator flight(s) instead of the "
                         "UDP stream (no network needed)")
    ap.add_argument("--count", type=int, default=1,
                    help="--demo: number of flights to record (default 1)")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed the sensor-noise RNG for deterministic output")
    ap.add_argument("--round", dest="nd", type=int, default=3,
                    help="--demo: decimal places in the output (default 3, "
                         "matching the wire; lower values shrink the file)")
    ap.add_argument("--port", type=int, default=proto.PORT,
                    help=f"UDP port to record from (default {proto.PORT})")
    ap.add_argument("--out", type=Path, default=ROOT / "missions",
                    help="output directory (default: <repo>/missions)")
    args = ap.parse_args()

    if args.count < 1:
        ap.error("--count must be >= 1")
    if args.seed is not None:
        random.seed(args.seed)

    if args.demo:
        record_demo(args.count, args.out, nd=args.nd)
    else:
        record_udp(args.out, args.port)


if __name__ == "__main__":
    main()
