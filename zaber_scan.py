#!/usr/bin/env python3
"""Back-and-forth repeatability test for one axis of a Zaber device.

Homes the axis, moves between two positions (mm) at a given speed (mm/s),
waits a settle pause after every move, then reads the encoder (`encoder.pos`)
until two consecutive reads agree, and the controller's position counter
(`pos`). The error is encoder minus commanded; without an encoder it is counter
minus commanded and is labelled so. Each arrival is tagged with the direction
it came from, so the spread is reported per direction and backlash separately.
Results go to a CSV file. Works with a Zaber Virtual Device (cloud ID) or a
device on a serial port. Independent example, not affiliated with Zaber.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable

CSV_FIELDS = [
    "move", "cycle", "label", "direction", "commanded_mm", "counter_mm", "encoder_mm",
    "error_mm", "error_basis", "reads", "stable", "within_tolerance", "duration_s",
]
PLACEHOLDER_SERIAL = 0xFFFFFFFF  # what a device without a programmed serial reports


@dataclass(frozen=True)
class Move:
    index: int      # 1-based position in the sequence
    cycle: int      # 1-based repetition
    label: str      # "start" or "end"
    target_mm: float


def plan_moves(start_mm: float, end_mm: float, cycles: int) -> list[Move]:
    """Move list: start, end, start, end, ... for `cycles` repetitions."""
    if cycles < 1:
        raise ValueError("cycles must be at least 1")
    if start_mm == end_mm:
        raise ValueError("start and end positions must differ")
    moves: list[Move] = []
    for cycle in range(1, cycles + 1):
        for label, target in (("start", start_mm), ("end", end_mm)):
            moves.append(Move(len(moves) + 1, cycle, label, target))
    return moves


def check_within_travel(moves: Iterable[Move], min_mm: float, max_mm: float) -> None:
    for m in moves:
        if not (min_mm <= m.target_mm <= max_mm):
            raise ValueError(
                f"target {m.target_mm} mm is outside the axis travel "
                f"[{min_mm:.3f}, {max_mm:.3f}] mm"
            )


def within_tolerance(commanded_mm: float, measured_mm: float, tolerance_mm: float) -> bool:
    return abs(measured_mm - commanded_mm) <= tolerance_mm


def min_duration_s(moves: Iterable[Move], home_mm: float, speed_mm_s: float) -> float:
    """Travel time at constant speed from the home position; ignores acceleration."""
    if speed_mm_s <= 0:
        raise ValueError("speed must be positive")
    pos, total = home_mm, 0.0
    for m in moves:
        total += abs(m.target_mm - pos) / speed_mm_s
        pos = m.target_mm
    return total


def measured_mm(row: dict) -> float:
    """The position the error is based on: the encoder when it was read, else the counter."""
    return row["encoder_mm"] if row["encoder_mm"] is not None else row["counter_mm"]


def arrival_direction(previous_mm: float, target_mm: float) -> str:
    """Direction the axis was moving when it reached `target_mm`."""
    if target_mm > previous_mm:
        return "forward"
    if target_mm < previous_mm:
        return "backward"
    return "none"


def read_stable(read, stable_mm: float, max_reads: int) -> tuple[float, int, bool]:
    """Read until two consecutive values agree within `stable_mm`.

    Returns (last value, number of reads, whether it became stable). Same idea
    as the stable-reading loop in Zaber's accuracy example.
    """
    value = read()
    reads = 1
    while reads < max_reads:
        previous, value = value, read()
        reads += 1
        if abs(value - previous) <= stable_mm:
            return value, reads, True
    return value, reads, False


def summarize(rows: list[dict]) -> dict:
    """Max |error|, and per position and arrival direction the peak-to-peak spread.

    Spread mixes no approach directions: `groups[(label, direction)]` holds only
    arrivals from that direction (spread is None with a single arrival).
    `backlash_mm[label]` = mean(arrived backward) - mean(arrived forward), only
    when both exist.
    """
    if not rows:
        return {"moves": 0, "max_abs_error_mm": 0.0, "groups": {}, "backlash_mm": {}, "failed": 0}
    values: dict[tuple[str, str], list[float]] = {}
    for r in rows:
        if r["direction"] in ("forward", "backward"):
            values.setdefault((r["label"], r["direction"]), []).append(measured_mm(r))
    groups = {k: {"n": len(v), "mean": sum(v) / len(v), "spread": (max(v) - min(v)) if len(v) > 1 else None}
              for k, v in values.items()}
    backlash = {label: groups[(label, "backward")]["mean"] - groups[(label, "forward")]["mean"]
                for label in {k[0] for k in groups}
                if (label, "backward") in groups and (label, "forward") in groups}
    return {
        "moves": len(rows),
        "max_abs_error_mm": max(abs(r["error_mm"]) for r in rows),
        "groups": groups,
        "backlash_mm": backlash,
        "failed": sum(1 for r in rows if not r["within_tolerance"]),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Repeatability test on one Zaber axis: home, move back and forth, "
                    "read back positions, write a CSV.")
    conn = p.add_argument_group("connection (not needed with --dry-run)")
    conn.add_argument("--cloud-id", default=os.environ.get("ZABER_CLOUD_ID"),
                      help="Virtual Device cloud ID (env ZABER_CLOUD_ID)")
    conn.add_argument("--token", default=os.environ.get("ZABER_TOKEN"),
                      help="access token with IoT scope; omit for guest devices (env ZABER_TOKEN)")
    conn.add_argument("--port", default=os.environ.get("ZABER_PORT"),
                      help="serial port of a physical device, e.g. /dev/tty.usbserial-X (env ZABER_PORT)")
    p.add_argument("--device", type=int, default=1, help="device number in detection order, 1-based (default 1)")
    p.add_argument("--axis", type=int, default=1, help="axis number on that device, 1-based (default 1)")
    p.add_argument("--start-mm", type=float, default=5.0, help="first position in mm (default 5)")
    p.add_argument("--end-mm", type=float, default=45.0, help="second position in mm (default 45)")
    p.add_argument("--cycles", type=int, default=5, help="number of start-end round trips (default 5)")
    p.add_argument("--speed-mm-s", type=float, default=20.0, help="move speed in mm/s (default 20)")
    p.add_argument("--tolerance-mm", type=float, default=0.01,
                   help="max |error| in mm (encoder minus commanded) to count a move as good (default 0.01)")
    p.add_argument("--settle-ms", type=float, default=200.0,
                   help="pause after each move before reading, in ms (default 200)")
    p.add_argument("--stable-um", type=float, default=0.1,
                   help="re-read until two consecutive reads differ by at most this many um (default 0.1)")
    p.add_argument("--max-reads", type=int, default=10,
                   help="give up waiting for a stable reading after this many reads (default 10)")
    p.add_argument("--output", default="scan.csv", help="CSV output path (default scan.csv)")
    p.add_argument("--dry-run", action="store_true", help="print the planned moves and exit; no device needed")
    a = p.parse_args(argv)
    if a.speed_mm_s <= 0:
        p.error("--speed-mm-s must be positive")
    if a.tolerance_mm < 0:
        p.error("--tolerance-mm must not be negative")
    if a.settle_ms < 0 or a.stable_um < 0:
        p.error("--settle-ms and --stable-um must not be negative")
    if a.max_reads < 2:
        p.error("--max-reads must be at least 2")
    if a.device < 1 or a.axis < 1:
        p.error("--device and --axis are 1-based")
    try:
        plan_moves(a.start_mm, a.end_mm, a.cycles)
    except ValueError as e:
        p.error(str(e))
    return a


def print_plan(a: argparse.Namespace, moves: list[Move]) -> None:
    print(f"Dry run: device {a.device}, axis {a.axis}, no connection made")
    print(f"Home first, then {len(moves)} moves at {a.speed_mm_s} mm/s, tolerance {a.tolerance_mm} mm")
    print(f"After each move: {a.settle_ms:g} ms pause, then re-read until two reads agree within "
          f"{a.stable_um:g} um (at most {a.max_reads} reads)")
    for m in moves:
        print(f"  move {m.index:>3}  cycle {m.cycle}  {m.label:<5} -> {m.target_mm:8.3f} mm")
    est = min_duration_s(moves, 0.0, a.speed_mm_s)
    print(f"Minimum motion time after homing (constant speed, no acceleration): {est:.1f} s")
    print(f"Would write: {a.output}")


def open_connection(a: argparse.Namespace):
    """Context manager for the connection chosen on the command line."""
    from zaber_motion import Library
    from zaber_motion.ascii import Connection

    Library.enable_device_db_store()
    if a.cloud_id:
        # Without a token the library connects as a guest ("unauthenticated").
        kwargs = {"token": a.token} if a.token else {}
        return Connection.open_iot(a.cloud_id, **kwargs)
    return Connection.open_serial_port(a.port)


def format_error(e: Exception) -> str:
    """Exception text without a doubled class name (zaber-motion already prefixes it)."""
    text = str(e)
    name = type(e).__name__
    return text if text.startswith(name) else f"{name}: {text}"


def read_encoder_mm(axis, units) -> float | None:
    """Encoder position in mm, or None when the axis has no encoder setting.

    Read the way Zaber's accuracy example does: the native `encoder.pos`
    value, converted with the `pos` conversion.
    """
    from zaber_motion import MotionLibException

    try:
        native = axis.settings.get("encoder.pos")
    except MotionLibException:
        return None
    return axis.settings.convert_from_native_units("pos", native, units)


def describe_serial(serial: int) -> str:
    return "serial not reported" if serial == PLACEHOLDER_SERIAL else f"serial {serial}"


def print_summary(rows: list[dict], n_moves: int, tolerance_mm: float, output: str) -> None:
    s = summarize(rows)
    basis = "encoder" if rows[0]["encoder_mm"] is not None else "counter"
    print(f"\nSummary: {s['moves']} of {n_moves} moves, max |error| {s['max_abs_error_mm']:.6f} mm "
          f"({basis} minus commanded), {s['failed']} outside {tolerance_mm} mm")
    for (label, direction), g in sorted(s["groups"].items()):
        n = f"{g['n']} arrival{'s' if g['n'] != 1 else ''}"
        spread = f"n/a ({n})" if g["spread"] is None else f"{g['spread']:.6f} mm peak to peak ({n})"
        print(f"  {basis} spread at {label}, arrived {direction}: {spread}")
    for label, b in sorted(s["backlash_mm"].items()):
        print(f"  backlash at {label}: {b:+.6f} mm (mean arrived backward minus mean arrived forward)")
    unstable = sum(1 for r in rows if not r["stable"])
    lo, hi = min(r["reads"] for r in rows), max(r["reads"] for r in rows)
    print(f"  reads per move: {lo if lo == hi else f'{lo} to {hi}'}"
          f"{f', {unstable} never settled' if unstable else ''}")
    print(f"CSV: {output}")


def run(a: argparse.Namespace, moves: list[Move], connect=open_connection) -> int:
    from zaber_motion import Units, MotionLibException

    if not a.cloud_id and not a.port:
        print("error: give --cloud-id (or ZABER_CLOUD_ID) or --port (or ZABER_PORT)", file=sys.stderr)
        return 2

    rows: list[dict] = []
    rc = 0
    try:
        with connect(a) as connection:
            devices = connection.detect_devices()
            print(f"Found {len(devices)} device(s):")
            for i, d in enumerate(devices, 1):
                print(f"  {i}: address {d.device_address}  {d.name}  ({describe_serial(d.serial_number)}, "
                      f"{d.axis_count} axis)")
            if a.device > len(devices):
                print(f"error: --device {a.device} but only {len(devices)} detected", file=sys.stderr)
                return 2
            device = devices[a.device - 1]
            if a.axis > device.axis_count:
                print(f"error: --axis {a.axis} but device has {device.axis_count}", file=sys.stderr)
                return 2
            axis = device.get_axis(a.axis)

            lo = axis.settings.convert_from_native_units("limit.min", axis.settings.get("limit.min"), Units.LENGTH_MILLIMETRES)
            hi = axis.settings.convert_from_native_units("limit.max", axis.settings.get("limit.max"), Units.LENGTH_MILLIMETRES)
            print(f"Axis travel: {lo:.3f} to {hi:.3f} mm")
            check_within_travel(moves, lo, hi)

            has_encoder = read_encoder_mm(axis, Units.LENGTH_MILLIMETRES) is not None
            if has_encoder:
                print("Encoder: yes (encoder.pos); error = encoder - commanded")
            else:
                print("Encoder: none (encoder.pos not supported); error = counter - commanded, "
                      "which only checks the controller's own count")

            try:
                print("Homing ...")
                axis.home()
                previous_mm = axis.get_position(Units.LENGTH_MILLIMETRES)
                print(f"Homed, counter {previous_mm:.4f} mm")
                stable_mm = a.stable_um / 1000

                with open(a.output, "w", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                    w.writeheader()
                    for m in moves:
                        t0 = time.monotonic()
                        axis.move_absolute(
                            m.target_mm, Units.LENGTH_MILLIMETRES, wait_until_idle=True,
                            velocity=a.speed_mm_s, velocity_unit=Units.VELOCITY_MILLIMETRES_PER_SECOND)
                        dt = time.monotonic() - t0
                        direction = arrival_direction(previous_mm, m.target_mm)
                        previous_mm = m.target_mm
                        time.sleep(a.settle_ms / 1000)
                        if has_encoder:
                            encoder, reads, stable = read_stable(
                                lambda: read_encoder_mm(axis, Units.LENGTH_MILLIMETRES), stable_mm, a.max_reads)
                            counter = axis.get_position(Units.LENGTH_MILLIMETRES)
                        else:
                            encoder = None
                            counter, reads, stable = read_stable(
                                lambda: axis.get_position(Units.LENGTH_MILLIMETRES), stable_mm, a.max_reads)
                        basis = counter if encoder is None else encoder
                        ok = within_tolerance(m.target_mm, basis, a.tolerance_mm)
                        row = {
                            "move": m.index, "cycle": m.cycle, "label": m.label, "direction": direction,
                            "commanded_mm": m.target_mm, "counter_mm": round(counter, 6),
                            "encoder_mm": None if encoder is None else round(encoder, 6),
                            "error_mm": round(basis - m.target_mm, 6),
                            "error_basis": "counter" if encoder is None else "encoder",
                            "reads": reads, "stable": stable,
                            "within_tolerance": ok, "duration_s": round(dt, 3),
                        }
                        rows.append(row)
                        w.writerow({k: ("" if v is None else v) for k, v in row.items()})
                        f.flush()
                        enc_text = "n/a" if encoder is None else f"{encoder:.6f}"
                        print(f"move {m.index:>3} {m.label:<5} {direction:<8} commanded {m.target_mm:8.3f}  "
                              f"counter {counter:11.6f}  encoder {enc_text:>11}  "
                              f"error {basis - m.target_mm:+.6f} mm  "
                              f"{'ok' if ok else 'OUT OF TOLERANCE'}  "
                              f"({reads} read{'s' if reads != 1 else ''}{'' if stable else ', not settled'}, {dt:.2f} s)")
            except BaseException:
                # Ctrl-C or a library error mid-move: halt the axis before the connection closes.
                try:
                    axis.stop()
                except Exception:
                    pass
                raise
    except KeyboardInterrupt:
        print("\nInterrupted: stopping axis, closing connection.", file=sys.stderr)
        rc = 130
    except MotionLibException as e:
        print(f"Zaber error: {format_error(e)}", file=sys.stderr)
        rc = 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        rc = 2

    if rows:
        print_summary(rows, len(moves), a.tolerance_mm, a.output)
    if rc == 0 and any(not r["within_tolerance"] for r in rows):
        rc = 3
    return rc


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep stdout and stderr in order
    a = parse_args(argv)
    moves = plan_moves(a.start_mm, a.end_mm, a.cycles)
    if a.dry_run:
        print_plan(a, moves)
        return 0
    return run(a, moves)


if __name__ == "__main__":
    sys.exit(main())
