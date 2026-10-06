import os
import subprocess

import pytest

import run


def test_open_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(run.webbrowser, "open_new", opened.append)
    run.open_browser()
    assert opened == [f"http://127.0.0.1:{run.PORT}"]


@pytest.mark.parametrize("fails", [False, True])
def test_http_shutdown(monkeypatch, fails):
    requests, sleeps = [], []

    def urlopen(req, timeout):
        requests.append((req.get_method(), req.full_url, timeout))
        if fails:
            raise ConnectionResetError

    monkeypatch.setattr(run.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(run.time, "sleep", sleeps.append)
    run.http_shutdown()
    assert requests == [("POST", run.SHUTDOWN_URL, 15)]
    assert sleeps == [6]


class _Process:
    def __init__(self, waits):
        self.waits = list(waits)
        self.events = []

    def wait(self, timeout=None):
        self.events.append(("wait", timeout))
        exc = self.waits.pop(0) if self.waits else None
        if exc:
            raise exc

    def terminate(self):
        self.events.append("terminate")

    def kill(self):
        self.events.append("kill")


@pytest.fixture
def launch(monkeypatch):
    calls = []

    def go(*waits):
        proc = _Process(waits)

        def popen(argv, **kwargs):
            calls.append(("popen", argv, kwargs))
            return proc

        monkeypatch.setattr(run, "check_port", lambda: calls.append("check_port"))
        monkeypatch.setattr(run.subprocess, "Popen", popen)
        monkeypatch.setattr(run.time, "sleep", lambda _: None)
        monkeypatch.setattr(run, "open_browser", lambda: calls.append("open_browser"))
        monkeypatch.setattr(run, "http_shutdown", lambda: proc.events.append("http_shutdown"))
        run.main()
        return calls, proc.events

    return go


def test_main_normal_exit(launch):
    calls, events = launch()
    assert calls[0] == "check_port"
    _, argv, kwargs = calls[1]
    assert argv == [
        "waitress-serve", f"--host={run.HOST}", f"--port={run.PORT}", f"--threads={run.WAITRESS_THREADS}", "app:app",
    ]
    assert kwargs == {"preexec_fn": os.setsid}
    assert calls[2] == "open_browser"
    assert events == [("wait", None)]


def test_main_ctrl_c_shuts_down_then_terminates(launch):
    _, events = launch(KeyboardInterrupt)
    assert events == [("wait", None), "http_shutdown", "terminate", ("wait", 10)]


def test_main_ctrl_c_kills_when_terminate_times_out(launch):
    _, events = launch(KeyboardInterrupt, subprocess.TimeoutExpired("waitress-serve", 10))
    assert events == [("wait", None), "http_shutdown", "terminate", ("wait", 10), "kill", ("wait", None)]
