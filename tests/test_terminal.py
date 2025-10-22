"""Tests for the interactive VXI-11 terminal."""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.server import Vxi11ServerFacade  # type: ignore[import]
from vxi_proxy.terminal import Vxi11Terminal  # type: ignore[import]


CONFIG_YAML = textwrap.dedent(
    """
    server:
        host: 127.0.0.1
        port: 0

    devices:
        loopback0:
            type: loopback

    mappings: {}
    """
)


DEVICE_NAME = "loopback0"


@dataclass(frozen=True)
class ServerHandle:
    host: str
    port: int


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_error: OSError | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"Server {host}:{port} did not become ready") from last_error


class TerminalLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text(CONFIG_YAML, encoding="utf-8")

        cls._facade = Vxi11ServerFacade(config_path)
        cls._ctx = cls._facade.start()
        cls.server_handle = ServerHandle(host=cls._ctx.server.host, port=cls._ctx.server.port)

        cls._thread = threading.Thread(target=cls._ctx.server.loop, daemon=True)
        cls._thread.start()
        _wait_for_server(cls.server_handle.host, cls.server_handle.port)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._facade.stop()
        finally:
            cls._thread.join(timeout=1.0)
            cls._tmpdir.cleanup()

    def setUp(self) -> None:
        self.terminal = Vxi11Terminal(auto_read=True, append_newline=False, read_size=1024, io_timeout=1.0)

    def tearDown(self) -> None:
        self.terminal.close()

    def test_connect_roundtrip_and_disconnect(self) -> None:
        connect_cmd = f"connect {self.server_handle.host} {DEVICE_NAME} --port {self.server_handle.port}"
        result = self.terminal.execute(connect_cmd)
        self.assertTrue(result.lines)
        self.assertIn("Connected", result.lines[0])

        lock = self.terminal.execute("lock")
        self.assertEqual(lock.lines, ["Lock acquired."])

        message = "ping"
        result = self.terminal.execute(message)
        self.assertTrue(any("[write]" in line for line in result.lines))
        self.assertTrue(any("ping" in line for line in result.lines))

        status = self.terminal.execute("status")
        self.assertTrue(any("Host:" in line for line in status.lines))

        disconnect = self.terminal.execute("disconnect")
        self.assertEqual(disconnect.lines, ["Disconnected."])

        post = self.terminal.execute("read")
        self.assertTrue(any("Not connected" in line for line in post.lines))

    def test_question_mark_triggers_help(self) -> None:
        result = self.terminal.execute("?")
        self.assertTrue(result.lines)
        self.assertIn("Commands:", result.lines[0])
        self.assertTrue(any(line.strip() == "?" or line.strip().startswith("? ") for line in result.lines))


if __name__ == "__main__":
    unittest.main()