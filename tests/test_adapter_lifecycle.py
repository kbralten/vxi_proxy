"""Unit tests for adapter lifecycle: open on acquire, close on release."""

from __future__ import annotations

import asyncio
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
        self._fake_serial_mod = types.SimpleNamespace(Serial=_FakeSerial)
        self._original_sys_serial = sys.modules.get("serial")
        sys.modules["serial"] = self._fake_serial_mod  # type: ignore

        from vxi_proxy.adapters import scpi_serial  # type: ignore

        self._original_adapter_serial = getattr(scpi_serial, "serial", None)
        scpi_serial.serial = self._fake_serial_mod  # type: ignore

        from vxi_proxy.adapters.scpi_serial import ScpiSerialAdapter  # type: ignore

        self.Adapter = ScpiSerialAdapter
        # Create a dedicated event loop for each test to work across Python versions
        try:
            self._previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            self._previous_loop = None
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

    def tearDown(self) -> None:
        if self._original_adapter_serial is not None:
            from vxi_proxy.adapters import scpi_serial  # type: ignore

            scpi_serial.serial = self._original_adapter_serial  # type: ignore
        else:
            from vxi_proxy.adapters import scpi_serial  # type: ignore

            scpi_serial.serial = None  # type: ignore

        if self._original_sys_serial is not None:
            sys.modules["serial"] = self._original_sys_serial
        else:
            sys.modules.pop("serial", None)
        try:
            self._loop.close()
        finally:
            if self._previous_loop is not None:
                asyncio.set_event_loop(self._previous_loop)
            else:
                asyncio.set_event_loop(None)

    def test_acquire_opens_and_release_closes(self) -> None:
        adapter = self.Adapter("lifecycle0", port="COM7")
        loop = self._loop
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
        loop = self._loop
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
        loop = self._loop
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
