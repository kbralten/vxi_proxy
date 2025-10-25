"""Integration test: VXI-11 -> MODBUS-TCP adapter -> mock MODBUS server.

This test starts the mock_modbus_server.py as a subprocess, writes a temp
YAML config with mapping rules, starts the VXI-11 facade, and exercises
SCPI-style commands that are translated to MODBUS operations.
"""

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
    """Wait for server to accept connections."""
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


class ModbusTcpIntegrationTests(unittest.TestCase):
    """Integration test for MODBUS-TCP adapter."""
    
    def setUp(self) -> None:
        # Skip if MODBUS_INTEGRATION_TEST not set
        if not os.getenv("MODBUS_INTEGRATION_TEST"):
            self.skipTest("Set MODBUS_INTEGRATION_TEST=1 to run this test")
        
        # Create temporary config with MODBUS device and mapping rules
        self.cfg_file = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml")
        host = "127.0.0.1"
        port = 5020
        self.mock_port = port
        
        # Config with mappings
        config = f"""server:
  host: 127.0.0.1
  port: 0

devices:
  mock_modbus:
    type: modbus-tcp
    host: {host}
    port: {port}
    unit_id: 1
    timeout: 2.0
    requires_lock: false
    mappings:
      - pattern: 'MEAS:TEMP\\?'
        action: read_holding_registers
        params:
          address: 0
          count: 2
          data_type: float32_be
      
      - pattern: 'MEAS:VOLT\\?'
        action: read_input_registers
        params:
          address: 0
          count: 2
          data_type: uint32_be
      
      - pattern: 'SOUR:TEMP\\s+(\\d+\\.?\\d*)'
        action: write_holding_registers
        params:
          address: 100
          value: '$1'
          data_type: float32_be
      
      - pattern: 'SOUR:VOLT\\s+(\\d+)'
        action: write_single_register
        params:
          address: 200
          value: '$1'
          data_type: uint16
"""
        
        self.cfg_file.write(config)
        self.cfg_file.flush()
        try:
            self.cfg_file.close()
        except Exception:
            pass
        self.cfg_path = Path(self.cfg_file.name)
        
        # Start mock MODBUS server subprocess
        python = sys.executable
        cmd = [
            python, "-u",
            str(PROJECT_ROOT / "tools" / "mock_modbus_server.py"),
            "--host", host,
            "--port", str(port),
        ]
        self.mock_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        
        # Wait for mock server readiness
        _wait_for_server(host, port, timeout=10.0)
        
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
    
    def test_modbus_read_temperature(self) -> None:
        """Test reading temperature via MODBUS holding registers."""
        from vxi11 import vxi11 as vxi11_proto
        
        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(2.0)
        
        # create_link
        res = client.create_link(0x5000, False, 1000, b"mock_modbus")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        
        # Write query command (will be translated to MODBUS read holding registers)
        err, written = client.device_write(lid, 1000, 0, 0, b"MEAS:TEMP?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(written, 0)
        
        # Read response (should be 25.5 from mock server)
        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        response = payload.decode("ascii").strip()
        
        # Check temperature value
        temp = float(response)
        self.assertAlmostEqual(temp, 25.5, places=2)
        
        # Destroy link
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
    
    def test_modbus_read_voltage(self) -> None:
        """Test reading voltage via MODBUS input registers."""
        from vxi11 import vxi11 as vxi11_proto
        
        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(2.0)
        
        # create_link
        res = client.create_link(0x5000, False, 1000, b"mock_modbus")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        
        # Write query command (will be translated to MODBUS read input registers)
        err, written = client.device_write(lid, 1000, 0, 0, b"MEAS:VOLT?")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        
        # Read response (should be 12345 from mock server)
        err, reason, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        response = payload.decode("ascii").strip()
        
        # Check voltage value
        voltage = int(response)
        self.assertEqual(voltage, 12345)
        
        # Destroy link
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
    
    def test_modbus_write_temperature_setpoint(self) -> None:
        """Test writing temperature setpoint via MODBUS write multiple registers."""
        from vxi11 import vxi11 as vxi11_proto
        
        host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
        client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
        self.client = client
        client.sock.settimeout(2.0)
        
        # create_link
        res = client.create_link(0x5000, False, 1000, b"mock_modbus")
        err = res[0]
        lid = res[1]
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        
        # Write command with value (will be translated to MODBUS write multiple registers)
        err, written = client.device_write(lid, 1000, 0, 0, b"SOUR:TEMP 30.5")
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)
        self.assertGreater(written, 0)
        
        # Write commands don't produce query responses in this adapter
        # Success is indicated by no error
        
        # Destroy link
        err = client.destroy_link(lid)
        self.assertEqual(err, vxi11_proto.ERR_NO_ERROR)


if __name__ == "__main__":
    unittest.main()
