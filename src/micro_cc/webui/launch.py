"""Start the GUI server in a background thread, sharing claude_loop's process, and open a browser."""

import os
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser

import uvicorn

from micro_cc.webui import session

# 0.0.0.0: containers publish 8765; 127.0.0.1 is the container's own loopback.
HOST = "0.0.0.0"
PORT = 8765


def _has_gui_browser() -> bool:
    """Whether webbrowser.open can reach a GUI browser; console browsers would hijack the TUI's tty."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _close_tabs(base: str) -> None:
    """Best-effort macOS: close our tab in Chrome/Arc/Safari if running (the page also tries window.close())."""
    if sys.platform != "darwin":
        return
    for app in ("Google Chrome", "Arc", "Safari"):
        script = (
            f'if application "{app}" is running then\n'
            f'tell application "{app}"\nrepeat with w in windows\n'
            f'close (tabs of w whose URL starts with "{base}")\n'
            "end repeat\nend tell\nend if"
        )
        try:
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def serve(project_dir, *, model=None, trim_budget=None,
          host=HOST, port=PORT, open_browser=True):
    """Boot the server. Returns (url, shutdown) — call shutdown() to stop it."""
    from micro_cc.models.catalog import DEFAULT_MODEL, DEFAULT_TRIM_BUDGET
    from micro_cc.webui.server import app  # deferred: pulls in fastapi

    session.init(
        project_dir=project_dir,
        msgs=[],
        model=model or DEFAULT_MODEL,
        # None would blow up inside token_cutter, so never pass it through.
        trim_budget=trim_budget or DEFAULT_TRIM_BUDGET,
        token=secrets.token_urlsafe(16),
    )

    # 0.0.0.0 is a bind address, not a browsable one.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    base = f"http://{display_host}:{port}"
    url = f"{base}/?t={session.STATE['token']}"  # one-time link; binds the first tab that opens it

    # Fail loudly: a second instance would otherwise attach the browser to the first one's port.
    probe = socket.socket()
    # SO_REUSEADDR: ignore TIME_WAIT leftovers; a live listener still fails to bind.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError as e:
        raise OSError(
            f"port {port} is already in use — another micro-cc GUI is probably "
            f"running. Stop it (esc in its terminal) and try again."
        ) from e
    finally:
        probe.close()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)

    # uvicorn can't install signal handlers off the main thread; the TUI owns Ctrl-C.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, name="micro-cc-gui", daemon=True)
    thread.start()

    # Wait for the socket so the browser never hits connection-refused.
    for _ in range(100):
        try:
            with socket.create_connection((display_host, port), timeout=0.1):
                break
        except OSError:
            threading.Event().wait(0.05)

    if open_browser and _has_gui_browser():
        # Fire-and-forget: a blocking launcher would freeze the TUI's asyncio thread.
        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, name="micro-cc-gui-open", daemon=True).start()

    def shutdown():
        session.STATE["stop"].set()  # in-flight turn breaks at its next chunk and persists
        server.should_exit = True
        thread.join(timeout=5)
        _close_tabs(base)

    return url, shutdown
