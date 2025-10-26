"""Unit tests for MODBUS serial adapters using a fake serial backend."""

from __future__ import annotations

import asyncio
import binascii
import struct
import sys
import threading
import types
import unittest
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
	sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.adapters.modbus_ascii import ModbusAsciiAdapter
from vxi_proxy.adapters.modbus_rtu import ModbusRtuAdapter
from vxi_proxy.serial_manager import SerialPortManager


def _crc16(data: bytes) -> int:
	crc = 0xFFFF
	for byte in data:
		crc ^= byte
		for _ in range(8):
			if crc & 0x0001:
				crc = (crc >> 1) ^ 0xA001
			else:
				crc >>= 1
	return crc & 0xFFFF


def _lrc(data: bytes) -> int:
	total = sum(data) & 0xFF
	return ((-total) & 0xFF)


class FakeModbusBus:
	"""Simulate a small MODBUS device with deterministic register values."""

	def __init__(self) -> None:
		self._state_lock = threading.Lock()
		self._active = 0
		self._max_active = 0
		self._holding: Dict[int, list[int]] = {}
		self._inputs: Dict[int, list[int]] = {}
		self.reset()

	def reset(self) -> None:
		with self._state_lock:
			self._active = 0
			self._max_active = 0
			self._holding = {}
			self._inputs = {}
			for unit in (1, 2):
				holding = [0] * 200
				inputs = [0] * 100

				temp_regs = struct.unpack(">HH", struct.pack(">f", 25.5))
				holding[0:2] = list(temp_regs)

				setpoint_regs = struct.unpack(">HH", struct.pack(">f", 20.0))
				holding[100:102] = list(setpoint_regs)

				voltage = 12345
				inputs[0:2] = [(voltage >> 16) & 0xFFFF, voltage & 0xFFFF]

				self._holding[unit] = holding
				self._inputs[unit] = inputs

	@property
	def max_active(self) -> int:
		with self._state_lock:
			return self._max_active

	def process_request(self, protocol: str, raw: bytes) -> bytes:
		with self._state_lock:
			self._active += 1
			if self._active > self._max_active:
				self._max_active = self._active

		try:
			if protocol == "ascii":
				unit, function, payload = self._parse_ascii_request(raw)
			else:
				unit, function, payload = self._parse_rtu_request(raw)

			response_payload = self._execute(unit, function, payload)

			if protocol == "ascii":
				return self._build_ascii_response(unit, function, response_payload)
			return self._build_rtu_response(unit, function, response_payload)
		finally:
			with self._state_lock:
				self._active -= 1

	# ------------------------------------------------------------------
	# Request decoding helpers
	# ------------------------------------------------------------------

	def _parse_rtu_request(self, raw: bytes) -> Tuple[int, int, bytes]:
		if len(raw) < 4:
			raise ValueError("RTU frame too short")
		crc_expected = _crc16(raw[:-2])
		crc_received = raw[-2] | (raw[-1] << 8)
		if crc_expected != crc_received:
			raise ValueError("RTU CRC mismatch")
		return raw[0], raw[1], raw[2:-2]

	def _parse_ascii_request(self, raw: bytes) -> Tuple[int, int, bytes]:
		if not raw.startswith(b":") or not raw.endswith(b"\r\n"):
			raise ValueError("ASCII frame delimiters invalid")
		hex_payload = raw[1:-2]
		data = binascii.unhexlify(hex_payload)
		if len(data) < 3:
			raise ValueError("ASCII payload too short")
		unit = data[0]
		checksum = data[-1]
		payload = data[:-1]
		if checksum != _lrc(payload):
			raise ValueError("ASCII LRC mismatch")
		return unit, payload[1], payload[2:]

	# ------------------------------------------------------------------
	# Response builders
	# ------------------------------------------------------------------

	def _build_rtu_response(self, unit: int, function: int, payload: bytes) -> bytes:
		frame = bytes([unit, function]) + payload
		crc = _crc16(frame)
		return frame + struct.pack("<H", crc)

	def _build_ascii_response(self, unit: int, function: int, payload: bytes) -> bytes:
		body = bytes([unit, function]) + payload
		checksum = _lrc(body)
		frame = body + bytes([checksum])
		return b":" + binascii.hexlify(frame).upper() + b"\r\n"

	# ------------------------------------------------------------------
	# MODBUS function emulation
	# ------------------------------------------------------------------

	def _execute(self, unit: int, function: int, payload: bytes) -> bytes:
		if unit not in self._holding:
			raise ValueError("Unknown unit")

		if function in (0x03, 0x04):
			address, count = struct.unpack(">HH", payload[:4])
			registers = (
				self._holding[unit] if function == 0x03 else self._inputs[unit]
			)
			values = registers[address : address + count]
			data = bytearray([len(values) * 2])
			for value in values:
				data += struct.pack(">H", value)
			return bytes(data)

		if function == 0x06:
			address, value = struct.unpack(">HH", payload[:4])
			self._holding[unit][address] = value
			return payload[:4]

		if function == 0x10:
			address, count, byte_count = struct.unpack(">HHB", payload[:5])
			values = []
			for idx in range(count):
				start = 5 + idx * 2
				values.append(struct.unpack(">H", payload[start : start + 2])[0])
			self._holding[unit][address : address + count] = values
			return struct.pack(">HH", address, count)

		raise ValueError(f"Unsupported function code {function:#x}")


