"""Integration test: VXI-11 -> SCPI-Serial adapter -> mock instrument (in-process fake).

This test starts the server using a temporary config that defines a `scpi-serial`
device. It patches the `serial` module so the adapter opens a FakeSerial whose
write() handler immediately pushes appropriate responses into the rx buffer.

The test then uses the VXI-11 CoreClient to create a link (with lock), send
SCPI commands (*IDN?, MEAS:VOLT?) and asserts the returned responses match
what the fake instrument produces.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Deque
from collections import deque

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class FakeSerial:
    """A simple fake serial port with rx/tx queues.

    When write() is called the data is inspected as a command and a
    matching response is pushed into the rx queue so adapter.read() sees it.
    """

    def __init__(self, *_, **kwargs):
        self.timeout = kwargs.get("timeout", 1.0)
        self.write_timeout = kwargs.get("write_timeout", 1.0)
        self.is_open = True
        self._rx: Deque[bytes] = deque()
        self._tx: Deque[bytes] = deque()

    def close(self):
        self.is_open = False

    def write(self, data: bytes) -> int:
        # Record written data
        self._tx.append(bytes(data))
        try:
            text = data.decode("utf-8", errors="replace").strip()
        except Exception:
            text = ""
        # Simple SCPI responses
        cmd = text.upper()
        if cmd == "*IDN?":
            self._rx.append(b"Mock Instruments Inc.,SCPI-SIM-1000,SIM123456,1.0.0\n")
        elif cmd in ("MEAS:VOLT?", "MEASURE:VOLTAGE?"):
            # deterministic value for test
            self._rx.append(b"5.0000\n")
        else:
            # Unknown commands produce no immediate response but populate error queue
            # Simulate no-response (adapter.read will timeout and return empty)
            pass
        return len(data)

    def read(self, size: int) -> bytes:
        # Pop from rx queue up to size
        if not self._rx:
            # Simulate timeout by returning empty bytes
            time.sleep(min(self.timeout, 0.01))
            return b""
        buf = bytearray()
        remaining = size
        while self._rx and remaining > 0:
            chunk = self._rx.popleft()
            if len(chunk) <= remaining:
                buf.extend(chunk)
                remaining -= len(chunk)
            else:
                buf.extend(chunk[:remaining])
                self._rx.appendleft(chunk[remaining:])
                remaining = 0
        return bytes(buf)


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"Server {host}:{port} did not become ready") from last_error


class ScpiSerialIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Ensure the venv's src is on sys.path (already done above)
        # Prepare a fake serial module so scpi_serial imports it
        self._orig_serial = sys.modules.get("serial")
        self.fake_serial = FakeSerial()
        fake_serial_mod = types.SimpleNamespace(Serial=lambda *a, **k: self.fake_serial)
        sys.modules["serial"] = fake_serial_mod  # type: ignore

        # Create a temporary config file pointing to a scpi-serial device
        self.cfg_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml")
        self.cfg_file.write(
            "server:\n  host: 127.0.0.1\n  port: 0\n\ndevices:\n  mock_dmm:\n    type: scpi-serial\n    port: COM_TEST\n    baudrate: 115200\n    timeout: 0.1\n    write_termination: '\\n'\n    read_termination: '\\n'\n"
        )
        self.cfg_file.flush()
        # Close the file handle; we'll unlink the path in tearDown
        try:
            self.cfg_file.close()
        except Exception:
            pass
        self.cfg_path = Path(self.cfg_file.name)

        # Start the server facade
        from vxi_proxy.server import Vxi11ServerFacade  # type: ignore

        self.facade = Vxi11ServerFacade(self.cfg_path)
        ctx = self.facade.start()
        self.server = ctx.server
        self.runtime = ctx.runtime

        # Run server loop in background thread
        self._worker = threading.Thread(target=self.server.loop, daemon=True)
        self._worker.start()

        # Wait for server to be ready
        host = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        _wait_for_server(host, self.server.port)
        # expose client placeholder for tearDown cleanup
        self.client = None

    def tearDown(self) -> None:
        try:
            self.facade.stop()
        finally:
            # restore serial module
            if self._orig_serial is not None:
                sys.modules["serial"] = self._orig_serial
            else:
                sys.modules.pop("serial", None)
            try:
                self._worker.join(timeout=1.0)
            except Exception:
                pass
            # Remove the temp file
            try:
                if isinstance(self.cfg_path, Path) and self.cfg_path.exists():
                    self.cfg_path.unlink()
            except Exception:
                pass
            # Close any vxi11 client socket
            try:
                if getattr(self, 'client', None) is not None:
                    sock = getattr(self.client, 'sock', None)
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
            except Exception:
                pass

    def test_scpi_commands_pass_through(self) -> None:
        # Connect using vxi11 client
        from vxi11 import vxi11 as vxi11_proto

        host = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host, port=self.server.port)
        # expose client for tearDown cleanup
        self.client = client
        client.sock.settimeout(1.0)

        # create_link with lock requested so adapter.acquire() opens the fake serial
        res = client.create_link(0x5000, True, 1000, b"mock_dmm")
        # create_link returns a tuple (err, lid, ...); index into it to satisfy static analyzers
        err = res[0]
        lid = res[1]
        # max_recv may be available at index 3
        max_recv = res[3] if len(res) > 3 else None
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(lid, 0)

        # Write *IDN? (adapter should append termination)
        err, written = client.device_write(lid, 1000, 0, 0, b"*IDN?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(written, 0)

        # Read response
        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertIn(b"MOCK INSTRUMENTS INC.", payload.upper())

        # Send MEAS:VOLT? and expect 5.0000
        err, written = client.device_write(lid, 1000, 0, 0, b"MEAS:VOLT?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(payload.strip(), b"5.0000")

        # Unlock and destroy link
        err = client.device_unlock(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        # destroy_link should succeed
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
