"""MODBUS-RTU adapter implementation."""

from __future__ import annotations

import asyncio
import struct
import time
from typing import Any

from .base import AdapterError
from .modbus_serial_base import ModbusSerialAdapterBase
from ..mapping_engine import ModbusAction


def _crc16(data: bytes) -> int:
	"""Compute MODBUS RTU CRC16 (little-endian)."""

	crc = 0xFFFF
	for byte in data:
		crc ^= byte
		for _ in range(8):
			if crc & 0x0001:
				crc = (crc >> 1) ^ 0xA001
			else:
				crc >>= 1
	return crc & 0xFFFF


class ModbusRtuAdapter(ModbusSerialAdapterBase):
	"""Adapter for MODBUS devices reachable over RTU serial links."""

	PROTOCOL_NAME = "RTU"

	async def _perform_transaction(self, serial_obj: Any, action: ModbusAction, pdu: bytes) -> bytes:
		frame = self._build_rtu_frame(pdu)
		try:
			return await asyncio.to_thread(self._exchange, serial_obj, action, frame)
		except AdapterError:
			raise
		except Exception as exc:  # pragma: no cover - defensive guard
			raise AdapterError(f"MODBUS RTU transaction failed: {exc}") from exc

	def _build_rtu_frame(self, pdu: bytes) -> bytes:
		body = bytes([self._unit_id]) + pdu
		crc = _crc16(body)
		return body + struct.pack("<H", crc)

	def _exchange(self, serial_obj: Any, action: ModbusAction, frame: bytes) -> bytes:
		self._prepare_port(serial_obj)
		written = serial_obj.write(frame)
		if written != len(frame):
			raise AdapterError("Incomplete MODBUS RTU write")
		try:
			if hasattr(serial_obj, "flush"):
				serial_obj.flush()
		except Exception:
			# Non-fatal; continue with response handling
			pass
		return self._read_response(serial_obj, action)

	def _prepare_port(self, serial_obj: Any) -> None:
		try:
			if hasattr(serial_obj, "reset_input_buffer"):
				serial_obj.reset_input_buffer()
			if hasattr(serial_obj, "reset_output_buffer"):
				serial_obj.reset_output_buffer()
		except Exception:
			# Non-fatal buffer reset failures should not abort the transaction.
			pass

	def _read_response(self, serial_obj: Any, action: ModbusAction) -> bytes:
		deadline = time.monotonic() + max(self._timeout, 0.05)
		buffer = bytearray()
		expected_length: int | None = None

		while time.monotonic() < deadline:
			chunk = serial_obj.read(1)
			if not chunk:
				continue

			buffer += chunk

			# Ensure the frame starts with the target unit address.
			if len(buffer) == 1 and buffer[0] != self._unit_id:
				buffer.clear()
				expected_length = None
				continue

			if len(buffer) >= 3 and expected_length is None:
				expected_length = self._expected_frame_length(buffer)

			if expected_length and len(buffer) >= expected_length:
				frame = bytes(buffer[:expected_length])
				crc_expected = _crc16(frame[:-2])
				crc_received = frame[-2] | (frame[-1] << 8)
				if crc_expected != crc_received:
					raise AdapterError("MODBUS RTU CRC mismatch")

				unit = frame[0]
				if unit != self._unit_id:
					# Stray frame for another slave; continue reading.
					buffer.clear()
					expected_length = None
					continue

				function = frame[1]
				if function >= 0x80:
					exception_code = frame[2] if len(frame) > 3 else 0
					raise AdapterError(
						f"MODBUS exception: function=0x{function:02X} code=0x{exception_code:02X}"
					)

				return frame[1:-2]

		raise AdapterError("MODBUS RTU response timeout")

	def _expected_frame_length(self, buffer: bytearray) -> int | None:
		if len(buffer) < 3:
			return None
		function = buffer[1]

		if function in (0x01, 0x02, 0x03, 0x04):
			byte_count = buffer[2]
			return 3 + byte_count + 2  # unit + fc + byte_count + payload + crc

		if function in (0x05, 0x06, 0x0F, 0x10):
			return 8  # unit + fc + address(2) + value/count(2) + crc(2)

		if function >= 0x80:
			return 5  # unit + fc + exception + crc(2)

		return None


__all__ = ["ModbusRtuAdapter"]
