import csv

import pytest
from zaber_motion import ConnectionFailedException, MotionLibException

import zaber_scan as zs


def test_plan_alternates_start_end():
    moves = zs.plan_moves(5, 45, 2)
    assert [(m.index, m.cycle, m.label, m.target_mm) for m in moves] == [
        (1, 1, "start", 5), (2, 1, "end", 45), (3, 2, "start", 5), (4, 2, "end", 45)]


@pytest.mark.parametrize("start,end,cycles", [(5, 5, 1), (5, 45, 0)])
def test_plan_rejects_bad_input(start, end, cycles):
    with pytest.raises(ValueError):
        zs.plan_moves(start, end, cycles)


def test_travel_check():
    moves = zs.plan_moves(5, 45, 1)
    zs.check_within_travel(moves, 0, 50)
    with pytest.raises(ValueError, match="outside the axis travel"):
        zs.check_within_travel(moves, 0, 40)


def test_tolerance_is_inclusive_and_symmetric():
    assert zs.within_tolerance(10.0, 10.01, 0.01)
    assert zs.within_tolerance(10.0, 9.99, 0.01)
    assert not zs.within_tolerance(10.0, 10.02, 0.01)


def test_min_duration():
    # home(0) -> 5 -> 45 -> 5 -> 45 = 5 + 40 + 40 + 40 = 125 mm at 25 mm/s
    moves = zs.plan_moves(5, 45, 2)
    assert zs.min_duration_s(moves, 0.0, 25.0) == pytest.approx(5.0)
    with pytest.raises(ValueError):
        zs.min_duration_s(moves, 0.0, 0)


def _row(label, counter, encoder, error, ok, direction="forward"):
    return {"label": label, "direction": direction, "counter_mm": counter, "encoder_mm": encoder,
            "error_mm": error, "within_tolerance": ok}


def test_summarize_uses_the_encoder():
    # counter reads perfect; the encoder shows the real spread
    rows = [_row("start", 5.0, 5.001, 0.001, True), _row("start", 5.0, 4.999, -0.001, True)]
    assert zs.summarize(rows)["groups"][("start", "forward")]["spread"] == pytest.approx(0.002)


def test_summarize():
    rows = [
        _row("start", 5.001, None, 0.001, True),
        _row("end", 45.0, None, 0.0, True),
        _row("start", 4.999, None, -0.001, True),
        _row("end", 45.03, None, 0.03, False),
    ]
    s = zs.summarize(rows)
    assert s["moves"] == 4 and s["failed"] == 1
    assert s["max_abs_error_mm"] == pytest.approx(0.03)
    assert s["groups"][("start", "forward")]["spread"] == pytest.approx(0.002)
    assert s["groups"][("end", "forward")]["spread"] == pytest.approx(0.03)
    assert s["backlash_mm"] == {}


def test_summarize_separates_directions_and_reports_backlash():
    # forward arrivals at 5.000 and 5.001; backward arrivals 0.02 lower, 4.980 and 4.981
    rows = [_row("start", 5, 5.000, 0, True, "forward"), _row("start", 5, 5.001, 0, True, "forward"),
            _row("start", 5, 4.980, 0, True, "backward"), _row("start", 5, 4.981, 0, True, "backward")]
    s = zs.summarize(rows)
    assert s["groups"][("start", "forward")]["spread"] == pytest.approx(0.001)
    assert s["groups"][("start", "backward")]["spread"] == pytest.approx(0.001)
    assert s["backlash_mm"]["start"] == pytest.approx(-0.0200)  # backward mean minus forward mean


def test_summarize_single_arrival_has_no_spread_and_direction_none_is_ignored():
    rows = [_row("start", 5, 5.0, 0, True, "forward"), _row("start", 5, 9.0, 0, True, "none")]
    s = zs.summarize(rows)
    assert s["groups"] == {("start", "forward"): {"n": 1, "mean": 5.0, "spread": None}}


def test_arrival_direction():
    assert zs.arrival_direction(0, 5) == "forward"
    assert zs.arrival_direction(45, 5) == "backward"
    assert zs.arrival_direction(5, 5) == "none"


def test_read_stable_returns_after_two_agreeing_reads():
    seq = iter([1.0, 1.5, 1.52, 1.52, 9.0])
    value, reads, stable = zs.read_stable(lambda: next(seq), 0.05, 10)
    assert (value, reads, stable) == (1.52, 3, True)


def test_read_stable_gives_up_at_the_limit():
    seq = iter([1.0, 2.0, 3.0, 4.0, 5.0])
    value, reads, stable = zs.read_stable(lambda: next(seq), 0.05, 4)
    assert (value, reads, stable) == (4.0, 4, False)


def test_args_defaults_and_validation(capsys):
    a = zs.parse_args(["--dry-run", "--cycles", "3", "--speed-mm-s", "10"])
    assert (a.cycles, a.speed_mm_s, a.start_mm, a.end_mm) == (3, 10.0, 5.0, 45.0)
    for bad in (["--speed-mm-s", "0"], ["--start-mm", "5", "--end-mm", "5"], ["--axis", "0"]):
        with pytest.raises(SystemExit):
            zs.parse_args(bad)


