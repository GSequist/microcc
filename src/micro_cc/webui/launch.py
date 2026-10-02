"""Start the GUI server in a background thread and open a browser at it.

Deliberately a thread, not a subprocess: the whole design rests on the server
and claude_loop sharing one process, so the suspended generator that carries a
pending approval is reachable from both.

The `serve()` entry point is also what a future desktop shell would call —
swap `webbrowser.open` for `webview.create_window(...)` (pywebview) and nothing
else in this file, or anywhere else, has to change.

## Binding

Always 0.0.0.0:8765. micro-cc is usually launched inside a sandbox container
that pulls it from PyPI and starts it — we don't control that container's run
flags, and it already publishes 8765. Binding loopback there would make the
GUI unreachable: inside a container 127.0.0.1 is the container's own loopback,
and published ports forward to its eth0 address instead. 0.0.0.0 covers both.
"""

import os
import socket
import sys
import threading
import webbrowser

import uvicorn

from micro_cc.webui import session

HOST = "0.0.0.0"
PORT = 8765


def _has_gui_browser() -> bool:
    """Whether `webbrowser.open` can plausibly hand off to a real GUI browser.

    Without this check, webbrowser.open() on headless Linux (any container
    with no DISPLAY/WAYLAND_DISPLAY — which is the normal case here) still
    registers CONSOLE browsers whenever $TERM is set and www-browser/links/
    elinks/lynx/w3m happens to be on PATH (see cpython's webbrowser.py
    register_standard_browsers). Those run via GenericBrowser.open(), which
    does `subprocess.Popen(cmdline)` with stdio inherited (not redirected)
    and then blocks on `p.wait()`. The child takes over the same tty the
    Textual app is drawing to, so the app appears to "freeze" — mouse and
    keyboard now belong to the hijacking text browser, not to us — until
    that subprocess is manually quit. Skipping the call entirely whenever
    there's no real display avoids ever reaching that branch.
    """
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def serve(project_dir, *, model=None, dangerous=(), trim_budget=None,
          host=HOST, port=PORT, open_browser=True):
    """Boot the server. Returns (url, shutdown) — call shutdown() to stop it."""
    from micro_cc.models.registry import DEFAULT_MODEL, DEFAULT_TRIM_BUDGET
    from micro_cc.webui.server import app  # deferred: pulls in fastapi

    session.init(
        project_dir=project_dir,
        msgs=[],
        model=model or DEFAULT_MODEL,
        dangerous=dangerous,
        # Passed straight to claude_loop's max_tokens, where None would blow up
        # inside token_cutter rather than falling back to anything.
        trim_budget=trim_budget or DEFAULT_TRIM_BUDGET,
    )

    # 0.0.0.0 is a bind address, not somewhere a browser can go — the URL we
    # hand back is always connectable.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{display_host}:{port}"

    # With a fixed port, a second instance would fail to bind but uvicorn only
    # logs it on its own thread — serve() would return happily and the browser
    # would attach to whatever is ALREADY on 8765 (another project's session).
    # Fail loudly here instead.
    probe = socket.socket()
    # Without this, the probe (and uvicorn's own bind right after) refuses
    # the port while a previous /gui's connections are still draining
    # through TIME_WAIT/FIN_WAIT_2 — no process actually holds the port,
    # the kernel is just being conservative. SO_REUSEADDR is what lets a
    # fresh listener claim the port anyway; a real still-running GUI would
    # still fail to bind because that one *is* an active LISTEN socket.
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

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    # uvicorn wants to own signal handlers; off the main thread it can't have
    # them, and shouldn't — the TUI is still the thing the user Ctrl-Cs.
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(target=server.run, name="micro-cc-gui", daemon=True)
    thread.start()

    # Wait for the socket to accept, so a browser never lands on a
    # connection-refused page and callers know the port is really up.
    for _ in range(100):
        try:
            with socket.create_connection((display_host, port), timeout=0.1):
                break
        except OSError:
            threading.Event().wait(0.05)

    if open_browser and _has_gui_browser():
        # serve() runs synchronously on the Textual app's single asyncio
        # thread (see cmd_gui) — a browser launcher that blocks (xdg-open
        # waiting on a dead D-Bus session is the classic case, but any
        # misbehaving registered browser has the same effect) would freeze
        # the whole TUI: no rendering, no mouse, no drag-select, nothing,
        # until that call returns. Fire-and-forget it on its own thread so a
        # hang there can never take the terminal down with it.
        def _open():
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, name="micro-cc-gui-open", daemon=True).start()

    def shutdown():
        server.should_exit = True
        thread.join(timeout=5)

    return url, shutdown
