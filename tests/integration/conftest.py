import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest
import uvicorn

from computer_use.safety import NavigationPolicy
from legacy_core.app import app

_ROUTES = frozenset({"/", "/workspace/inquiry", "/workspace/member/:member_number"})


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture(scope="session")
def legacy_core_url() -> Iterator[str]:
    """Run LegacyCore on an ephemeral port in a background thread for browser tests."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def nav_policy(legacy_core_url: str) -> NavigationPolicy:
    """A navigation scope for the ephemeral test origin (safe to derive here: the
    test controls its own target)."""
    parts = urlsplit(legacy_core_url)
    return NavigationPolicy(
        allowed_origins=frozenset({f"{parts.scheme}://{parts.netloc}"}),
        allowed_routes=_ROUTES,
    )
