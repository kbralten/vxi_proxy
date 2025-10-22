"""Unit tests for adapter lifecycle: open on acquire, close on release."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


class _FakeSerial:
    def __init__(self, *_, **kwargs):
        self.timeout = kwargs.get("timeout", 1.0)
        self.write_timeout = kwargs.get("write_timeout", 1.0)
        self.is_open = True

    def close(self):
        self.is_open = False

    def write(self, data: bytes) -> int: # pragma: no cover - not used here
        return len(data)

    def read(self, size: int) -> bytes: # pragma: no cover - not used here
        return b""


class _BadSerial:
    def __init__(self, *_, **__):
        raise RuntimeError("open failed")


class AdapterLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        # Default to the good fake serial
        fake_serial_mod = types.SimpleNamespace(Serial=_FakeSerial)
        sys.modules["serial"] = fake_serial_mod  # type: ignore

        from vxi_proxy.adapters.scpi_serial import ScpiSerialAdapter  # type: ignore

        self.Adapter = ScpiSerialAdapter

    def tearDown(self) -> None:
        sys.modules.pop("serial", None)

    def test_acquire_opens_and_release_closes(self) -> None:
        adapter = self.Adapter("lifecycle0", port="COM7")
        import asyncio

        loop = asyncio.get_event_loop()
        # Initially no serial object
        self.assertIsNone(getattr(adapter, "_ser", None))

        # Acquire should open the serial port and hold the internal lock
        loop.run_until_complete(adapter.acquire())
        ser = getattr(adapter, "_ser", None)
        self.assertIsNotNone(ser)
        import serial  # type: ignore

        self.assertIsInstance(ser, serial.Serial)
        # Internal lock should be held
        self.assertTrue(adapter._lock.locked())

        # Release should close the port and release the lock
        adapter.release()
        self.assertIsNone(getattr(adapter, "_ser", None))
        self.assertFalse(adapter._lock.locked())

    def test_release_idempotent(self) -> None:
        adapter = self.Adapter("lifecycle1", port="COM8")
        import asyncio

        loop = asyncio.get_event_loop()
        loop.run_until_complete(adapter.acquire())
        # First release
        adapter.release()
        # Second release should not raise and should keep lock released
        adapter.release()
        self.assertFalse(adapter._lock.locked())

    def test_acquire_failure_releases_lock(self) -> None:
        # Ensure the scpi_serial module will attempt to open using a
        # Serial implementation that raises on instantiation.
        from vxi_proxy.adapters import scpi_serial  # type: ignore
        old_serial = getattr(scpi_serial, "serial", None)
        scpi_serial.serial = types.SimpleNamespace(Serial=_BadSerial)
        from vxi_proxy.adapters.base import AdapterError  # type: ignore
        from vxi_proxy.adapters.scpi_serial import ScpiSerialAdapter  # type: ignore

        adapter = ScpiSerialAdapter("lifecycle_bad", port="COM9")
        import asyncio

        loop = asyncio.get_event_loop()
        try:
            with self.assertRaises(AdapterError):
                loop.run_until_complete(adapter.acquire())
            # After failure the internal lock must not be held
            self.assertFalse(adapter._lock.locked())
        finally:
            # Restore the module serial binding so other tests are unaffected
            scpi_serial.serial = old_serial


if __name__ == "__main__":
    unittest.main()
