"""Integration test for the generic regex adapter with the mock server."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
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


class GenericRegexIntegrationTests(unittest.TestCase):
    """Validate end-to-end flow through VXI-11 and the generic regex adapter."""

    def setUp(self) -> None:
        if not os.getenv("GENERIC_PROTOCOL_INTEGRATION"):
            self.skipTest("Set GENERIC_PROTOCOL_INTEGRATION=1 to run this test")

        self.cfg_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml")
        host = "127.0.0.1"
        # Pick an ephemeral free port to avoid conflicts between test runs
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            _sock.bind((host, 0))
            port = _sock.getsockname()[1]
        self.mock_port = port
        self.mock_host = host

        config = f"""server:
  host: 127.0.0.1
  port: 0

devices:
  custom_device:
    type: generic-regex
    transport: tcp
    host: {host}
    port: {port}
    # Increased io_timeout to reduce flakiness in CI/test harness where
    # subprocess scheduling can delay the mock server reply slightly.
    io_timeout: 2.0
    mappings:
      - pattern: '^STAT\\s*$'
        request_format: 'STATUS\\n'
        expects_response: true
        response_regex: '^OK TEMP=(?P<temp>\\d+\\.\\d+) MODE=(?P<mode>\\w+)'
        response_format: 'TEMP=$temp\\nMODE=$mode\\n'

      - pattern: '^SET:MODE\\s+(\\w+)$'
        request_format: 'MODE $1\\n'
        expects_response: false
"""

        self.cfg_file.write(config)
        self.cfg_file.flush()
        try:
            self.cfg_file.close()
        except Exception:
            pass
        self.cfg_path = Path(self.cfg_file.name)

        python = sys.executable
        cmd = [
            python,
            "-u",
            str(PROJECT_ROOT / "tools" / "mock_generic_protocol.py"),
            "--host",
            host,
            "--port",
            str(port),
        ]
        self.mock_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _wait_for_server(host, port, timeout=10.0)

        from vxi_proxy.server import Vxi11ServerFacade

        self.facade = Vxi11ServerFacade(self.cfg_path)
        ctx = self.facade.start()
        self.server = ctx.server
        self.runtime = ctx.runtime

        self._worker = threading.Thread(target=self.server.loop, daemon=True)
        self._worker.start()
        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        _wait_for_server(host_srv, self.server.port)

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
            try:
                if self.mock_proc.poll() is None:
                    self.mock_proc.terminate()
                    try:
                        self.mock_proc.wait(timeout=1.0)
                    except Exception:
                        self.mock_proc.kill()
                for stream in (self.mock_proc.stdout, self.mock_proc.stderr):
                    try:
                        if stream:
                            stream.close()
                    except Exception:
                        pass
            except Exception:
                pass

    def test_status_query(self) -> None:
        from vxi11 import vxi11 as vxi11_proto

        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(5.0)

        res = client.create_link(0x5000, False, 1000, b"custom_device")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err, written = client.device_write(lid, 1000, 0, 0, b"STAT")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(written, len(b"STAT"))

        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(reason, vxi11_proto.RX_END)
        response = payload.decode("ascii").strip()
        lines = response.split("\n")
        self.assertIn("TEMP=", lines[0])
        self.assertIn("MODE=", lines[1])

        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

    def test_fire_and_forget_command(self) -> None:
        from vxi11 import vxi11 as vxi11_proto

        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(5.0)

        res = client.create_link(0x5000, False, 1000, b"custom_device")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err, written = client.device_write(lid, 1000, 0, 0, b"SET:MODE AUTO")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(written, len(b"SET:MODE AUTO"))

        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertEqual(payload, b"")
        self.assertEqual(reason, 0)

        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
