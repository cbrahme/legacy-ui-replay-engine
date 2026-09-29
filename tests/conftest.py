import copy
import socket
import threading
import time
import httpx
import pytest
import uvicorn

from src.target_app.app import INITIAL_MEMBERS, app
import src.target_app.app as app_module


@pytest.fixture(autouse=True)
def reset_mock_db():
    """Ensures each test runs with fresh in-memory mock banking data."""
    app_module.MEMBERS_DB = copy.deepcopy(INITIAL_MEMBERS)
    yield
    app_module.MEMBERS_DB = copy.deepcopy(INITIAL_MEMBERS)


@pytest.fixture(scope="session")
def target_server_url():
    """Runs target mock server in a background thread/process or returns test host."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            r = httpx.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.1)

    return base_url
