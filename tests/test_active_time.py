import pytest

import app


@pytest.fixture
def walking(monkeypatch):
    """A walking session past its grace window, with a controllable monotonic clock."""
    clock = [1000.0]
    monkeypatch.setattr(app.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(app, "_save_session_state", lambda: None)
    monkeypatch.setattr(app, "_session_id", None)
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", True)
    monkeypatch.setattr(app, "_belt_transitioning", False)
    monkeypatch.setattr(app, "_resume_grace_deadline", 0)
    monkeypatch.setattr(app, "current_session_active_seconds", 0)
    monkeypatch.setattr(app, "current_speed_kmh", 0.0)
    monkeypatch.setattr(app, "_last_moving_packet_monotonic", None)
    monkeypatch.setattr(app, "_last_dev_dist", 0)
    monkeypatch.setattr(app, "_last_dev_steps", 0)
    monkeypatch.setattr(app, "resume_speed_kmh", 3.0)
    app.speed_history.clear()

    def packet(speed_kmh, after=1.0):
        clock[0] += after
        app.process_status_packet(0, 0, int(speed_kmh * 10))

    return packet


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
