#!/usr/bin/env python3
"""Plot the error per move from a zaber_scan.py CSV (needs matplotlib, see requirements-dev.txt)."""
from __future__ import annotations

import argparse
import csv

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def load(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def errors_um(rows: list[dict]) -> tuple[list[float], list[float | None]]:
    """(counter - commanded, encoder - commanded) per move in micrometres; None without an encoder."""
    counter = [(float(r["counter_mm"]) - float(r["commanded_mm"])) * 1000 for r in rows]
    encoder = [None if r["encoder_mm"] == "" else (float(r["encoder_mm"]) - float(r["commanded_mm"])) * 1000
               for r in rows]
    return counter, encoder


def ylim_um(counter: list[float], encoder: list[float | None]) -> tuple[float, float]:
    """Y limits that contain every plotted value (and zero), with margin; extra room on top for the legend."""
    values = [v for v in counter + encoder if v is not None] + [0.0]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    return lo - 0.1 * span, hi + 0.4 * span


def plot(rows: list[dict], out: str, title: str):
    moves = [int(r["move"]) for r in rows]
    counter, encoder = errors_um(rows)
    fig, ax = plt.subplots(figsize=(7, 3.2), dpi=150)
    ax.axhline(0, color="#999999", linewidth=0.8)
    ax.plot(moves, counter, "o--", color="#8a8a8a", markersize=5, label="counter - commanded")
    if all(e is not None for e in encoder):
        ax.plot(moves, encoder, "o-", color="#c2410c", markersize=5, label="encoder - commanded")
    ax.set_xlabel("move (odd: start position, even: end position)")
    ax.set_ylabel("error (µm)")
    ax.set_xticks(moves)
    ax.set_ylim(*ylim_um(counter, encoder))
    ax.set_title(title, fontsize=10, loc="left")
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out)
    return ax


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", help="CSV written by zaber_scan.py")
    p.add_argument("--output", default="scan.png")
    p.add_argument("--title", default="Position error per move")
    a = p.parse_args()
    plot(load(a.csv), a.output, a.title)
    print(f"wrote {a.output}")


if __name__ == "__main__":
    main()
