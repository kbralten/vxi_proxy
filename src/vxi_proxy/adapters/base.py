"""Base adapter abstractions for backend instrument connectivity."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Optional


class AdapterError(RuntimeError):
    """General adapter failure."""


class DeviceAdapter(ABC):
    """Common interface implemented by all backend adapters."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = asyncio.Lock()
        self.requires_lock = False

    async def connect(self) -> None:
        """Establish connectivity to the physical device."""

    async def disconnect(self) -> None:
        """Cleanly tear down connectivity to the physical device."""

    @abstractmethod
    async def write(self, data: bytes) -> int:
        """Send *data* to the device and return number of bytes accepted."""

    @abstractmethod
    async def read(self, request_size: int) -> bytes:
        """Read up to *request_size* bytes from the device."""

    async def acquire(self) -> None:
        """Acquire the adapter's internal lock."""

        await self._lock.acquire()

    def release(self) -> None:
        """Release the adapter's internal lock."""

        if self._lock.locked():
            self._lock.release()

    async def reset(self) -> None:
        """Optional hook to revert device state after errors."""

    async def trigger(self) -> None:
        """Optional hook for SCPI-style trigger support."""

    async def read_stb(self) -> Optional[int]:
        """Optional hook returning Status Byte for SRQ support."""

        return None
