"""USBTMC (USB Test & Measurement Class) adapter using python-usbtmc.

This adapter provides pass-through of SCPI commands to USB instruments that
implement the USBTMC protocol (USB TMC-488 subset). It uses pyusb/python-usbtmc
for device discovery and communication.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, cast

from .base import AdapterError, DeviceAdapter


try:
    import usbtmc  # type: ignore - python-usbtmc
except Exception:
    usbtmc = cast(Any, None)

_LOG = logging.getLogger(__name__)


def _parse_termination(value: Optional[str]) -> Optional[bytes]:
    if value is None:
        return None
    v = value
    if v == "":
        return None
    if v.upper() == "CRLF":
        return b"\r\n"
    if v.upper() == "CR":
        return b"\r"
    if v.upper() == "LF":
        return b"\n"
    try:
        return v.encode("utf-8").decode("unicode_escape").encode("utf-8")
    except Exception:
        return v.encode("utf-8")


class UsbTmcAdapter(DeviceAdapter):
    """Adapter forwarding SCPI commands to a USBTMC USB device.

    Settings supported (DeviceDefinition.settings):
      - vid (int, required): USB Vendor ID (hex, e.g., 0x0957)
      - pid (int, required): USB Product ID (hex, e.g., 0x1755)
      - serial (str, optional): USB device serial number for disambiguation
      - timeout (float, default 1.0): I/O timeout in seconds
      - write_termination (str, optional): e.g., "\\n" or "CRLF"
      - read_termination (str, optional): e.g., "\\n" or "CRLF"
    """

    def __init__(self, name: str, **settings: Any) -> None:
        super().__init__(name)
        # USBTMC devices require exclusive access
        self.requires_lock = True
        self._settings = settings

        vid = settings.get("vid")
        pid = settings.get("pid")
        if vid is None or pid is None:
            raise AdapterError(f"Device {name!r} missing required 'vid' and 'pid' settings")

        try:
            self._vid = int(vid) if isinstance(vid, int) else int(vid, 16)
        except Exception as exc:
            raise AdapterError(f"vid must be an integer (hex or dec): {exc}") from exc

        try:
            self._pid = int(pid) if isinstance(pid, int) else int(pid, 16)
        except Exception as exc:
            raise AdapterError(f"pid must be an integer (hex or dec): {exc}") from exc

        self._serial = str(settings.get("serial", ""))
        self._timeout = float(settings.get("timeout", 1.0))

        self._write_term = _parse_termination(settings.get("write_termination"))
        self._read_term = _parse_termination(settings.get("read_termination"))

        # Underlying device handle; set when acquire() succeeds
        self._device: Optional[Any] = None

    async def connect(self) -> None:
        """Lightweight validation; actual device open happens in acquire()."""
        if usbtmc is None:
            raise AdapterError("python-usbtmc is required for usbtmc adapter")
        return None

    async def acquire(self) -> None:
        """Open the USBTMC device (blocking operation run in thread)."""
        await super().acquire()
        if self._device is not None:
            return

        if usbtmc is None:
            # Release lock to avoid deadlock
            super().release()
            raise AdapterError("python-usbtmc is required for usbtmc adapter")

        def _open() -> Any:
            # python-usbtmc.Instrument accepts idVendor, idProduct, and optional iSerial
            # Use getattr to avoid static analyzers complaining if the
            # runtime package is not installed in the analysis environment.
            InstrumentCls = getattr(usbtmc, "Instrument", None)
            if InstrumentCls is None:
                raise RuntimeError("python-usbtmc Instrument class not available")
            if self._serial:
                device = InstrumentCls(idVendor=self._vid, idProduct=self._pid, iSerial=self._serial)
            else:
                device = InstrumentCls(idVendor=self._vid, idProduct=self._pid)
            # Set timeout in seconds (python-usbtmc uses milliseconds internally)
            device.timeout = self._timeout
            return device

        try:
            self._device = await asyncio.to_thread(_open)
            _LOG.debug(
                "usbtmc.acquire: opened device VID=0x%04x PID=0x%04x serial=%r",
                self._vid,
                self._pid,
                self._serial or "<any>",
            )
        except Exception as exc:
            # Opening failed; release lock and surface AdapterError
            super().release()
            raise AdapterError(
                f"Failed to open USBTMC device VID=0x{self._vid:04x} PID=0x{self._pid:04x}: {exc}"
            ) from exc

    def release(self) -> None:
        """Close the USBTMC device and release the adapter lock."""
        device = self._device
        self._device = None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        super().release()

    async def disconnect(self) -> None:
        """Fully close the device if open."""
        device = self._device
        self._device = None
        if device is None:
            return

        def _close() -> None:
            try:
                device.close()
            except Exception:
                pass

        await asyncio.to_thread(_close)

    async def write(self, data: bytes) -> int:
        """Send data to the USBTMC device; append termination if configured."""
        device = self._device
        if device is None:
            raise AdapterError("USBTMC device is not connected")

        payload = data
        if self._write_term and not payload.endswith(self._write_term):
            payload += self._write_term

        def _do_write() -> int:
            # python-usbtmc.Instrument.write accepts bytes or str
            device.write_raw(payload)
            _LOG.debug("usbtmc.write: wrote %d bytes: %r", len(payload), payload)
            return len(payload)

        try:
            return await asyncio.to_thread(_do_write)
        except Exception as exc:
            raise AdapterError(f"USBTMC write failed: {exc}") from exc

    async def read(self, request_size: int) -> bytes:
        """Read from the USBTMC device until termination, request_size, or timeout."""
        device = self._device
        if device is None:
            raise AdapterError("USBTMC device is not connected")

        term = self._read_term
        target = max(1, request_size)

        def _do_read() -> bytes:
            buf = bytearray()
            # Read in chunks until we hit the terminator or target size
            chunk_size = min(1024, target)
            while len(buf) < target:
                try:
                    # python-usbtmc read_raw returns bytes
                    chunk = device.read_raw(chunk_size)
                    if not chunk:
                        break  # timeout or no data
                    buf += chunk
                    if term and buf.endswith(term):
                        break
                except Exception:
                    # On timeout or error, return what we have
                    break
            _LOG.debug("usbtmc.read: read %d bytes: %r", len(buf), bytes(buf))
            return bytes(buf)

        try:
            return await asyncio.to_thread(_do_read)
        except Exception as exc:
            raise AdapterError(f"USBTMC read failed: {exc}") from exc
