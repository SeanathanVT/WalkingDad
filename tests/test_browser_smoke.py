"""Browser smoke test (ROADMAP 2.12): the real app and templates in headless Chromium, the treadmill faked."""
import asyncio
import contextlib
import os
import threading

import pytest
from test_ble_async import FakeController, reply
from waitress import create_server

import app

sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
_real_run_coroutine_threadsafe = asyncio.run_coroutine_threadsafe


class _Stop(BaseException):
    """Not an Exception, so the broadcaster loop's own except lets it end the loop."""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            chromium = p.chromium.launch()
        except sync_api.Error:
            if os.environ.get("CI"):
                raise
            pytest.skip("Chromium won't launch; run `python -m playwright install chromium-headless-shell` "
                        "(Linux also needs its system libraries)")
        yield chromium
        chromium.close()


@pytest.fixture
def server(app_state, monkeypatch):
    """The app served on a free port, with belt sequences running on a real loop against FakeController."""
    monkeypatch.setattr(app.asyncio, "run_coroutine_threadsafe", _real_run_coroutine_threadsafe)
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    monkeypatch.setattr(app, "ble_loop", loop)
    monkeypatch.setattr(app, "controller", FakeController(hook=reply(3.0)))
    monkeypatch.setattr(app, "_ble_command_lock", asyncio.Lock())

    stop, real_stale_check = threading.Event(), app._end_session_if_stale_pause

    def stale_check():
        if stop.is_set():
            raise _Stop
        real_stale_check()

    def broadcast():
        with contextlib.suppress(_Stop):
            app._sse_broadcast_loop()

    monkeypatch.setattr(app, "_end_session_if_stale_pause", stale_check)
    broadcaster = threading.Thread(target=broadcast, daemon=True)
    broadcaster.start()

    srv = create_server(app.app, host="127.0.0.1", port=0)
    threading.Thread(target=srv.run, daemon=True).start()
    yield f"http://127.0.0.1:{srv.effective_port}/"
    srv.close()
    stop.set()
    broadcaster.join(timeout=5)

    async def cancel_tasks():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    _real_run_coroutine_threadsafe(cancel_tasks(), loop).result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)
    loop.close()


@pytest.fixture
def page(browser, server):
    context = browser.new_context()
    page = context.new_page()
    page.goto(server)
    yield page
    context.close()


def expect_screen(page, name):
    if name == "start":
        expect(page.locator("#btn-start")).to_be_visible()
    else:
        expect(page.locator("#speed-heading")).to_have_text({"walking": "Belt speed", "paused": "Belt stopped"}[name])


def test_session_lifecycle(page, server):
    expect_screen(page, "start")
    page.get_by_role("button", name="Start walking").click()
    expect_screen(page, "walking")
    page.get_by_role("button", name="Pause").click()
    expect_screen(page, "paused")
    page.get_by_role("button", name="Resume").click()
    expect_screen(page, "walking")
    page.get_by_role("button", name="Pause").click()
    expect_screen(page, "paused")
    page.get_by_role("button", name="End session").click()
    expect_screen(page, "start")
    assert page.url == server
    assert len(app._load_session_history()) == 1


def test_paused_page_follows_remote_resume(page):
    page.get_by_role("button", name="Start walking").click()
    page.get_by_role("button", name="Pause").click()
    expect_screen(page, "paused")
    expect(page.get_by_role("button", name="Resume")).to_be_enabled()  # pause sequence done
    # The remote's Resume as the pad reports it: a stopped reading, then the belt moving.
    app.ble_loop.call_soon_threadsafe(app.process_status_packet, 0, 0, 0)
    app.ble_loop.call_soon_threadsafe(app.process_status_packet, 0, 0, 30)
    expect_screen(page, "walking")


def test_walking_page_follows_auto_pause(page, monkeypatch):
    page.get_by_role("button", name="Start walking").click()
    expect(page.get_by_role("button", name="Pause")).to_be_enabled()  # start sequence done
    # Stepping off: the pad reports the belt stopped once the grace window has passed.
    monkeypatch.setattr(app, "_resume_grace_deadline", 0)
    app.controller.hook = reply(0.0)
    expect_screen(page, "paused")


def test_page_follows_a_dropped_connection(page, monkeypatch):
    async def no_reconnect():
        pass

    monkeypatch.setattr(app, "_auto_reconnect", no_reconnect)
    expect_screen(page, "start")
    app.ble_loop.call_soon_threadsafe(app._handle_disconnect, None)
    expect(page.get_by_role("heading", name="Looking for your WalkingPad")).to_be_visible()


def test_paused_page_follows_a_server_side_end(page):
    page.get_by_role("button", name="Start walking").click()
    page.get_by_role("button", name="Pause").click()
    expect(page.get_by_role("button", name="Resume")).to_be_enabled()
    app._end_session(stale=True)  # what the stale-pause timer calls
    expect_screen(page, "start")
