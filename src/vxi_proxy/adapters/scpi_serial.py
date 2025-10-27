"""SCPI over serial (RS-232/USB-serial) adapter using pyserial.

This adapter provides simple pass-through of ASCII SCPI commands over a
serial port with configurable line terminations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast
import os
import importlib

from .base import AdapterError, DeviceAdapter


try:  # Import lazily to allow tests to patch without pyserial initially
    import serial  # type: ignore
except Exception as _exc:  # pragma: no cover - exercised only if pyserial missing
    serial = cast(Any, None)


_LOG = logging.getLogger(__name__)


_PARITY_MAP = {
    "N": "N",
    "E": "E",
    "O": "O",
    "M": "M",
    "S": "S",
}


def _parse_termination(value: Optional[str]) -> Optional[bytes]:
    if value is None:
        return None
    v = value
    if v == "":
        return None
    # Common shorthands
    if v.upper() == "CRLF":
        return b"\r\n"
    if v.upper() == "CR":
        return b"\r"
    if v.upper() == "LF":
        return b"\n"
    # Support typical escape sequences like "\n", "\r", "\t"
    try:
        return v.encode("utf-8").decode("unicode_escape").encode("utf-8")
    except Exception:
        return v.encode("utf-8")


class ScpiSerialAdapter(DeviceAdapter):
    """Adapter that forwards SCPI commands over a serial port.

    Settings (DeviceDefinition.settings):
      - port (str, required): e.g. "COM3" or "/dev/ttyUSB0"
      - baudrate (int, default 9600)
      - bytesize (int, one of 5,6,7,8; default 8)
      - parity (str, one of N,E,O,M,S; default "N")
      - stopbits (int|float, one of 1, 1.5, 2; default 1)
      - timeout (float seconds, default 1.0)
      - write_termination (str, e.g. "\n" or "CRLF"; optional)
      - read_termination (str, e.g. "\n" or "CRLF"; optional)
            - inter_byte_timeout (float seconds, optional): when no read_termination
                is configured, this helps return promptly after a short gap between
                bytes rather than waiting for the full device timeout.
    """

    def __init__(self, name: str, **settings: object) -> None:
        super().__init__(name)
        self.requires_lock = True
        self._settings = settings
        self._ser: Optional[Any] = None

        # Extract settings with defaults
        self._port = str(settings.get("port")) if settings.get("port") is not None else None
        if not self._port:
            raise AdapterError(f"Device {name!r} missing required 'port' setting")
        # On Windows, com0com may create named ports like CNCA0/CNCB0 which
        # must be opened via the Windows device path prefix (\\\\.\\) when
        # using pyserial. Normalize common cases so tests and configs may use
        # the simple port name.
        if os.name == "nt" and not self._port.startswith("\\\\.\\"):
            self._port = "\\\\.\\" + self._port

        def _as_int(key: str, default: int) -> int:
            val = settings.get(key, default)
            try:
                return int(cast(Any, val))
            except Exception as exc:
                raise AdapterError(f"{key} must be an integer") from exc

        def _as_float(key: str, default: float) -> float:
            val = settings.get(key, default)
            try:
                return float(cast(Any, val))
            except Exception as exc:
                raise AdapterError(f"{key} must be a number") from exc

        self._baudrate = _as_int("baudrate", 9600)
        self._bytesize = _as_int("bytesize", 8)
        self._parity = str(settings.get("parity", "N")).upper()
        if self._parity not in _PARITY_MAP:
            raise AdapterError(f"Invalid parity {self._parity!r}; expected one of N,E,O,M,S")
        stopbits_val: float = _as_float("stopbits", 1.0)
        if stopbits_val not in (1, 1.5, 2):
            raise AdapterError("stopbits must be 1, 1.5, or 2")
        self._stopbits: float = stopbits_val

        self._timeout = _as_float("timeout", 1.0)
        wt = settings.get("write_termination")
        rt = settings.get("read_termination")
        self._write_term = _parse_termination(cast(Optional[str], wt) if isinstance(wt, str) else None)
        self._read_term = _parse_termination(cast(Optional[str], rt) if isinstance(rt, str) else None)
        # Optional inter-byte timeout to avoid long waits when devices do not
        # emit a terminator or send very short bursts. If unspecified, leave
        # it as None so pyserial defaults apply. Users can set a small value
        # like 0.02 for very snappy reads.
        # Default to a small inter-byte timeout for low-latency reads; users can
        # override by specifying inter_byte_timeout explicitly.
        ibt_raw = settings.get("inter_byte_timeout", 0.02)
        try:
            self._inter_byte_timeout = float(cast(Any, ibt_raw)) if ibt_raw is not None else None
        except Exception as exc:
            raise AdapterError("inter_byte_timeout must be a number") from exc

    async def connect(self) -> None:
        # Allow eager open for convenience (tests and some workflows expect
        # connect() to open the underlying serial device). If the port is
        # already open, do nothing.
        if self._ser is not None:
            return None

        # Choose the best 'serial' provider at call time. Tests may either
        # patch sys.modules['serial'] with a fake that implements helpers
        # like push_rx/pop_tx, or they may directly set the module-level
        # scpi_serial.serial attribute to force a specific behavior. Prefer
        # the sys.modules provider when it exposes the testing helpers,
        # otherwise honor the explicit module-level binding if present.
        global serial
        serial_mod = None
        try:
            serial_mod = importlib.import_module("serial")  # type: ignore
        except Exception:
            serial_mod = None

        # If the sys.modules provider looks like the richer test fake,
        # prefer it.
        if serial_mod is not None:
            SerialCls = getattr(serial_mod, "Serial", None)
            if SerialCls is not None and hasattr(SerialCls, "push_rx"):
                serial = serial_mod
        # Otherwise, if a module-level serial has already been set (for
        # example tests that directly assign scpi_serial.serial), prefer
        # that.
        if serial is None and serial_mod is not None:
            # If nothing selected yet, fall back to the sys.modules provider.
            serial = serial_mod
        if serial is None:
            raise AdapterError("pyserial is required for scpi-serial adapter")

        def _open() -> Any:
            return serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._bytesize,
                parity=self._parity,
                stopbits=self._stopbits,
                timeout=self._timeout,
                write_timeout=self._timeout,
                inter_byte_timeout=self._inter_byte_timeout,
                xonxoff=bool(self._settings.get("xonxoff", False)),
                rtscts=bool(self._settings.get("rtscts", False)),
                dsrdtr=bool(self._settings.get("dsrdtr", False)),
            )

        try:
            self._ser = await asyncio.to_thread(_open)
        except Exception as exc:
            raise AdapterError(f"Failed to open serial port {self._port}: {exc}") from exc

    async def acquire(self) -> None:
        """Acquire internal adapter lock and open serial port if needed."""
        # Acquire adapter-level mutex first
        await super().acquire()
        if self._ser is not None:
            return
        # Choose the best 'serial' provider at call time. See comment in
        # connect() for the rationale and precedence.
        global serial
        serial_mod = None
        try:
            serial_mod = importlib.import_module("serial")  # type: ignore
        except Exception:
            serial_mod = None

        if serial_mod is not None:
            SerialCls = getattr(serial_mod, "Serial", None)
            if SerialCls is not None and hasattr(SerialCls, "push_rx"):
                serial = serial_mod

        if serial is None and serial_mod is not None:
            serial = serial_mod

        if serial is None:
            # Release the internal lock to avoid deadlock
            super().release()
            raise AdapterError("pyserial is required for scpi-serial adapter")

        def _open() -> Any:
            return serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=self._bytesize,
                parity=self._parity,
                stopbits=self._stopbits,
                timeout=self._timeout,
                write_timeout=self._timeout,
                inter_byte_timeout=self._inter_byte_timeout,
                xonxoff=bool(self._settings.get("xonxoff", False)),
                rtscts=bool(self._settings.get("rtscts", False)),
                dsrdtr=bool(self._settings.get("dsrdtr", False)),
            )

        try:
            self._ser = await asyncio.to_thread(_open)
        except Exception as exc:
            # Opening failed; release internal lock and surface AdapterError
            super().release()
            raise AdapterError(f"Failed to open serial port {self._port}: {exc}") from exc

    async def disconnect(self) -> None:
        ser = self._ser
        self._ser = None
        if ser is None:
            return

        def _close() -> None:
            try:
                if getattr(ser, "is_open", True):
                    ser.close()
            except Exception:
                pass

        await asyncio.to_thread(_close)

    def release(self) -> None:
        """Release adapter internal lock and close serial port if open."""
        ser = self._ser
        self._ser = None
        if ser is not None:
            try:
                # Close synchronously; keep it simple and fast.
                ser.close()
            except Exception:
                pass
        # Finally release internal mutex
        super().release()

    async def write(self, data: bytes) -> int:
        ser = self._ser
        if ser is None:
            raise AdapterError("Serial port is not connected")

        payload = data
        if self._write_term and not payload.endswith(self._write_term):
            payload += self._write_term

        def _do_write() -> int:
            # Write and flush to ensure data is pushed to the driver/device
            written = int(ser.write(payload))
            try:
                # Some pyserial backends buffer writes; flush to force transmit.
                if hasattr(ser, "flush"):
                    ser.flush()
            except Exception:
                # Non-fatal; log at debug level
                _LOG.debug("flush() failed on serial port %s", getattr(ser, "port", "<unknown>"), exc_info=True)
            _LOG.debug("scpi_serial.write: port=%s bytes=%s payload=%r", getattr(ser, "port", "<unknown>"), written, payload)
            return written

        return await asyncio.to_thread(_do_write)

    async def read(self, request_size: int) -> bytes:
        ser = self._ser
        if ser is None:
            raise AdapterError("Serial port is not connected")

        term = self._read_term
        # To avoid waiting for a large request_size, prefer a modest chunking
        # strategy and finish as soon as the terminator is observed. If no
        # terminator is configured, return whatever we have when the port's
        # timeout or inter-byte timeout elapses.
        target = max(1, min(65536, request_size or 65536))

        def _do_read() -> bytes:
            buf = bytearray()
            while len(buf) < target:
                # Read in small chunks to allow terminator detection
                # Read strictly 1 byte at a time to minimize latency; rely on
                # inter_byte_timeout to terminate quickly when data stops.
                chunk = ser.read(1)
                if not chunk:
                    # timeout or no data available; if we have any bytes, return them
                    break
                buf += chunk
                if term and buf.endswith(term):
                    break
            _LOG.debug("scpi_serial.read: port=%s got=%r", getattr(ser, "port", "<unknown>"), bytes(buf))
            return bytes(buf)

        return await asyncio.to_thread(_do_read)
