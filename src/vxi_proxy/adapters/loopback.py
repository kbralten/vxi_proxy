"""Simple loopback adapter for integration testing of the façade."""

from __future__ import annotations

import asyncio
from typing import Deque
from collections import deque

from .base import DeviceAdapter


class LoopbackAdapter(DeviceAdapter):
    """Adapter that echoes all writes back to the reader."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._buffer: Deque[bytes] = deque()
        self._data_ready: asyncio.Event | None = None
        self.requires_lock = True

    def _event(self) -> asyncio.Event:
        if self._data_ready is None:
            self._data_ready = asyncio.Event()
        return self._data_ready

    async def write(self, data: bytes) -> int:
        self._buffer.append(data)
        self._event().set()
        return len(data)

    async def read(self, request_size: int) -> bytes:
        event = self._event()
        await event.wait()
        chunks: list[bytes] = []
        remaining = request_size
        while self._buffer and remaining > 0:
            payload = self._buffer.popleft()
            if len(payload) <= remaining:
                chunks.append(payload)
                remaining -= len(payload)
            else:
                chunks.append(payload[:remaining])
                self._buffer.appendleft(payload[remaining:])
                remaining = 0
        if not self._buffer:
            event.clear()
        return b"".join(chunks)
