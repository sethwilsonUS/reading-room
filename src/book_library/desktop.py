"""Native macOS window for the Reading Room (`library app`).

The window shows the same React interface, over the same fixed loopback
address, as `library serve`. Citation links, Host/Origin/CSRF checks, and the
authorized intake worker therefore behave identically; the window only removes
the terminal and the browser address from everyday use.

Server ownership:
- If a Reading Room server for this library already answers at the configured
  address (for example the optional login service), the window reuses it and
  leaves it running when the window closes.
- Otherwise the window runs its own server on a background thread and stops it
  gracefully when the window closes, so the intake worker's shutdown path runs.
"""
from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any, Callable

import httpx

from .local_server import server_address
from .onboarding import port_in_use

STARTUP_TIMEOUT_SECONDS = 30.0
# Library status walks the books folder; allow for a large collection.
PROBE_TIMEOUT_SECONDS = 15.0
WINDOW_OPTIONS = {'title': 'The Reading Room', 'width': 1280, 'height': 860, 'min_size': (720, 540)}


def served_library_root(base_url: str, *, timeout: float | None = None) -> str | None:
    """Return the library root a Reading Room server reports at `base_url`.

    Returns None when nothing answers there as a Reading Room server.
    """
    try:
        response = httpx.get(base_url + '/api/status', timeout=PROBE_TIMEOUT_SECONDS if timeout is None else timeout)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        root = response.json().get('root')
    except (ValueError, AttributeError):
        return None
    return root if isinstance(root, str) else None


class EmbeddedServer:
    """Uvicorn on a worker thread; macOS requires the UI on the main thread.

    Uvicorn skips installing signal handlers off the main thread, so shutdown
    is requested explicitly through `should_exit`, which runs the app lifespan
    (and with it the intake worker's graceful stop).
    """

    def __init__(self, app: Any, host: str, port: int):
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level='warning'))
        self.thread = threading.Thread(target=self.server.run, name='reading-room-server')

    def start(self, timeout: float = STARTUP_TIMEOUT_SECONDS) -> None:
        self.thread.start()
        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError('The library server stopped while starting. Run `library serve` to see the full error.')
            if time.monotonic() > deadline:
                self.stop()
                raise RuntimeError('The library server did not start in time. Run `library serve` to see the full error.')
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread.is_alive():
            self.thread.join()


def _default_server(lib, host: str, port: int) -> EmbeddedServer:
    from .web import create_app
    return EmbeddedServer(create_app(lib, enable_intake_worker=True), host, port)


def run_app(lib, port: int | None = None, *, webview: Any = None,
            server_factory: Callable[..., EmbeddedServer] = _default_server) -> dict[str, Any]:
    """Open the Reading Room window and block until it closes.

    `webview` and `server_factory` are injectable for tests; production uses
    pywebview and an in-process uvicorn server.
    """
    if webview is None:
        try:
            import webview
        except ImportError:
            raise ValueError('The desktop window needs pywebview. Run ./bin/setup again to install the locked dependencies.') from None
    host, port, base_url = server_address(lib, port)
    lib._local_base_url = base_url
    server = None
    if port_in_use(host, port):
        served = served_library_root(base_url)
        if served is None:
            raise ValueError(f'Another program is using {base_url}. Stop it, or open the window on another port with `library app --port PORT`.')
        if Path(served).resolve() != lib.root:
            raise ValueError(f'{base_url} is already serving a different library ({served}). Stop that server, or use `library app --port PORT`.')
        mode = 'reused'
    else:
        server = server_factory(lib, host, port)
        server.start()
        mode = 'owned'
    try:
        webview.create_window(url=base_url, **WINDOW_OPTIONS)
        # A persistent (non-private) web view keeps the appearance preference
        # and tab-session state that the interface stores in the browser.
        webview.start(private_mode=False)
    finally:
        if server is not None:
            server.stop()
    return {'address': base_url, 'server': mode, 'root': str(lib.root)}
