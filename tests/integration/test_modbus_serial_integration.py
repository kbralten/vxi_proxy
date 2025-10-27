"""Integration tests for MODBUS serial adapters (RTU & ASCII)."""

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


def _wait_for_tcp_server(host: str, port: int, timeout: float = 5.0) -> None:
	deadline = time.time() + timeout
	last_error: Exception | None = None
	while time.time() < deadline:
		try:
			with socket.create_connection((host, port), timeout=0.5):
				return
		except OSError as exc:  # pragma: no cover - transient
			last_error = exc
			time.sleep(0.05)
	raise TimeoutError(f"Server {host}:{port} did not become ready") from last_error


class ModbusSerialIntegrationTests(unittest.TestCase):
	def setUp(self) -> None:
		if not os.getenv("MODBUS_SERIAL_INTEGRATION_TEST"):
			self.skipTest("Set MODBUS_SERIAL_INTEGRATION_TEST=1 to run these tests")

		self._tmp_config = tempfile.NamedTemporaryFile("w", delete=False, suffix=".yaml")
		self.cfg_path = Path(self._tmp_config.name)

		self.rtu_port = 6200
		self.ascii_port = 6201
		self.serial_host = "127.0.0.1"

		config = f"""server:
  host: 127.0.0.1
  port: 0

devices:
  modbus_rtu_u1:
	type: modbus-rtu
	port: socket://{self.serial_host}:{self.rtu_port}
	baudrate: 19200
	timeout: 2.0
	unit_id: 1
	mappings:
	  - pattern: 'MEAS:TEMP\\?'
		action: read_holding_registers
		params:
		  address: 0
		  count: 2
		  data_type: float32_be

  modbus_rtu_u2:
	type: modbus-rtu
	port: socket://{self.serial_host}:{self.rtu_port}
	baudrate: 19200
	timeout: 2.0
	unit_id: 2
	mappings:
	  - pattern: 'MEAS:TEMP\\?'
		action: read_holding_registers
		params:
		  address: 0
		  count: 2
		  data_type: float32_be

  modbus_ascii:
	type: modbus-ascii
	port: socket://{self.serial_host}:{self.ascii_port}
	baudrate: 9600
	timeout: 2.0
	unit_id: 1
	mappings:
	  - pattern: 'MEAS:VOLT\\?'
		action: read_input_registers
		params:
		  address: 0
		  count: 2
		  data_type: uint32_be
"""

		self._tmp_config.write(config)
		self._tmp_config.flush()
		self._tmp_config.close()

		python = sys.executable
		server_script = PROJECT_ROOT / "tools" / "mock_modbus_server.py"
		cmd = [
			python,
			"-u",
			str(server_script),
			"--serial-host",
			self.serial_host,
			"--rtu-port",
			str(self.rtu_port),
			"--ascii-port",
			str(self.ascii_port),
			"--units",
			"1,2",
			"--no-tcp",
		]

		self.mock_proc = subprocess.Popen(
			cmd,
			stdout=subprocess.PIPE,
			stderr=subprocess.PIPE,
		)

		_wait_for_tcp_server(self.serial_host, self.rtu_port, timeout=10.0)
		_wait_for_tcp_server(self.serial_host, self.ascii_port, timeout=10.0)

		from vxi_proxy.server import Vxi11ServerFacade

		self.facade = Vxi11ServerFacade(self.cfg_path)
		ctx = self.facade.start()
		self.server = ctx.server
		self.runtime = ctx.runtime

		self._worker = threading.Thread(target=self.server.loop, daemon=True)
		self._worker.start()

		host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
		_wait_for_tcp_server(host_srv, self.server.port, timeout=10.0)

	def tearDown(self) -> None:  # noqa: D401
		try:
			self.facade.stop()
		finally:
			try:
				if self._worker.is_alive():
					self._worker.join(timeout=1.0)
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
				if stream:
					stream.close()
		except Exception:
			pass

		try:
			if self.cfg_path.exists():
				self.cfg_path.unlink()
		except Exception:
			pass

	def _open_client(self):
		from vxi11 import vxi11 as vxi11_proto

		host_srv = self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1"
		client = vxi11_proto.CoreClient(host_srv, port=self.server.port)
		client.sock.settimeout(2.0)
		return client

	def _read_temperature(self, device_name: bytes) -> float:
		client = self._open_client()
		try:
			res = client.create_link(0x6000, False, 1000, device_name)
			err = res[0]
			lid = res[1]
			self.assertEqual(err, 0)
			err, _ = client.device_write(lid, 1000, 0, 0, b"MEAS:TEMP?")
			self.assertEqual(err, 0)
			err, _, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
			self.assertEqual(err, 0)
			client.destroy_link(lid)
			return float(payload.decode("ascii"))
		finally:
			sock = getattr(client, "sock", None)
			if sock is not None:
				sock.close()

	def test_modbus_rtu_temperature_read(self) -> None:
		temp = self._read_temperature(b"modbus_rtu_u1")
		self.assertAlmostEqual(temp, 25.5, places=2)

	def test_modbus_ascii_voltage_read(self) -> None:

		client = self._open_client()
		try:
			res = client.create_link(0x6001, False, 1000, b"modbus_ascii")
			err = res[0]
			lid = res[1]
			self.assertEqual(err, 0)
			err, _ = client.device_write(lid, 1000, 0, 0, b"MEAS:VOLT?")
			self.assertEqual(err, 0)
			err, _, payload = client.device_read(lid, 1024, 1000, 0, 0, 0)
			self.assertEqual(err, 0)
			voltage = int(payload.decode("ascii"))
			self.assertEqual(voltage, 12345)
			client.destroy_link(lid)
		finally:
			sock = getattr(client, "sock", None)
			if sock is not None:
				sock.close()

	def test_shared_serial_port_concurrency(self) -> None:
		results = [0.0, 0.0]

		def worker(name: bytes, index: int) -> None:
			temp = self._read_temperature(name)
			results[index] = temp

		threads = [
			threading.Thread(target=worker, args=(b"modbus_rtu_u1", 0)),
			threading.Thread(target=worker, args=(b"modbus_rtu_u2", 1)),
		]

		for t in threads:
			t.start()
		for t in threads:
			t.join()

		self.assertAlmostEqual(results[0], 25.5, places=2)
		self.assertAlmostEqual(results[1], 25.5, places=2)


if __name__ == "__main__":
	unittest.main()
