"""Unit tests for the USBTMC adapter using a fake python-usbtmc module."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any, Deque
from collections import deque

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class _FakeInstrument:
    """Fake python-usbtmc.Instrument for testing."""

    def __init__(self, idVendor: int, idProduct: int, iSerial: str = ""):
        self.idVendor = idVendor
        self.idProduct = idProduct
        self.iSerial = iSerial
        self.timeout = 1.0
        self.is_open = True
        self._rx: Deque[bytes] = deque()
        self._tx: Deque[bytes] = deque()

    def close(self):
        self.is_open = False

    def write_raw(self, data: bytes) -> None:
        self._tx.append(bytes(data))

    def read_raw(self, size: int) -> bytes:
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


class UsbTmcAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        # Patch the 'usbtmc' module with our fake
        fake_usbtmc_mod = types.SimpleNamespace(Instrument=_FakeInstrument)
        sys.modules["usbtmc"] = fake_usbtmc_mod  # type: ignore

        from vxi_proxy.adapters.usbtmc import UsbTmcAdapter  # type: ignore

        self.Adapter = UsbTmcAdapter

    def tearDown(self) -> None:
        # Clean up the usbtmc patch
        sys.modules.pop("usbtmc", None)

    def test_missing_vid_pid_raises_error(self) -> None:
        from vxi_proxy.adapters.base import AdapterError

        with self.assertRaises(AdapterError):
            self.Adapter("test0")

    def test_requires_lock_and_connect_disconnect(self) -> None:
        adapter = self.Adapter("usbtmc0", vid=0x1234, pid=0x5678)
        self.assertTrue(adapter.requires_lock)

        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # connect() is lightweight
            loop.run_until_complete(adapter.connect())
            self.assertIsNone(getattr(adapter, "_device", None))

            # acquire() opens the device
            loop.run_until_complete(adapter.acquire())
            self.assertIsNotNone(getattr(adapter, "_device", None))

            # release() closes it
            adapter.release()
            self.assertIsNone(getattr(adapter, "_device", None))
        finally:
            loop.close()

    def test_write_appends_termination_and_read_until_termination(self) -> None:
        adapter = self.Adapter(
            "usbtmc1",
            vid=0x1234,
            pid=0x5678,
            write_termination="\n",
            read_termination="\n",
        )
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(adapter.acquire())

            device = getattr(adapter, "_device")
            assert device is not None
            device.push_rx(b"PONG\n")

            # Write without trailing newline; adapter should append one
            n = loop.run_until_complete(adapter.write(b"PING"))
            self.assertEqual(n, len(b"PING\n"))
            self.assertEqual(device.pop_tx(), b"PING\n")

            # Read should stop at the newline terminator
            data = loop.run_until_complete(adapter.read(1024))
            self.assertEqual(data, b"PONG\n")

            adapter.release()
        finally:
            loop.close()

    def test_write_does_not_duplicate_termination(self) -> None:
        adapter = self.Adapter("usbtmc2", vid=0x1234, pid=0x5678, write_termination="CRLF")
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(adapter.acquire())
            device = getattr(adapter, "_device")
            assert device is not None

            payload = b"*IDN?\r\n"
            n = loop.run_until_complete(adapter.write(payload))
            self.assertEqual(n, len(payload))
            self.assertEqual(device.pop_tx(), payload)
            adapter.release()
        finally:
            loop.close()

    def test_read_partial_on_timeout(self) -> None:
        adapter = self.Adapter("usbtmc3", vid=0x1234, pid=0x5678, read_termination="\n")
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(adapter.acquire())
            device = getattr(adapter, "_device")
            assert device is not None

            # Push partial data without termination
            device.push_rx(b"PARTIAL")

            # Read should return partial data
            data = loop.run_until_complete(adapter.read(1024))
            self.assertEqual(data, b"PARTIAL")
            adapter.release()
        finally:
            loop.close()


if __name__ == "__main__":
    unittest.main()
