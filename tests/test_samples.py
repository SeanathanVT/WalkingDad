from samples import SampleBuffer


def test_walking_throttled_to_one_per_second():
    buf = SampleBuffer()
    for t in range(0, 10_000, 250):
        buf.add(t, 1.25, t / 1000, t // 500, True)

    rows = buf.drain()

    assert [r[0] for r in rows] == list(range(0, 10_000, 1000))


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
