"""Integration tests for the VXI-11 façade using the loopback adapter."""

from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Tuple, cast
import textwrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi11 import vxi11 as vxi11_proto

from vxi_proxy.server import Vxi11ServerFacade  # type: ignore[import]

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

DEVICE_NAME = b"loopback0"


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
        except OSError as exc:  # pragma: no cover - polling until ready
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"Server {host}:{port} did not become ready") from last_error


def _open_core_client(handle: ServerHandle) -> vxi11_proto.CoreClient:
    client = vxi11_proto.CoreClient(handle.host, port=handle.port)
    client.sock.settimeout(2.0)
    return client


class LoopbackFacadeTests(unittest.TestCase):
    """End-to-end tests for the façade against the loopback adapter."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = TemporaryDirectory()
        config_path = Path(cls._tmpdir.name) / "config.yaml"
        config_path.write_text(CONFIG_YAML, encoding="utf-8")

        cls._facade = Vxi11ServerFacade(config_path)
        cls._ctx = cls._facade.start()

        cls.server_handle = ServerHandle(host="127.0.0.1", port=cls._ctx.server.port)

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

    def test_link_create_and_destroy(self) -> None:
        client = _open_core_client(self.server_handle)
        self.addCleanup(client.close)

        err, lid, _, max_recv = cast(
            Tuple[int, int, int, int],
            client.create_link(1234, False, 0, DEVICE_NAME),
        )
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(lid, 0)
        self.assertGreater(max_recv, 0)

        err_destroy = cast(int, client.destroy_link(lid))
        self.assertEqual(err_destroy, vxi11_proto.ERR_NO_ERROR)

        err_write, _ = cast(Tuple[int, int], client.device_write(lid, 1000, 0, 0, b"ping"))
        self.assertEqual(err_write, vxi11_proto.ERR_INVALID_LINK_IDENTIFIER)

    def test_lock_required_for_io(self) -> None:
        client = _open_core_client(self.server_handle)
        self.addCleanup(client.close)

        err, lid, _, _ = cast(
            Tuple[int, int, int, int],
            client.create_link(0xDEAD, False, 0, DEVICE_NAME),
        )
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.addCleanup(lambda: client.destroy_link(lid))

        err_write, _ = cast(Tuple[int, int], client.device_write(lid, 1000, 0, 0, b"hello"))
        self.assertEqual(err_write, vxi11_proto.ERR_NO_LOCK_HELD_BY_THIS_LINK)

        err_lock = cast(int, client.device_lock(lid, 0, 1000))
        self.assertEqual(err_lock, vxi11_proto.ERR_NO_ERROR)
        self.addCleanup(lambda: client.device_unlock(lid))

        payload = b"hello"
        err_write, size = cast(Tuple[int, int], client.device_write(lid, 1000, 0, 0, payload))
        self.assertEqual(err_write, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(size, len(payload))

        err_read, reason, data = cast(
            Tuple[int, int, bytes],
            client.device_read(lid, 1024, 1000, 0, 0, 0),
        )
        self.assertEqual(err_read, vxi11_proto.ERR_NO_ERROR)
        self.assertIn(reason, (0, vxi11_proto.RX_END))
        self.assertEqual(data, payload)

        err_unlock = cast(int, client.device_unlock(lid))
        self.assertEqual(err_unlock, vxi11_proto.ERR_NO_ERROR)

        err_destroy = cast(int, client.destroy_link(lid))
        self.assertEqual(err_destroy, vxi11_proto.ERR_NO_ERROR)

    def test_concurrent_locking(self) -> None:
        client_a = _open_core_client(self.server_handle)
        client_b = _open_core_client(self.server_handle)
        self.addCleanup(client_a.close)
        self.addCleanup(client_b.close)

        err, lid_a, _, _ = cast(
            Tuple[int, int, int, int],
            client_a.create_link(111, False, 0, DEVICE_NAME),
        )
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.addCleanup(lambda: client_a.destroy_link(lid_a))

        err, lid_b, _, _ = cast(
            Tuple[int, int, int, int],
            client_b.create_link(222, False, 0, DEVICE_NAME),
        )
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.addCleanup(lambda: client_b.destroy_link(lid_b))

        err = cast(int, client_a.device_lock(lid_a, 0, 2000))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.addCleanup(lambda: client_a.device_unlock(lid_a))

        start = time.time()
        err = cast(int, client_b.device_lock(lid_b, 0, 500))
        elapsed = time.time() - start
        self.assertEqual(err, vxi11_proto.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK)
        self.assertGreaterEqual(elapsed, 0)

        err = cast(int, client_a.device_unlock(lid_a))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err = cast(int, client_b.device_lock(lid_b, 0, 1000))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err = cast(int, client_b.device_unlock(lid_b))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err = cast(int, client_a.destroy_link(lid_a))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        err = cast(int, client_b.destroy_link(lid_b))
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
