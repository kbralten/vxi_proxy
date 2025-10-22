"""Tests for the SCPI-Serial adapter using a fake pyserial Serial stub."""

from __future__ import annotations

import sys
import types
import unittest
from collections import deque
from pathlib import Path
from typing import Deque

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class _FakeSerial:
    def __init__(self, *_, **kwargs):
        self.timeout = kwargs.get("timeout", 1.0)
        self.write_timeout = kwargs.get("write_timeout", 1.0)
        self.is_open = True
        self._rx: Deque[bytes] = deque()
        self._tx: Deque[bytes] = deque()

    def close(self):
        self.is_open = False

    def write(self, data: bytes) -> int:
        self._tx.append(bytes(data))
        return len(data)

    def read(self, size: int) -> bytes:
        if not self._rx:
            return b""
        buf = bytearray()
        remaining = size
        while self._rx and remaining > 0:
            chunk = self._rx.popleft()
            if len(chunk) <= remaining:
                buf.extend(chunk)
                remaining -= len(chunk)
            else:
                buf.extend(chunk[:remaining])
                self._rx.appendleft(chunk[remaining:])
                remaining = 0
        return bytes(buf)

    # Test helpers
    def push_rx(self, data: bytes) -> None:
        self._rx.append(data)

    def pop_tx(self) -> bytes:
        return self._tx.popleft()


class ScpiSerialAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the 'serial' module with our fake
        fake_serial_mod = types.SimpleNamespace(Serial=_FakeSerial)
        sys.modules["serial"] = fake_serial_mod  # type: ignore

        from vxi_proxy.adapters.scpi_serial import ScpiSerialAdapter  # type: ignore

        self.Adapter = ScpiSerialAdapter

    def tearDown(self) -> None:
        # Clean up the serial patch
        sys.modules.pop("serial", None)

    def test_requires_lock_and_connect_disconnect(self) -> None:
        adapter = self.Adapter("scpi0", port="COM3")
        self.assertTrue(adapter.requires_lock)
        # Connect opens the FakeSerial
        import serial  # type: ignore

        self.assertIsNone(getattr(adapter, "_ser", None))
        # connect
        import asyncio

        asyncio.get_event_loop().run_until_complete(adapter.connect())
        self.assertIsInstance(getattr(adapter, "_ser", None), serial.Serial)
        # disconnect
        asyncio.get_event_loop().run_until_complete(adapter.disconnect())
        self.assertIsNone(getattr(adapter, "_ser", None))

    def test_write_appends_termination_and_read_until_termination(self) -> None:
        adapter = self.Adapter(
            "scpi1",
            port="COM4",
            baudrate=19200,
            write_termination="\n",
            read_termination="\n",
        )
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(adapter.connect())

        # Prime RX buffer with a response terminated by \n
        ser = getattr(adapter, "_ser")
        assert ser is not None
        ser.push_rx(b"PONG\n")

        # Write without trailing newline; adapter should append one
        n = loop.run_until_complete(adapter.write(b"PING"))
        self.assertEqual(n, len(b"PING\n"))
        self.assertEqual(ser.pop_tx(), b"PING\n")

        # Read should stop at the newline terminator
        data = loop.run_until_complete(adapter.read(1024))
        self.assertEqual(data, b"PONG\n")

        loop.run_until_complete(adapter.disconnect())

    def test_write_does_not_duplicate_termination(self) -> None:
        adapter = self.Adapter("scpi2", port="COM5", write_termination="CRLF")
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(adapter.connect())
        ser = getattr(adapter, "_ser")
        assert ser is not None
        payload = b"*IDN?\r\n"
        n = loop.run_until_complete(adapter.write(payload))
        self.assertEqual(n, len(payload))
        self.assertEqual(ser.pop_tx(), payload)
        loop.run_until_complete(adapter.disconnect())


if __name__ == "__main__":
    unittest.main()
