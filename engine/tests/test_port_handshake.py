"""`serve --port 0` and the TRADEALERTS_PORT handshake the desktop app reads."""

import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from tradealerts.web.app import bind_socket


def test_port_zero_gets_a_free_port():
    sock = bind_socket("127.0.0.1", 0)
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()


def test_taken_port_falls_back_only_when_asked():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    port = taken.getsockname()[1]
    try:
        with pytest.raises(SystemExit, match="--port 0"):
            bind_socket("127.0.0.1", port)
        sock = bind_socket("127.0.0.1", port, fallback=True)
        try:
            assert sock.getsockname()[1] not in (0, port)
        finally:
            sock.close()
    finally:
        taken.close()


def test_serve_prints_handshake_and_answers(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-m", "tradealerts", "--config", str(tmp_path / "config.yaml"),
         "serve", "--port", "0", "--no-runner", "--no-auth"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=tmp_path)
    try:
        line = proc.stdout.readline().strip()
        assert line.startswith("TRADEALERTS_PORT=")
        port = int(line.split("=", 1)[1])
        assert port > 0
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session", timeout=2) as r:
                    assert r.status == 200
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
