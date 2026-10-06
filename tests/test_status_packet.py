import time

import pytest

import app


def test_distance_and_steps_accumulate(walking):
    walking(4, dist=10, steps=20)
    walking(4, dist=25, steps=50)
    assert app.current_distance_km == pytest.approx(0.25)
    assert app.current_steps == 50


def test_device_counter_reset_continues_from_new_baseline(walking):
    walking(4, dist=100, steps=500)
    walking(4, dist=3, steps=7)
    walking(4, dist=5, steps=10)
    assert app.current_distance_km == pytest.approx(1.05)
    assert app.current_steps == 510


def test_calories_follow_distance(walking):
    walking(4, dist=160)
    assert app.current_calories == pytest.approx(app.kcal_estimate(1.6 * app.KM_TO_MI))


def test_current_speed_updates(walking):
    walking(4.2)
    assert app.current_speed_kmh == pytest.approx(4.2)
    walking(3.5)
    assert app.current_speed_kmh == pytest.approx(3.5)


def test_speed_history_only_records_above_min_speed(walking, monkeypatch):
    monkeypatch.setattr(app, "MIN_SPEED_KMH", 1.0)
    walking(1.0)
    walking(1.5)
    assert list(app.speed_history) == [1.5]


def test_speed_history_not_recorded_while_paused(walking, monkeypatch):
    monkeypatch.setattr(app, "belt_running", False)
    walking(app.MIN_SPEED_KMH + 1)
    assert not app.speed_history


def test_speed_history_capped(walking, monkeypatch):
    monkeypatch.setattr(app, "MIN_SPEED_KMH", 1.0)
    cap = app.speed_history.maxlen
    for i in range(cap + 3):
        walking(2 + i / 10)
    assert len(app.speed_history) == cap
    assert app.speed_history[0] == pytest.approx(2.3)


def test_auto_pause_resumes_at_oldest_history_speed(walking, monkeypatch):
    monkeypatch.setattr(app, "MIN_SPEED_KMH", 1.0)
    monkeypatch.setattr(app, "_record_pause", lambda *a, **k: None)
    walking(4.5)
    walking(3.0)  # deceleration
    walking(0)
    assert not app.belt_running
    assert app.resume_speed_kmh == pytest.approx(4.5)


def test_auto_pause_falls_back_to_min_speed_when_history_empty(walking, monkeypatch):
    monkeypatch.setattr(app, "MIN_SPEED_KMH", 2.0)
    monkeypatch.setattr(app, "_record_pause", lambda *a, **k: None)
    walking(1.5)
    walking(0)
    assert app.resume_speed_kmh == 2.0


def test_auto_pause_persists(walking, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_record_pause", lambda reason, *a, **k: calls.append(reason))
    monkeypatch.setattr(app, "_save_session_state", lambda: calls.append("saved"))
    walking(4)
    assert calls == []
    walking(0)
    assert calls == ["auto", "saved"]


def test_grace_deadline_suppresses_auto_pause(walking, monkeypatch):
    monkeypatch.setattr(app, "_resume_grace_deadline", time.time() + 60)
    walking(4)
    walking(0)
    assert app.belt_running


def test_status_update_stamped_on_success(walking, monkeypatch):
    monkeypatch.setattr(app, "_last_status_update_monotonic", 0.0)
    walking(4)
    assert app._last_status_update_monotonic == app.time.monotonic()


def test_status_update_not_stamped_when_packet_raises(walking, monkeypatch):
    monkeypatch.setattr(app, "_last_status_update_monotonic", 0.0)
    with pytest.raises(TypeError):
        walking(4, dist=None)
    assert app._last_status_update_monotonic == 0.0