def test_settle_and_stable_args():
    a = zs.parse_args(["--dry-run"])
    assert (a.settle_ms, a.stable_um, a.max_reads) == (200.0, 0.1, 10)
    for bad in (["--settle-ms", "-1"], ["--stable-um", "-0.1"], ["--max-reads", "1"]):
        with pytest.raises(SystemExit):
            zs.parse_args(bad)


def test_dry_run_makes_no_connection(capsys, monkeypatch):
    monkeypatch.setattr(zs, "run", lambda *a, **k: pytest.fail("run() called in dry-run"))
    assert zs.main(["--dry-run", "--cycles", "1"]) == 0
    out = capsys.readouterr().out
    assert "no connection made" in out and "45.000 mm" in out


# ---- run() against a fake connection: no network, no device ----------------

class FakeSettings:
    def __init__(self, encoder):
        self.encoder = encoder

    def get(self, name):
        if name == "limit.min":
            return 0.0
        if name == "limit.max":
            return 50.0
        if name == "encoder.pos":
            if self.encoder is None:
                raise MotionLibException("BADCOMMAND: no encoder")
            return self.encoder.native()
        raise KeyError(name)

    def convert_from_native_units(self, name, value, unit):
        return value


class FakeAxis:
    """Counter follows the command exactly; the encoder is off by `encoder_offset_mm`.

    `backlash_mm`: the encoder reads this much lower after arriving moving negative.
    `settling_mm`: after each move the first encoder reads are off by these extra
    amounts, one per read, then the encoder is steady.
    """

    def __init__(self, encoder_offset_mm=0.002, has_encoder=True, fail_on_move=None, fail_with=None,
                 backlash_mm=0.0, settling_mm=()):
        self.pos = 0.0
        self.stopped = 0
        self.moves = 0
        self.fail_on_move = fail_on_move
        self.fail_with = fail_with
        self.encoder_offset_mm = encoder_offset_mm
        self.backlash_mm = backlash_mm
        self.settling_mm = list(settling_mm)
        self.pending = []
        self.encoder_reads = 0
        self.arrived_negative = False
        self.settings = FakeSettings(self if has_encoder else None)

    def native(self):
        self.encoder_reads += 1
        extra = self.pending.pop(0) if self.pending else 0.0
        return self.pos + self.encoder_offset_mm - (self.backlash_mm if self.arrived_negative else 0.0) + extra

    def home(self):
        self.pos = 0.0
        self.arrived_negative = False

    def move_absolute(self, target, unit, **kw):
        self.moves += 1
        if self.moves == self.fail_on_move:
            raise self.fail_with
        self.arrived_negative = target < self.pos
        self.pos = target
        self.pending = list(self.settling_mm)

    def get_position(self, unit):
        return self.pos

    def stop(self):
        self.stopped += 1


class FakeDevice:
    def __init__(self, axis, serial=4294967295):
        self.axis, self.axis_count = axis, 1
        self.device_address, self.name, self.serial_number = 1, "FAKE-STAGE", serial

    def get_axis(self, n):
        return self.axis


class FakeConnection:
    def __init__(self, device):
        self.device = device
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def detect_devices(self):
        return [self.device]


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(zs.time, "sleep", slept.append)
    return slept


def run_fake(tmp_path, argv, axis, **kw):
    a = zs.parse_args(["--cloud-id", "fake", "--output", str(tmp_path / "out.csv"), *argv])
    conn = FakeConnection(FakeDevice(axis, **kw))
    rc = zs.run(a, zs.plan_moves(a.start_mm, a.end_mm, a.cycles), connect=lambda _a: conn)
    return rc, conn, tmp_path / "out.csv"


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_encoder_error_is_encoder_minus_commanded(tmp_path, capsys):
    rc, conn, out = run_fake(tmp_path, ["--cycles", "1", "--tolerance-mm", "0.01"], FakeAxis(0.002))
    rows = read_csv(out)
    assert rc == 0 and conn.closed
    assert list(rows[0]) == zs.CSV_FIELDS
    assert float(rows[0]["counter_mm"]) == 5.0
    assert float(rows[0]["encoder_mm"]) == pytest.approx(5.002)
    assert float(rows[0]["error_mm"]) == pytest.approx(0.002)
    assert rows[0]["error_basis"] == "encoder"
    out_text = capsys.readouterr().out
    assert "Encoder: yes" in out_text and "serial not reported" in out_text


def test_tolerance_uses_the_encoder_not_the_counter(tmp_path):
    # counter is perfect, the encoder is 0.05 mm off: must exit 3
    rc, _, out = run_fake(tmp_path, ["--cycles", "1"], FakeAxis(0.05))
    assert rc == 3
    assert read_csv(out)[0]["within_tolerance"] == "False"


