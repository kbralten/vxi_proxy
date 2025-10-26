"""MODBUS-ASCII adapter implementation."""

from __future__ import annotations

import asyncio
import binascii
import time
from typing import Any

from .base import AdapterError
from .modbus_serial_base import ModbusSerialAdapterBase
from ..mapping_engine import ModbusAction


def _lrc(data: bytes) -> int:
	"""Compute the longitudinal redundancy check for MODBUS ASCII."""

	total = sum(data) & 0xFF
	return ((-total) & 0xFF)


class ModbusAsciiAdapter(ModbusSerialAdapterBase):
	"""Adapter for MODBUS devices reachable over ASCII serial links."""

	PROTOCOL_NAME = "ASCII"

	async def _perform_transaction(self, serial_obj: Any, action: ModbusAction, pdu: bytes) -> bytes:
		frame = self._build_ascii_frame(pdu)
		try:
			return await asyncio.to_thread(self._exchange, serial_obj, action, frame)
		except AdapterError:
			raise
		except Exception as exc:  # pragma: no cover - defensive guard
			raise AdapterError(f"MODBUS ASCII transaction failed: {exc}") from exc

	def _build_ascii_frame(self, pdu: bytes) -> bytes:
		payload = bytes([self._unit_id]) + pdu
		checksum = _lrc(payload)
		full = payload + bytes([checksum])
		return b":" + binascii.hexlify(full).upper() + b"\r\n"

	def _exchange(self, serial_obj: Any, action: ModbusAction, frame: bytes) -> bytes:
		self._prepare_port(serial_obj)
		written = serial_obj.write(frame)
		if written != len(frame):
			raise AdapterError("Incomplete MODBUS ASCII write")
		try:
			if hasattr(serial_obj, "flush"):
				serial_obj.flush()
		except Exception:
			pass
		return self._read_response(serial_obj, action)

	def _prepare_port(self, serial_obj: Any) -> None:
		try:
			if hasattr(serial_obj, "reset_input_buffer"):
				serial_obj.reset_input_buffer()
			if hasattr(serial_obj, "reset_output_buffer"):
				serial_obj.reset_output_buffer()
		except Exception:
			pass

	def _read_response(self, serial_obj: Any, action: ModbusAction) -> bytes:
		deadline = time.monotonic() + max(self._timeout, 0.05)
		line = bytearray()
		started = False

		while time.monotonic() < deadline:
			chunk = serial_obj.read(1)
			if not chunk:
				continue

			if not started:
				if chunk == b":":
					line.clear()
					line.extend(chunk)
					started = True
				continue

			line += chunk

			if chunk == b"\n":
				parsed = self._parse_frame(bytes(line))
				if parsed is None:
					started = False
					line.clear()
					continue
				return parsed

		raise AdapterError("MODBUS ASCII response timeout")

	def _parse_frame(self, frame: bytes) -> bytes | None:
		if not frame.startswith(b":") or not frame.endswith(b"\r\n"):
			raise AdapterError("Invalid MODBUS ASCII frame delimiters")

		hex_payload = frame[1:-2]
		if len(hex_payload) < 4 or len(hex_payload) % 2 != 0:
			raise AdapterError("Invalid MODBUS ASCII payload length")

		try:
			data = binascii.unhexlify(hex_payload)
		except binascii.Error as exc:
			raise AdapterError(f"Invalid MODBUS ASCII hex payload: {exc}") from exc

		if len(data) < 3:
			raise AdapterError("MODBUS ASCII payload too short")

		unit = data[0]
		if unit != self._unit_id:
			return None

		function = data[1]
		checksum = data[-1]
		payload = data[:-1]

		expected = _lrc(payload)
		if checksum != expected:
			raise AdapterError("MODBUS ASCII LRC mismatch")

		if function >= 0x80:
			exception_code = data[2] if len(data) > 2 else 0
			raise AdapterError(
				f"MODBUS exception: function=0x{function:02X} code=0x{exception_code:02X}"
			)

		return data[1:-1]


__all__ = ["ModbusAsciiAdapter"]
