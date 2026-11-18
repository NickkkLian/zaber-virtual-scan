import csv

import pytest

import plot_scan


def write_csv(path, commanded, counter, encoder):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["move", "commanded_mm", "counter_mm", "encoder_mm"])
        w.writeheader()
        for i, (c, k, e) in enumerate(zip(commanded, counter, encoder), 1):
            w.writerow({"move": i, "commanded_mm": c, "counter_mm": k, "encoder_mm": e})


@pytest.mark.parametrize("offset_mm", [-0.004, +0.004])
def test_both_lines_are_inside_the_y_limits(tmp_path, offset_mm):
    commanded = [5.0, 45.0, 5.0, 45.0]
    counter = [c + 0.00001 for c in commanded]
    encoder = [c + offset_mm * (1 + i / 4) for i, c in enumerate(commanded)]
    write_csv(tmp_path / "s.csv", commanded, counter, encoder)
    ax = plot_scan.plot(plot_scan.load(str(tmp_path / "s.csv")), str(tmp_path / "s.png"), "t")
    lo, hi = ax.get_ylim()
    lines = [l for l in ax.get_lines() if len(l.get_xdata()) == 4]
    assert len(lines) == 2  # counter and encoder
    for line in lines:
        assert all(lo < y < hi for y in line.get_ydata())
