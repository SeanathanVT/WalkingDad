import app


def test_stale_pause_ends_session(monkeypatch):
    ended = []
    monkeypatch.setattr(app, "_end_session", lambda stale=False: ended.append(stale))
    monkeypatch.setattr(app, "STALE_PAUSE_TIMEOUT_MINUTES", 30)
    monkeypatch.setattr(app, "_paused_since", None)
    monkeypatch.setattr(app, "session_active", True)
    monkeypatch.setattr(app, "belt_running", False)
    now = [1000.0]
    monkeypatch.setattr(app.time, "time", lambda: now[0])

    app._end_session_if_stale_pause()
    now[0] += 30 * 60 - 1
    app._end_session_if_stale_pause()
    assert ended == []

    app.belt_running = True  # resume resets the clock
    app._end_session_if_stale_pause()
    app.belt_running = False
    app._end_session_if_stale_pause()
    now[0] += 30 * 60 - 1
    app._end_session_if_stale_pause()
    assert ended == []

    now[0] += 1
    app._end_session_if_stale_pause()
    assert ended == [True]

    app.STALE_PAUSE_TIMEOUT_MINUTES = 0
    app._end_session_if_stale_pause()
    now[0] += 10**6
    app._end_session_if_stale_pause()
    assert ended == [True]