class FakeSerial:
	"""Minimal pyserial Serial stub backed by FakeModbusBus."""

	bus = FakeModbusBus()

	def __init__(self, *_, **kwargs):
		self.timeout = kwargs.get("timeout", 1.0)
		self.write_timeout = kwargs.get("write_timeout", 1.0)
		self.is_open = True
		self.port = kwargs.get("port", "COM_FAKE")
		self._rx: Deque[bytes] = deque()

	def close(self) -> None:
		self.is_open = False

	def reset_input_buffer(self) -> None:
		self._rx.clear()

	def reset_output_buffer(self) -> None:  # pragma: no cover - no-op
		return None

	def flush(self) -> None:  # pragma: no cover - no-op
		return None

	def write(self, data: bytes) -> int:
		protocol = "ascii" if data.startswith(b":") else "rtu"
		response = self.bus.process_request(protocol, bytes(data))
		chunk = 4 if protocol == "rtu" else 6
		for idx in range(0, len(response), chunk):
			self._rx.append(response[idx : idx + chunk])
		return len(data)

	def read(self, size: int) -> bytes:
		if not self._rx:
			return b""
		chunk = self._rx.popleft()
		if len(chunk) <= size:
			return chunk
		self._rx.appendleft(chunk[size:])
		return chunk[:size]


class ModbusSerialAdapterTests(unittest.TestCase):
	def setUp(self) -> None:
		self._orig_serial = sys.modules.get("serial")
		FakeSerial.bus.reset()
		sys.modules["serial"] = types.SimpleNamespace(Serial=FakeSerial)  # type: ignore

	def tearDown(self) -> None:
		if self._orig_serial is None:
			sys.modules.pop("serial", None)
		else:
			sys.modules["serial"] = self._orig_serial

		loop = asyncio.new_event_loop()
		try:
			loop.run_until_complete(SerialPortManager.reset())
		finally:
			loop.close()

	def test_modbus_rtu_temperature_read(self) -> None:
		mappings = [
			{
				"pattern": r"MEAS:TEMP\?",
				"action": "read_holding_registers",
				"params": {"address": 0, "count": 2, "data_type": "float32_be"},
			}
		]

		adapter = ModbusRtuAdapter(
			"rtu0",
			port="COM_FAKE",
			baudrate=19200,
			unit_id=1,
			mappings=mappings,
		)

		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(adapter.connect())
			loop.run_until_complete(adapter.write(b"MEAS:TEMP?"))
			data = loop.run_until_complete(adapter.read(256))
			temp = float(data.decode("ascii"))
			self.assertAlmostEqual(temp, 25.5, places=2)
		finally:
			loop.run_until_complete(adapter.disconnect())
			loop.close()
			asyncio.set_event_loop(None)

	def test_modbus_ascii_voltage_read(self) -> None:
		mappings = [
			{
				"pattern": r"MEAS:VOLT\?",
				"action": "read_input_registers",
				"params": {"address": 0, "count": 2, "data_type": "uint32_be"},
			}
		]

		adapter = ModbusAsciiAdapter(
			"ascii0",
			port="COM_FAKE_ASCII",
			baudrate=9600,
			unit_id=1,
			mappings=mappings,
		)

		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			loop.run_until_complete(adapter.connect())
			loop.run_until_complete(adapter.write(b"MEAS:VOLT?"))
			data = loop.run_until_complete(adapter.read(256))
			voltage = int(data.decode("ascii"))
			self.assertEqual(voltage, 12345)
		finally:
			loop.run_until_complete(adapter.disconnect())
			loop.close()
			asyncio.set_event_loop(None)

	def test_shared_serial_port_arbitration(self) -> None:
		mappings = [
			{
				"pattern": r"MEAS:TEMP\?",
				"action": "read_holding_registers",
				"params": {"address": 0, "count": 2, "data_type": "float32_be"},
			}
		]

		adapter_a = ModbusRtuAdapter(
			"rtu_a",
			port="COM_SHARED",
			baudrate=19200,
			unit_id=1,
			mappings=mappings,
		)

		adapter_b = ModbusRtuAdapter(
			"rtu_b",
			port="COM_SHARED",
			baudrate=19200,
			unit_id=2,
			mappings=mappings,
		)

		async def run_transaction(adapter: ModbusRtuAdapter) -> float:
			await adapter.write(b"MEAS:TEMP?")
			payload = await adapter.read(256)
			return float(payload.decode("ascii"))

		async def scenario() -> Tuple[float, float]:
			await adapter_a.connect()
			await adapter_b.connect()
			try:
				results = await asyncio.gather(
					run_transaction(adapter_a),
					run_transaction(adapter_b),
				)
				return results[0], results[1]
			finally:
				await adapter_a.disconnect()
				await adapter_b.disconnect()

		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		try:
			temp_a, temp_b = loop.run_until_complete(scenario())
			self.assertAlmostEqual(temp_a, 25.5, places=2)
			self.assertAlmostEqual(temp_b, 25.5, places=2)
			self.assertEqual(FakeSerial.bus.max_active, 1)
		finally:
			loop.close()
			asyncio.set_event_loop(None)


if __name__ == "__main__":
	unittest.main()
