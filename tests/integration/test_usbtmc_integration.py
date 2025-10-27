"""Gated integration test for USBTMC.

This test is skipped unless HAVE_USBTMC_DEVICE=1 and VID/PID env vars
are provided (optional SERIAL). It exercises a basic write/read against
an actual USBTMC instrument via the VXI-11 façade.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

HAVE_DEVICE = os.environ.get("HAVE_USBTMC_DEVICE") == "1"
VID = os.environ.get("DEVICE_VID")
PID = os.environ.get("DEVICE_PID")
SERIAL = os.environ.get("DEVICE_SERIAL")


@unittest.skipUnless(HAVE_DEVICE and VID and PID, "USBTMC device not available for integration test")
class UsbTmcIntegrationTests(unittest.TestCase):
    def test_vxi11_roundtrip(self) -> None:
        # Import here to avoid heavy deps when skipped
        from vxi_proxy.server import Vxi11ServerFacade  # type: ignore
        from vxi11 import vxi11 as vxi11_client  # type: ignore
        import tempfile
        import yaml

        vid = int(VID, 0)
        pid = int(PID, 0)
        serial = SERIAL

        # Create a temporary config with usbtmc device
        cfg = {
            "server": {"host": "127.0.0.1", "port": 0, "portmapper_enabled": False},
            "devices": {
                "usbtmc0": {
                    "type": "usbtmc",
                    "settings": {
                        "vid": vid,
                        "pid": pid,
                        **({"serial": serial} if serial else {}),
                        "timeout": 1.0,
                        "write_termination": "\n",
                        "read_termination": "\n",
                    },
                }
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tf:
            yaml.safe_dump(cfg, tf)
            cfg_path = Path(tf.name)

        facade = Vxi11ServerFacade(cfg_path)
        ctx = facade.start()
        try:
            # Connect to the VXI11 server using client
            client = vxi11_client.Instrument("127.0.0.1", b"usbtmc0", ctx.server.port)
            # Basic roundtrip: *IDN? (may not be supported; if empty, we still validate no crash)
            client.write(b"*IDN?\n")
            data = client.read(1024)
            # Only check type; content depends on device
            self.assertIsInstance(data, (bytes, bytearray))
        finally:
            facade.stop()
            try:
                os.unlink(cfg_path)
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
