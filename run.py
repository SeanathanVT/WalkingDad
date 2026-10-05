import errno
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from config import HOST, PORT, WAITRESS_THREADS

SHUTDOWN_URL = f"http://127.0.0.1:{PORT}/shutdown"

def open_browser():
    """Opens the web browser to the application."""
    print(f"Opening browser to http://127.0.0.1:{PORT}")
    webbrowser.open_new(f"http://127.0.0.1:{PORT}")


def _listen_addrs():
    """(family, sockaddr) for every socket Waitress will bind for HOST/PORT,
    resolved the same way as waitress.adjustments.Adjustments."""
    host = HOST
    if "[" in host and "]" in host:
        host = host.strip("[").rstrip("]")
    if host == "*":
        host = None
    addrs = {}
    for family, _, _, _, sockaddr in socket.getaddrinfo(
        host, PORT, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP, socket.AI_PASSIVE
    ):
        # Zone index dropped: macOS can return one link-local address twice
        # with different zones, which still collide on bind().
        addrs.setdefault((sockaddr[0].split("%", 1)[0], sockaddr[1]), (family, sockaddr))
    return list(addrs.values())


def check_port():
    """Exit with a clear message if Waitress won't be able to listen on
    HOST:PORT, instead of a bare traceback after the browser opens."""
    # getaddrinfo wraps out-of-range ports (70000 -> 4464), so check first.
    if type(PORT) is not int or not 1 <= PORT <= 65535:
        sys.exit(f'Can\'t start: "port" in config.json must be a whole number from 1 to 65535, got {PORT!r}.')
    try:
        addrs = _listen_addrs()
    except socket.gaierror as exc:
        sys.exit(f'Can\'t start: "host" {HOST!r} in config.json doesn\'t resolve ({exc}).')

    for family, sockaddr in addrs:
        try:
            # Waitress sets SO_REUSEADDR everywhere; create_server only on POSIX.
            # On Windows that option lets a second listener bind over a live one,
            # so probing without it is what catches a second WalkingDad there.
            socket.create_server(sockaddr, family=family).close()
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", None)):
                hint = ("Another program, possibly another WalkingDad window, is using it. "
                        'Close it, or set a different "port" in config.json and restart.')
            elif exc.errno in (errno.EACCES, getattr(errno, "WSAEACCES", None)):
                hint = ("The OS doesn't allow this port (on Linux, ports below 1024 need root; "
                        'on Windows, it may be reserved). Set a different "port" in config.json and restart.')
            else:
                hint = 'Check "host" and "port" in config.json, then restart.'
            sys.exit(f"Can't start: can't listen on {sockaddr[0]} port {PORT} ({exc}).\n{hint}")


def http_shutdown():
    """Call the /shutdown HTTP endpoint so app.py can set _server_stopping,
    do BLE cleanup, and give the UI a moment to show the shutdown message."""
    try:
        print("Calling /shutdown endpoint...")
        req = urllib.request.Request(SHUTDOWN_URL, method="POST")
        # The /shutdown route does BLE cleanup before returning (up to ~15s),
        # so we give it a generous timeout. Even if the TCP connection is reset
        # by Waitress during cleanup, the shutdown was accepted.
        urllib.request.urlopen(req, timeout=15)
        print("/shutdown acknowledged.")
    except Exception as exc:
        # "Connection reset by peer" is expected if Waitress dies while we're
        # reading the response, the shutdown request was already processed.
        print(f"/shutdown requested (server may have exited before response): {exc}")

    # Wait for the server's deferred exit thread (~5s) to complete so the
    # browser has time to receive stopping:true over the /stats_stream SSE
    # connection and display the shutdown message.
    time.sleep(6)


if __name__ == "__main__":
    check_port()
    print("Starting production server with Waitress...")

    # Start the Waitress server as a subprocess in its own process group
    # so that Ctrl+C in this terminal only hits run.py, not Waitress directly.
    # This gives the /shutdown HTTP endpoint time to complete cleanly.
    if os.name == "posix":
        popen_kwargs = {"preexec_fn": os.setsid}
    else:
        popen_kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    server_process = subprocess.Popen(
        # --threads: each open SSE connection (/stats_stream) holds a worker
        # thread for its entire lifetime, unlike the old short-lived polling
        # requests. Defaults well above the Waitress default of 4 (see
        # config.py's waitress_threads) so several concurrent devices can
        # each hold a stream open alongside action POSTs (start/pause/speed)
        # without stalling; tunable via config.json like every other setting.
        ["waitress-serve", f"--host={HOST}", f"--port={PORT}", f"--threads={WAITRESS_THREADS}", "app:app"],
        **popen_kwargs,
    )

    # Give the server a moment to start up
    time.sleep(2)

    # Open the web browser
    open_browser()

    try:
        # Wait for the server process to complete.
        # You can press Ctrl+C in this window to stop the server.
        server_process.wait()
    except KeyboardInterrupt:
        print("\nStopping server...")
        http_shutdown()  # graceful shutdown via HTTP so UI gets notified
        server_process.terminate()
        try:
            server_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_process.kill()
            server_process.wait()
        print("Server stopped.")
