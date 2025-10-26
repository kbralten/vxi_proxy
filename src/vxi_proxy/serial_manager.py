
"""Shared serial port manager used by adapters that multiplex RS-485 buses."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional


_LOG = logging.getLogger(__name__)


class SerialPortManagerError(RuntimeError):
	"""Raised when a serial port cannot be provisioned or used."""


class SerialPortManager:
	"""Coordinate shared access to physical serial ports across adapters.

	The manager keeps a single ``pyserial`` ``Serial`` instance per normalized
	port path. Adapters call :meth:`attach` during ``connect`` to obtain the
	shared manager instance, execute transactions via :meth:`transaction`, and
	finally call :meth:`detach` during ``disconnect``.

	A per-manager ``asyncio.Lock`` serializes transactions so that requests
	from different adapters (potentially representing different MODBUS unit
	IDs on the same RS-485 bus) do not collide on the wire.
	"""

	_registry: Dict[str, "SerialPortManager"] = {}
	_registry_lock: asyncio.Lock = asyncio.Lock()

	def __init__(self, key: str, open_kwargs: Dict[str, Any]) -> None:
		self._key = key
		self._open_kwargs = dict(open_kwargs)
		self._serial: Optional[Any] = None
		self._serial_module: Optional[Any] = None
		self._transaction_lock: asyncio.Lock = asyncio.Lock()
		self._refcount: int = 0

	# ------------------------------------------------------------------
	# Lifecycle management
	# ------------------------------------------------------------------

	@classmethod
	async def attach(cls, port: str, open_kwargs: Dict[str, Any]) -> "SerialPortManager":
		"""Return the manager for *port*, creating it if needed.

		The caller-provided ``open_kwargs`` are validated against any existing
		manager for the same port to ensure consistent serial configuration.
		"""

		key, prepared = cls._prepare_settings(port, open_kwargs)

		async with cls._registry_lock:
			manager = cls._registry.get(key)
			if manager is None:
				manager = cls(key, prepared)
				cls._registry[key] = manager
			else:
				manager._validate_settings(prepared)
			manager._refcount += 1
			_LOG.debug("SerialPortManager.attach: key=%s refcount=%s", key, manager._refcount)
			return manager

	async def detach(self) -> None:
		"""Release a reference to this manager and close when unused."""

		need_close = False

		async with self.__class__._registry_lock:
			if self._refcount > 0:
				self._refcount -= 1
			if self._refcount == 0:
				self.__class__._registry.pop(self._key, None)
				need_close = True
			_LOG.debug("SerialPortManager.detach: key=%s refcount=%s", self._key, self._refcount)

		if need_close:
			await self._close_serial()

	@classmethod
	async def reset(cls) -> None:
		"""Close all managed ports (primarily for test cleanup)."""

		async with cls._registry_lock:
			managers = list(cls._registry.values())
			cls._registry.clear()

		for manager in managers:
			await manager._close_serial()

	# ------------------------------------------------------------------
	# Transaction support
	# ------------------------------------------------------------------

	@asynccontextmanager
	async def transaction(self) -> AsyncIterator[Any]:
		"""Async context manager yielding an opened ``pyserial`` instance."""

		await self._transaction_lock.acquire()
		try:
			serial_obj = await self._ensure_serial()
			yield serial_obj
		finally:
			self._transaction_lock.release()

	async def _ensure_serial(self) -> Any:
		serial_obj = self._serial
		if serial_obj is not None and getattr(serial_obj, "is_open", True):
			return serial_obj

		try:
			serial_obj = await asyncio.to_thread(self._open_serial)
		except Exception as exc:  # pragma: no cover - defensive guard
			raise SerialPortManagerError(f"Failed to open serial port {self._open_kwargs['port']!r}: {exc}") from exc

		self._serial = serial_obj
		return serial_obj

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	@classmethod
	def _prepare_settings(cls, port: str, raw_kwargs: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
		normalized = cls.normalize_port(port)
		prepared = dict(raw_kwargs)
		prepared["port"] = normalized
		prepared.setdefault("timeout", 1.0)
		prepared.setdefault("write_timeout", prepared.get("timeout", 1.0))
		return normalized, prepared

	def _validate_settings(self, new_kwargs: Dict[str, Any]) -> None:
		comparable_keys = (
			"port",
			"baudrate",
			"bytesize",
			"parity",
			"stopbits",
			"timeout",
			"write_timeout",
			"xonxoff",
			"rtscts",
			"dsrdtr",
		)
		for key in comparable_keys:
			existing = self._open_kwargs.get(key)
			incoming = new_kwargs.get(key)
			if existing is None and incoming is None:
				continue
			if existing != incoming:
				raise SerialPortManagerError(
					f"Serial port {self._key!r} already open with different setting {key!r}:"
					f" existing={existing!r} new={incoming!r}"
				)

	def _resolve_serial_module(self) -> Any:
		if self._serial_module is not None:
			return self._serial_module
		try:
			module = importlib.import_module("serial")  # type: ignore
		except Exception as exc:  # pragma: no cover - missing dependency
			raise SerialPortManagerError("pyserial is required for serial adapters") from exc
		self._serial_module = module
		return module

	def _open_serial(self) -> Any:
		module = self._resolve_serial_module()
		SerialCls = getattr(module, "Serial", None)
		if SerialCls is None:
			raise SerialPortManagerError("pyserial module does not expose Serial class")
		serial_obj = SerialCls(**self._open_kwargs)
		# Best effort cleanup of residual buffers before first use.
		try:
			if hasattr(serial_obj, "reset_input_buffer"):
				serial_obj.reset_input_buffer()
			if hasattr(serial_obj, "reset_output_buffer"):
				serial_obj.reset_output_buffer()
		except Exception:  # pragma: no cover - defensive cleanup
			_LOG.debug("Serial buffer reset failed for port %s", self._open_kwargs.get("port"), exc_info=True)
		return serial_obj

	async def _close_serial(self) -> None:
		serial_obj = self._serial
		self._serial = None
		if serial_obj is None:
			return

		def _close() -> None:
			try:
				if hasattr(serial_obj, "close"):
					serial_obj.close()
			except Exception:  # pragma: no cover - defensive cleanup
				_LOG.debug("Serial close failed for port %s", self._open_kwargs.get("port"), exc_info=True)

		await asyncio.to_thread(_close)

	# ------------------------------------------------------------------
	# Utility functions
	# ------------------------------------------------------------------

	@staticmethod
	def normalize_port(port: str) -> str:
		"""Normalize platform-specific serial port names."""

		if "://" in port:
			# URL-style transports (socket://, loop://, etc.) must remain intact
			return port
		if os.name == "nt":
			if port.startswith("\\\\.\\"):
				return port
			return f"\\\\.\\{port}"
		return port


__all__ = ["SerialPortManager", "SerialPortManagerError"]
