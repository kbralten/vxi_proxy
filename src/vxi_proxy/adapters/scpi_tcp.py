"""SCPI over TCP adapter.

Implements the DeviceAdapter contract for plain TCP sockets. Blocking socket
operations are executed in a thread via asyncio.to_thread to match the
existing scpi_serial adapter pattern.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any, Optional

from .base import AdapterError, DeviceAdapter


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


class ScpiTcpAdapter(DeviceAdapter):
    """Adapter forwarding SCPI commands over a TCP socket.

    Settings supported (DeviceDefinition.settings):
      - host (str, required)
      - port (int, required)
      - connect_timeout (float, default 1.0)
      - io_timeout (float, default 1.0)
      - write_termination (str, optional)
      - read_termination (str, optional)
      - tcp_nodelay (bool, default True)
      - keepalive (bool, default False)
      - requires_lock (bool, default False)
      - reconnect_on_error (bool, default False)
    """

    def __init__(self, name: str, **settings: Any) -> None:
        super().__init__(name)
        # Default: allow concurrent clients for TCP devices
        self.requires_lock = bool(settings.get("requires_lock", False))
        self._settings = settings

        host = settings.get("host")
        port = settings.get("port")
        if not host or port is None:
            raise AdapterError("scpi-tcp requires 'host' and 'port' settings")
        self._host = str(host)
        try:
            self._port = int(port)
        except Exception as exc:
            raise AdapterError("port must be an integer") from exc

        self._connect_timeout = float(settings.get("connect_timeout", 1.0))
        self._io_timeout = float(settings.get("io_timeout", 1.0))
        self._tcp_nodelay = bool(settings.get("tcp_nodelay", True))
        self._keepalive = bool(settings.get("keepalive", False))
        self._reconnect_on_error = bool(settings.get("reconnect_on_error", False))

        self._write_term = _parse_termination(settings.get("write_termination"))
        self._read_term = _parse_termination(settings.get("read_termination"))

        # Underlying socket; set when acquire() succeeds
        self._sock: Optional[socket.socket] = None

    async def connect(self) -> None:
        # Eager connect not performed by default; acquire() opens socket.
        return None

    async def acquire(self) -> None:
        await super().acquire()
        if self._sock is not None:
            return

        def _open() -> socket.socket:
            s = socket.create_connection((self._host, self._port), timeout=self._connect_timeout)
            # set timeouts for subsequent IO
            s.settimeout(self._io_timeout)
            if self._tcp_nodelay:
                try:
                    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
            if self._keepalive:
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass
            return s

        try:
            self._sock = await asyncio.to_thread(_open)
        except Exception as exc:
            super().release()
            raise AdapterError(f"Failed to connect to {self._host}:{self._port}: {exc}") from exc

    async def disconnect(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is None:
            return

        def _close() -> None:
            try:
                sock.close()
            except Exception:
                pass

        await asyncio.to_thread(_close)

    def release(self) -> None:
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        super().release()

    async def write(self, data: bytes) -> int:
        # Ensure socket is connected for non-locked (requires_lock=False) use
        if self._sock is None:
            # Try to open the socket lazily
            try:
                await self.acquire()
            except AdapterError:
                raise
        sock = self._sock
        if sock is None:
            raise AdapterError("TCP socket is not connected")

        payload = data
        if self._write_term and not payload.endswith(self._write_term):
            payload += self._write_term

        def _do_write() -> int:
            try:
                sock.sendall(payload)
                _LOG.debug("scpi_tcp.write: host=%s port=%s bytes=%s payload=%r", self._host, self._port, len(payload), payload)
                return len(payload)
            except Exception as exc:
                # Close underlying socket on errors
                try:
                    sock.close()
                except Exception:
                    pass
                raise

        try:
            return await asyncio.to_thread(_do_write)
        except Exception as exc:
            raise AdapterError(f"Write failed for {self._host}:{self._port}: {exc}") from exc

    async def read(self, request_size: int) -> bytes:
        # Ensure socket is connected for non-locked (requires_lock=False) use
        if self._sock is None:
            try:
                await self.acquire()
            except AdapterError:
                raise
        sock = self._sock
        if sock is None:
            raise AdapterError("TCP socket is not connected")

        term = self._read_term
        target = max(1, request_size)

        def _do_read() -> bytes:
            buf = bytearray()
            try:
                while len(buf) < target:
                    # recv will block up to socket timeout
                    chunk = sock.recv(max(1, min(4096, target - len(buf))))
                    if not chunk:
                        break
                    buf += chunk
                    if term and buf.endswith(term):
                        break
            except socket.timeout:
                # timed out; return whatever we have
                pass
            except Exception:
                # On any other socket error, close socket and re-raise
                try:
                    sock.close()
                except Exception:
                    pass
                raise
            _LOG.debug("scpi_tcp.read: host=%s port=%s got=%r", self._host, self._port, bytes(buf))
            return bytes(buf)

        try:
            return await asyncio.to_thread(_do_read)
        except Exception as exc:
            raise AdapterError(f"Read failed for {self._host}:{self._port}: {exc}") from exc
