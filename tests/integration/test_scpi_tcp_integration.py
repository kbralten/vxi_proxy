"""Integration test: VXI-11 -> SCPI-TCP adapter -> mock TCP instrument.

This test starts the mock_scpi_tcp_server.py as a subprocess, writes a temp
YAML config pointing at it, starts the VXI-11 facade, and exercises create_link,
device_write, and device_read. Teardown is defensive and closes sockets and
terminates the mock subprocess.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
import subprocess
import os
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


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


class ScpiTcpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        # Create a temporary config file pointing to a scpi-tcp device
        self.cfg_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml")
        host = "127.0.0.1"
        port = 5555
        self.mock_port = port
        self.cfg_file.write(
            f"server:\n  host: 127.0.0.1\n  port: 0\n\n"
            f"devices:\n  mock_dmm:\n    type: scpi-tcp\n    host: {host}\n    port: {port}\n    io_timeout: 0.5\n    write_termination: '\\n'\n    read_termination: '\\n'\n"
        )
        self.cfg_file.flush()
        try:
            self.cfg_file.close()
        except Exception:
            pass
        self.cfg_path = Path(self.cfg_file.name)

        # Start mock server subprocess
        python = sys.executable
        cmd = [python, "-u", str(PROJECT_ROOT / "tools" / "mock_scpi_tcp_server.py"), "--host", host, "--port", str(port)]
        self.mock_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Wait for mock server readiness
        _wait_for_server(host, port)

        # Start the VXI-11 facade
        from vxi_proxy.server import Vxi11ServerFacade

        self.facade = Vxi11ServerFacade(self.cfg_path)
        ctx = self.facade.start()
        self.server = ctx.server
        self.runtime = ctx.runtime

        # Run server loop in background thread
        self._worker = threading.Thread(target=self.server.loop, daemon=True)
        self._worker.start()
        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        _wait_for_server(host_srv, self.server.port)

        # Expose client placeholder for tearDown
        self.client = None

    def tearDown(self) -> None:
        try:
            self.facade.stop()
        finally:
            try:
                if self._worker.is_alive():
                    self._worker.join(timeout=1.0)
            except Exception:
                pass
            try:
                if isinstance(self.cfg_path, Path) and self.cfg_path.exists():
                    self.cfg_path.unlink()
            except Exception:
                pass
            try:
                if getattr(self, "client", None) is not None:
                    sock = getattr(self.client, "sock", None)
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
            except Exception:
                pass
            # Terminate mock subprocess
            try:
                if self.mock_proc.poll() is None:
                    self.mock_proc.terminate()
                    try:
                        self.mock_proc.wait(timeout=1.0)
                    except Exception:
                        self.mock_proc.kill()
                # close pipes
                for s in (self.mock_proc.stdout, self.mock_proc.stderr):
                    try:
                        if s:
                            s.close()
                    except Exception:
                        pass
            except Exception:
                pass

    def test_scpi_tcp_roundtrip(self) -> None:
        from vxi11 import vxi11 as vxi11_proto

        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(2.0)

        # create_link without lock (TCP default is requires_lock=False)
        res = client.create_link(0x5000, False, 1000, b"mock_dmm")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        # Write *IDN?
        err, written = client.device_write(lid, 1000, 0, 0, b"*IDN?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(written, 0)

        # Read response
        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertIn(b"MOCK INSTRUMENTS INC.", payload.upper())

        # Destroy link
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
