import pytest

from samples import SampleBuffer, summary_from_samples


def test_summary_from_samples():
    rows = [
        {"t_ms": 1000, "distance_m": 0.0, "steps": 0, "belt_running": 1},
        {"t_ms": 2000, "distance_m": 1.5, "steps": 2, "belt_running": 1},
        {"t_ms": 3000, "distance_m": 3.0, "steps": 4, "belt_running": 1},
        {"t_ms": 8000, "distance_m": 3.0, "steps": 4, "belt_running": 0},
        {"t_ms": 61_000, "distance_m": 1609.344, "steps": 2000, "belt_running": 1},
    ]

    s = summary_from_samples("2026-07-20T23:59:30-04:00", rows, kcal_per_mile=95)

    assert s["end_time"] == "2026-07-21T00:00:31-04:00"
    assert s["elapsed_s"] == 61
    assert s["moving_s"] == 4
    assert s["distance_m"] == 1609.344
    assert s["steps"] == 2000
    assert s["calories_kcal"] == pytest.approx(95, rel=1e-5)
    assert s["avg_speed_mps"] == pytest.approx(1609.344 / 4)


def test_walking_throttled_to_one_per_second():
    buf = SampleBuffer()
    for t in range(0, 10_000, 250):
        buf.add(t, 1.25, t / 1000, t // 500, True)

    rows = buf.drain()

    assert [r[0] for r in rows] == list(range(0, 10_000, 1000))


def test_real_packet_jitter_keeps_every_packet():
    # Real WalkingPad packets arrive ~1/s with +/-20 ms jitter (gaps of 980-1020 ms).
    buf = SampleBuffer()
    times = [k * 1000 + (15 if k % 2 else -15) for k in range(1, 21)]
    for t in times:
        buf.add(t, 1.25, t / 1000, t // 500, True)

    assert [r[0] for r in buf.drain()] == times


def test_paused_throttled_to_one_per_five_seconds():
    buf = SampleBuffer()
    for t in range(0, 20_000, 1000):
        buf.add(t, 0.0, 5.0, 10, False)

    assert len(buf.drain()) == 4


def test_cumulative_values_pass_through():
    buf = SampleBuffer()
    buf.add(0, 1.25, 12.5, 20, True)

    assert buf.drain() == [(0, 1.25, 12.5, 20, 1, None)]


def test_force_bypasses_throttle_but_never_repeats_t_ms():
    buf = SampleBuffer()
    buf.add(1000, 1.0, 1.0, 1, True)
    buf.add(1200, 1.0, 1.2, 2, False, force=True)
    buf.add(1200, 1.0, 1.2, 2, False, force=True)

    assert [r[0] for r in buf.drain()] == [1000, 1200]


def test_drain_empties_but_keeps_throttle_state():
    buf = SampleBuffer()
    buf.add(0, 1.0, 0.0, 0, True)
    buf.drain()
    buf.add(500, 1.0, 0.5, 1, True)

    assert buf.drain() == []


def test_reset_clears_rows_and_throttle():
    buf = SampleBuffer()
    buf.add(5000, 1.0, 5.0, 10, True)
    buf.reset()
    buf.add(0, 1.0, 0.0, 0, True)

    assert [r[0] for r in buf.drain()] == [0]
