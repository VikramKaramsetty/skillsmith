"""WS3 fixtures. Helpers live in ws3_fakes.py (a unique module name; tests/ws1 also has a conftest)."""

import threading
from http.server import ThreadingHTTPServer

import pytest
from ws3_fakes import FakeRobotd, robotd_handler


@pytest.fixture
def robotd():
    state = FakeRobotd()
    server = ThreadingHTTPServer(("127.0.0.1", 0), robotd_handler(state))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()
