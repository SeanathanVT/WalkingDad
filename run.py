import os
import subprocess
import time
import urllib.request
import webbrowser

from config import HOST, PORT

SHUTDOWN_URL = f"http://127.0.0.1:{PORT}/shutdown"

def open_browser():
    """Opens the web browser to the application."""
    print(f"Opening browser to http://127.0.0.1:{PORT}")
    webbrowser.open_new(f"http://127.0.0.1:{PORT}")


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
        # reading the response — the shutdown request was already processed.
        print(f"/shutdown requested (server may have exited before response): {exc}")

    # Wait for the server's deferred exit thread (~5s) to complete so the
    # browser has time to receive a /stats poll with stopping:true and display
    # the shutdown message.
    time.sleep(6)


if __name__ == "__main__":
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
        # requests. Bumped well above the Waitress default of 4 so several
        # concurrent devices can each hold a stream open alongside action
        # POSTs (start/pause/speed) without stalling.
        ["waitress-serve", f"--host={HOST}", f"--port={PORT}", "--threads=16", "app:app"],
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
