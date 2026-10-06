import pytest

import app


def test_counts_only_intervals_opened_by_a_moving_packet(walking, monkeypatch):
    monkeypatch.setattr(app, "_resume_grace_deadline", float("inf"))  # ramp-up: no auto-pause
    walking(0)
    walking(0)
    walking(4)       # first moving packet opens an interval
    walking(4)
    walking(4.2, after=0.5)
    assert app.current_session_active_seconds == pytest.approx(1.5)
    assert app.belt_running


def test_gap_is_capped(walking):
    walking(4)
    walking(4, after=60)
    assert app.current_session_active_seconds == app._MAX_MOVING_GAP_SECONDS


def test_stop_counts_final_interval_then_auto_pauses(walking):
    walking(4)
    walking(4)
    walking(0)
    assert app.current_session_active_seconds == pytest.approx(2)
    assert not app.belt_running
    walking(0)
    assert app.current_session_active_seconds == pytest.approx(2)


def test_belt_that_never_moved_auto_pauses_after_grace(walking, monkeypatch):
    monkeypatch.setattr(app, "_resume_grace_deadline", float("inf"))
    walking(0)
    assert app.belt_running
    monkeypatch.setattr(app, "_resume_grace_deadline", 0)
    walking(0)
    assert not app.belt_running
    assert app.current_session_active_seconds == 0
    assert app.resume_speed_kmh == 3.0  # keeps the speed it was asked for


def test_no_auto_pause_during_belt_sequence(walking, monkeypatch):
    monkeypatch.setattr(app, "_belt_transitioning", True)
    walking(0)
    assert app.belt_running


def test_not_counted_while_paused(walking, monkeypatch):
    walking(4)
    monkeypatch.setattr(app, "belt_running", False)
    walking(4)
    walking(4)
    assert app.current_session_active_seconds == 0


def test_stop_moment_sampled_despite_paused_throttle(walking, monkeypatch):
    monkeypatch.setattr(app, "_session_id", "s")
    monkeypatch.setattr(app, "_session_start_monotonic", 0.0)
    monkeypatch.setattr(app, "_record_pause", lambda *a, **k: None)
    app._samples.reset()

    walking(4)
    walking(0)  # auto-pause, 1 s after the last walking sample
    rows = app._samples.drain()
    assert [r[4] for r in rows] == [1, 0]  # belt_running column