def test_no_encoder_falls_back_to_counter_and_says_so(tmp_path, capsys):
    rc, _, out = run_fake(tmp_path, ["--cycles", "1"], FakeAxis(has_encoder=False))
    rows = read_csv(out)
    assert rc == 0
    assert rows[0]["encoder_mm"] == "" and rows[0]["error_basis"] == "counter"
    text = capsys.readouterr().out
    assert "Encoder: none" in text and "counter minus commanded" in text


def test_real_serial_is_printed(tmp_path, capsys):
    run_fake(tmp_path, ["--cycles", "1"], FakeAxis(), serial=12345)
    assert "serial 12345" in capsys.readouterr().out


def test_ctrl_c_stops_axis_keeps_rows_exit_130(tmp_path):
    axis = FakeAxis(fail_on_move=3, fail_with=KeyboardInterrupt())
    rc, conn, out = run_fake(tmp_path, ["--cycles", "3"], axis)
    assert rc == 130
    assert axis.stopped == 1 and conn.closed
    assert len(read_csv(out)) == 2  # moves 1 and 2 were written before the interrupt


def test_library_error_stops_axis_exit_1(tmp_path, capsys):
    axis = FakeAxis(fail_on_move=2, fail_with=ConnectionFailedException("Connection lost"))
    rc, conn, out = run_fake(tmp_path, ["--cycles", "2"], axis)
    assert rc == 1
    assert axis.stopped == 1 and conn.closed
    assert len(read_csv(out)) == 1
    err = capsys.readouterr().err
    assert "Zaber error: ConnectionFailedException: Connection lost" in err
    assert err.count("ConnectionFailedException") == 1


def test_plan_outside_travel_is_refused_before_moving(tmp_path):
    axis = FakeAxis()
    rc, conn, out = run_fake(tmp_path, ["--end-mm", "60", "--cycles", "1"], axis)
    assert rc == 2
    assert axis.moves == 0 and not out.exists() and conn.closed


def test_failed_connection_prints_no_summary(tmp_path, capsys):
    a = zs.parse_args(["--cloud-id", "fake", "--output", str(tmp_path / "o.csv")])

    def boom(_a):
        raise ConnectionFailedException("Establishing connection failed")

    rc = zs.run(a, zs.plan_moves(a.start_mm, a.end_mm, a.cycles), connect=boom)
    cap = capsys.readouterr()
    assert rc == 1 and "Summary" not in cap.out
    assert cap.err.count("ConnectionFailedException") == 1
    assert not (tmp_path / "o.csv").exists()


def test_backlash_shows_in_backlash_not_in_spread(tmp_path, capsys):
    # encoder reads 0.02 mm low whenever the axis arrives moving negative; otherwise repeatable
    axis = FakeAxis(encoder_offset_mm=0.0, backlash_mm=0.02)
    rc, _, out = run_fake(tmp_path, ["--cycles", "3", "--tolerance-mm", "0.05"], axis)
    rows = read_csv(out)
    assert [r["direction"] for r in rows] == ["forward", "forward", "backward", "forward", "backward", "forward"]
    s = zs.summarize([{**r, "counter_mm": float(r["counter_mm"]), "encoder_mm": float(r["encoder_mm"]),
                       "error_mm": float(r["error_mm"]), "within_tolerance": r["within_tolerance"] == "True"}
                      for r in rows])
    assert s["groups"][("start", "backward")]["spread"] == pytest.approx(0.0, abs=1e-9)
    assert s["groups"][("end", "forward")]["spread"] == pytest.approx(0.0, abs=1e-9)
    assert s["backlash_mm"]["start"] == pytest.approx(-0.02, abs=1e-6)
    text = capsys.readouterr().out
    assert "spread at start, arrived backward: 0.000000 mm" in text
    assert "backlash at start: -0.020000 mm" in text
    assert "spread at start, arrived forward: n/a" in text  # only the first move from home came forward


def test_settle_pause_then_reread_until_stable(tmp_path, no_real_sleep):
    # the first two reads after a move are still creeping; the third matches the fourth
    axis = FakeAxis(encoder_offset_mm=0.0, settling_mm=[0.004, 0.001])
    rc, _, out = run_fake(tmp_path, ["--cycles", "1", "--settle-ms", "250", "--stable-um", "0.1"], axis)
    rows = read_csv(out)
    assert no_real_sleep == [0.25, 0.25]
    assert [r["reads"] for r in rows] == ["4", "4"]
    assert all(r["stable"] == "True" for r in rows)
    assert float(rows[0]["encoder_mm"]) == pytest.approx(5.0)  # the settled value, not the first read
    assert axis.encoder_reads == 1 + 8  # one probe for "has encoder", then 4 reads per move


def test_unstable_encoder_stops_at_the_read_limit(tmp_path, capsys):
    axis = FakeAxis(encoder_offset_mm=0.0, settling_mm=[0.01 * i for i in range(1, 20)])
    rc, _, out = run_fake(tmp_path, ["--cycles", "1", "--max-reads", "3"], axis)
    rows = read_csv(out)
    assert [r["reads"] for r in rows] == ["3", "3"] and all(r["stable"] == "False" for r in rows)
    assert "not settled" in capsys.readouterr().out
