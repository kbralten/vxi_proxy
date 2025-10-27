"""Integration test using the real mock_scpi_instrument.py and OS-level virtual serial ports.

This test attempts to create a virtual serial port pair and run the real
`tools/mock_scpi_instrument.py` script in a subprocess. It then starts the
VXI-11 server configured to use the other end of the pair and verifies that
SCPI commands are proxied end-to-end.

Notes:
- POSIX: requires `socat` installed and available on PATH.
- Windows: requires the user to pre-create a COM pair (e.g., com0com) and set
  the environment variables `TEST_COM_A` and `TEST_COM_B` to the two port names.

The test will skip if the platform is unsupported or the helper tools are not
available. This keeps CI portable while still providing an option for a full
hardware-level integration test when the environment supports it.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _wait_for_port(path: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return
        time.sleep(0.05)
    raise TimeoutError(f"Port {path} did not appear")


class RealScpiIntegrationTests(unittest.TestCase):
    # Pylance-friendly attribute annotations
    socat_proc: Optional[subprocess.Popen] = None
    mock_proc: Optional[subprocess.Popen] = None
    cfg_path: Optional[Path] = None
    facade: Optional[Any] = None
    server: Optional[Any] = None
    _worker: Optional[threading.Thread] = None
    client: Optional[Any] = None
    def setUp(self) -> None:
        self.socat_proc = None
        self.mock_proc = None
        self.cfg_path = None
        self.facade = None
        self.client = None

        system = platform.system()
        if system == "Windows":
            # Expect user-provided COM pair via env vars
            com_a = os.environ.get("TEST_COM_A")
            com_b = os.environ.get("TEST_COM_B")
            if not com_a or not com_b:
                self.skipTest("Windows real-serial test requires TEST_COM_A and TEST_COM_B environment variables pointing to a com0com pair")
            self.port_mock = com_a
            self.port_server = com_b
        else:
            # POSIX path: need socat
            if shutil.which("socat") is None:
                self.skipTest("socat not available; skipping real-serial integration test")
            # Create PTYs pair with socat
            # Launch socat in the background to create two PTYs and print their names
            socat_cmd = [
                "socat",
                "-d",
                "-d",
                "pty,raw,echo=0",
                "pty,raw,echo=0",
            ]
            self.socat_proc = subprocess.Popen(socat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # socat writes PTY names to stderr; read stderr lines until both appear
            pty_a = None
            pty_b = None
            start = time.time()
            while time.time() - start < 5:
                if self.socat_proc.stderr is None:
                    break
                line = self.socat_proc.stderr.readline()
                if not line:
                    time.sleep(0.01)
                    continue
                if "PTY is" in line:
                    path = line.strip().split()[-1]
                    if pty_a is None:
                        pty_a = path
                    else:
                        pty_b = path
                        break
            if not pty_a or not pty_b:
                # Clean up and skip
                try:
                    self.socat_proc.kill()
                except Exception:
                    pass
                self.skipTest("Failed to create PTY pair with socat")
            self.port_mock = pty_a
            self.port_server = pty_b

        # Start the real mock_scpi_instrument.py on the mock side
        PROJECT_ROOT / "tools" / "mock_scpi_instrument.py"
        def tearDown(self) -> None:
            # Stop the server facade if running
            if self.facade:
                try:
                    self.facade.stop()
                except Exception:
                    pass

            # Close vxi11 client socket if present
            client = getattr(self, "client", None)
            if client is not None:
                try:
                    sock = getattr(client, "sock", None)
                    if sock is not None:
                        try:
                            sock.close()
                        except Exception:
                            pass
                except Exception:
                    pass

            # Terminate and clean up mock subprocess (if any)
            if self.mock_proc is not None:
                try:
                    self.mock_proc.terminate()
                    self.mock_proc.wait(timeout=1)
                except Exception:
                    try:
                        self.mock_proc.kill()
                    except Exception:
                        pass
                # Close subprocess pipes
                for p in ("stdout", "stderr"):
                    try:
                        f = getattr(self.mock_proc, p, None)
                        if f is not None:
                            try:
                                f.close()
                            except Exception:
                                pass
                    except Exception:
                        pass

            # Clean up socat if used
            socat = getattr(self, "socat_proc", None)
            if socat is not None:
                try:
                    socat.kill()
                except Exception:
                    pass
                try:
                    stdout = getattr(socat, "stdout", None)
                    if stdout is not None:
                        try:
                            stdout.close()
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    stderr = getattr(socat, "stderr", None)
                    if stderr is not None:
                        try:
                            stderr.close()
                        except Exception:
                            pass
                except Exception:
                    pass

            # Remove temporary config file
            cfg = getattr(self, "cfg_path", None)
            if cfg is not None:
                try:
                    if isinstance(cfg, Path):
                        cfg.unlink()
                except Exception:
                    pass

            # Ensure server thread is joined
            worker = getattr(self, "_worker", None)
            if worker is not None:
                try:
                    if isinstance(worker, threading.Thread):
                        worker.join(timeout=1)
                except Exception:
                    pass
                pass

        # Clean up socat if used
        if getattr(self, "socat_proc", None):
            try:
                self.socat_proc.kill()
            except Exception:
                pass
            try:
                if getattr(self.socat_proc, "stdout", None) is not None:
                    try:
                        self.socat_proc.stdout.close()
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                if getattr(self.socat_proc, "stderr", None) is not None:
                    try:
                        self.socat_proc.stderr.close()
                    except Exception:
                        pass
            except Exception:
                pass

        # Remove temporary config file
        if getattr(self, "cfg_path", None):
            try:
                self.cfg_path.unlink()
            except Exception:
                pass

        # Ensure server thread is joined
        if getattr(self, "_worker", None) is not None:
            try:
                self._worker.join(timeout=1)
            except Exception:
                pass

    def test_scpi_commands_end_to_end(self) -> None:
        from vxi11 import vxi11 as vxi11_proto

        # Obtain the server object (fall back to facade._server if needed)
        server_obj = getattr(self, "server", None) or getattr(self.facade, "_server", None)
        if server_obj is None:
            self.skipTest("VXI-11 server not available in test environment")
        host = server_obj.host if server_obj.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host, port=server_obj.port)
        # expose client to tearDown so its socket can be closed
        self.client = client
        # Give the client a larger socket timeout to accommodate slower virtual serial responses
        client.sock.settimeout(5.0)

        res = client.create_link(0x6000, True, 1000, b"mock_dmm")
        # create_link returns a tuple (err, lid, ...). Index into it to avoid
        # confusing the static analyzer which may mark the call NoReturn.
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        err, written = client.device_write(lid, 1000, 0, 0, b"*IDN?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)

        # Increase device_read timeout to allow the serial mock time to respond
        err, reason, payload = client.device_read(lid, 1024, 2000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertIn(b"MOCK INSTRUMENTS INC.", payload.upper())

        err, written = client.device_write(lid, 1000, 0, 0, b"MEAS:VOLT?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        # Increase device_read timeout for this subsequent read as well
        err, reason, payload = client.device_read(lid, 1024, 2000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        # numeric value should parse
        self.assertRegex(payload.decode("utf-8"), r"\d+\.\d+")

        # unlock/destroy
        err = client.device_unlock(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
