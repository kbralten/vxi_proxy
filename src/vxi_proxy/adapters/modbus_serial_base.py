"""Common functionality for MODBUS serial adapters (RTU and ASCII)."""

from __future__ import annotations

from abc import ABC, abstractmethod
import logging
import struct
from typing import Any, List, Optional
import re

from .base import AdapterError, DeviceAdapter
from ..mapping_engine import (
	MappingError,
	ModbusAction,
	decode_registers,
	translate_command,
	FC_READ_COILS,
	FC_READ_DISCRETE_INPUTS,
	FC_READ_HOLDING_REGISTERS,
	FC_READ_INPUT_REGISTERS,
	FC_WRITE_SINGLE_COIL,
	FC_WRITE_SINGLE_REGISTER,
	FC_WRITE_MULTIPLE_REGISTERS,
)
from ..serial_manager import SerialPortManager, SerialPortManagerError


_LOG = logging.getLogger(__name__)

_VALID_PARITY = {"N", "E", "O", "M", "S"}
_ALLOWED_STOPBITS = {1, 1.5, 2}


class ModbusSerialAdapterBase(DeviceAdapter, ABC):
	"""Base class shared by MODBUS-RTU and MODBUS-ASCII adapters."""

	PROTOCOL_NAME = "serial"

	def __init__(self, name: str, **settings: Any) -> None:
		super().__init__(name)
		self.requires_lock = False  # Arbitration handled by SerialPortManager

		port = settings.get("port")
		if not port:
			raise AdapterError(f"Device {name!r} missing required 'port' setting for MODBUS {self.PROTOCOL_NAME}")
		self._port = str(port)

		try:
			self._unit_id = int(settings.get("unit_id", 1))
		except Exception as exc:
			raise AdapterError("unit_id must be an integer") from exc

		if not (0 <= self._unit_id <= 247):
			raise AdapterError(f"unit_id {self._unit_id} out of range [0, 247]")

		try:
			self._timeout = float(settings.get("timeout", 1.0))
		except Exception as exc:
			raise AdapterError("timeout must be numeric") from exc

		try:
			baudrate = int(settings.get("baudrate", 9600))
		except Exception as exc:
			raise AdapterError("baudrate must be an integer") from exc

		try:
			bytesize = int(settings.get("bytesize", 8))
		except Exception as exc:
			raise AdapterError("bytesize must be an integer") from exc

		parity = str(settings.get("parity", "N")).upper()
		if parity not in _VALID_PARITY:
			raise AdapterError(f"Invalid parity {parity!r}; expected one of {_VALID_PARITY}")

		try:
			stopbits = float(settings.get("stopbits", 1.0))
		except Exception as exc:
			raise AdapterError("stopbits must be numeric") from exc
		if stopbits not in _ALLOWED_STOPBITS:
			raise AdapterError("stopbits must be one of 1, 1.5, 2")

		self._serial_kwargs = {
			"port": self._port,
			"baudrate": baudrate,
			"bytesize": bytesize,
			"parity": parity,
			"stopbits": stopbits,
			"timeout": self._timeout,
			"write_timeout": self._timeout,
			"xonxoff": bool(settings.get("xonxoff", False)),
			"rtscts": bool(settings.get("rtscts", False)),
			"dsrdtr": bool(settings.get("dsrdtr", False)),
		}

		self._mappings = settings.get("mappings", []) or []
		self._manager: Optional[SerialPortManager] = None
		self._read_buffer: str = ""

	# ------------------------------------------------------------------
	# Lifecycle hooks
	# ------------------------------------------------------------------

	async def connect(self) -> None:
		try:
			self._manager = await SerialPortManager.attach(self._port, self._serial_kwargs)
		except SerialPortManagerError as exc:
			raise AdapterError(str(exc)) from exc

	async def disconnect(self) -> None:
		self._read_buffer = ""
		manager = self._manager
		self._manager = None
		if manager is not None:
			await manager.detach()

	# ------------------------------------------------------------------
	# VXI-11 adapter interface
	# ------------------------------------------------------------------

	async def write(self, data: bytes) -> int:
		if not self._manager:
			raise AdapterError("Serial port is not connected")

		try:
			command = data.decode("ascii").strip()
		except UnicodeDecodeError as exc:
			raise AdapterError("Commands must be ASCII-encoded for MODBUS adapters") from exc

		# Support static-response rules: if a mapping includes a 'response'
		# string and its pattern matches, bypass I/O and return that response.
		for rule in self._mappings:
			try:
				pattern = rule.get("pattern") if isinstance(rule, dict) else None
				if not pattern:
					continue
				regex = re.compile(str(pattern), re.IGNORECASE)
				m = regex.match(command)
				if not m:
					continue
				resp = None
				if isinstance(rule.get("response"), str) and rule.get("response"):
					resp = rule.get("response")
				else:
					params = rule.get("params", {}) if isinstance(rule, dict) else {}
					maybe = params.get("response") if isinstance(params, dict) else None
					if isinstance(maybe, str) and maybe:
						resp = maybe
				if resp is not None:
					def _sub_token(mm: re.Match[str]) -> str:
						key = mm.group(1) or mm.group(2)
						try:
							if key.isdigit():
								val = m.group(int(key))
							else:
								val = m.group(key)
						except Exception:
							val = ""
						return "" if val is None else str(val)

					token_re = re.compile(r"\$(\w+)|\$\{(\w+)\}")
					self._read_buffer = token_re.sub(_sub_token, str(resp))
					return len(data)
			except re.error:
				# Ignore invalid patterns here; mapping engine will handle errors
				pass

		try:
			action = translate_command(command, self._mappings)
		except MappingError as exc:
			raise AdapterError(f"Command mapping failed: {exc}") from exc

		result = await self._execute_action(action)

		if action.function_code in (FC_READ_HOLDING_REGISTERS, FC_READ_INPUT_REGISTERS):
			self._read_buffer = self._format_register_result(result)
		elif action.function_code in (FC_READ_COILS, FC_READ_DISCRETE_INPUTS):
			self._read_buffer = str(result)
		else:
			self._read_buffer = ""

		return len(data)

	async def read(self, request_size: int) -> bytes:
		response = self._read_buffer
		self._read_buffer = ""
		return response.encode("ascii")

	# ------------------------------------------------------------------
	# MODBUS helpers shared by subclasses
	# ------------------------------------------------------------------

	async def _execute_action(self, action: ModbusAction) -> Any:
		manager = self._manager
		if manager is None:
			raise AdapterError("Serial port is not connected")

		pdu = self._build_pdu(action)

		try:
			async with manager.transaction() as serial_obj:
				response_pdu = await self._perform_transaction(serial_obj, action, pdu)
		except SerialPortManagerError as exc:
			raise AdapterError(str(exc)) from exc

		return self._decode_response(action, response_pdu)

	def _build_pdu(self, action: ModbusAction) -> bytes:
		fc = action.function_code

		if fc in (
			FC_READ_COILS,
			FC_READ_DISCRETE_INPUTS,
			FC_READ_HOLDING_REGISTERS,
			FC_READ_INPUT_REGISTERS,
		):
			return struct.pack(">BHH", fc, action.address, action.count)

		if fc in (FC_WRITE_SINGLE_COIL, FC_WRITE_SINGLE_REGISTER):
			if not action.values:
				raise AdapterError("Write action missing values")
			return struct.pack(">BHH", fc, action.address, action.values[0])

		if fc == FC_WRITE_MULTIPLE_REGISTERS:
			if not action.values:
				raise AdapterError("Write multiple registers missing values")
			count = len(action.values)
			byte_count = count * 2
			pdu = struct.pack(">BHHB", fc, action.address, count, byte_count)
			for value in action.values:
				pdu += struct.pack(">H", value)
			return pdu

		raise AdapterError(f"Unsupported MODBUS function code: 0x{fc:02X}")

	def _decode_response(self, action: ModbusAction, response_pdu: bytes) -> Any:
		if not response_pdu:
			raise AdapterError("Empty MODBUS response")

		function = response_pdu[0]
		fc = action.function_code

		if function != fc:
			if function >= 0x80:
				exception_code = response_pdu[1] if len(response_pdu) > 1 else 0
				raise AdapterError(
					f"MODBUS exception: function=0x{function:02X} code=0x{exception_code:02X}"
				)
			raise AdapterError(
				f"Unexpected MODBUS function code in response: expected=0x{fc:02X} got=0x{function:02X}"
			)

		if fc in (FC_READ_HOLDING_REGISTERS, FC_READ_INPUT_REGISTERS):
			if len(response_pdu) < 2:
				raise AdapterError("MODBUS response missing byte count")
			byte_count = response_pdu[1]
			data = response_pdu[2 : 2 + byte_count]
			if len(data) != byte_count:
				raise AdapterError("Incomplete MODBUS register payload")
			if byte_count % 2 != 0:
				raise AdapterError("Register payload length must be even")
			registers: List[int] = []
			for offset in range(0, byte_count, 2):
				registers.append(struct.unpack(">H", data[offset : offset + 2])[0])
			return decode_registers(registers, action.data_type)

		if fc in (FC_READ_COILS, FC_READ_DISCRETE_INPUTS):
			if len(response_pdu) < 2:
				raise AdapterError("MODBUS response missing coil byte count")
			byte_count = response_pdu[1]
			payload = response_pdu[2 : 2 + byte_count]
			if len(payload) != byte_count:
				raise AdapterError("Incomplete MODBUS coil payload")
			bits: List[str] = []
			for idx in range(action.count):
				byte_index = idx // 8
				bit_index = idx % 8
				bit_val = 0
				if byte_index < len(payload):
					bit_val = 1 if (payload[byte_index] >> bit_index) & 0x01 else 0
				bits.append("1" if bit_val else "0")
			return "".join(bits)

		if fc in (
			FC_WRITE_SINGLE_COIL,
			FC_WRITE_SINGLE_REGISTER,
			FC_WRITE_MULTIPLE_REGISTERS,
		):
			return "OK"

		return "OK"

	def _format_register_result(self, value: Any) -> str:
		if isinstance(value, bool):
			return "1" if value else "0"
		if isinstance(value, float):
			return f"{value:.6f}"
		return str(value)

	# ------------------------------------------------------------------
	# Subclass contract
	# ------------------------------------------------------------------

	@abstractmethod
	async def _perform_transaction(self, serial_obj: Any, action: ModbusAction, pdu: bytes) -> bytes:
		"""Send the MODBUS request *pdu* and return the response PDU."""


__all__ = ["ModbusSerialAdapterBase"]
