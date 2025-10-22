"""Internal state tracking for active VXI-11 links."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict

from .adapters.base import DeviceAdapter


@dataclass(slots=True)
class Link:
    """Represents an active VXI-11 link bound to an adapter instance."""

    lid: int
    device_name: str
    adapter: DeviceAdapter
    client_id: int
    has_lock: bool = False


class LinkNotFoundError(RuntimeError):
    """Raised when a requested link does not exist."""


class LinkManager:
    """Allocate and manage link identifiers for the façade."""

    def __init__(self) -> None:
        self._next_lid = 1
        self._links: Dict[int, Link] = {}
        self._lock: asyncio.Lock | None = None

    async def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def create_link(self, device_name: str, adapter: DeviceAdapter, client_id: int) -> Link:
        lock = await self._ensure_lock()
        async with lock:
            lid = self._next_lid
            self._next_lid += 1
            link = Link(
                lid=lid,
                device_name=device_name,
                adapter=adapter,
                client_id=client_id,
                has_lock=False,
            )
            self._links[lid] = link
            return link

    async def destroy_link(self, lid: int) -> None:
        lock = await self._ensure_lock()
        async with lock:
            link = self._links.pop(lid, None)
        if link is None:
            raise LinkNotFoundError(f"Link {lid} does not exist")
        await link.adapter.disconnect()

    async def get(self, lid: int) -> Link:
        lock = await self._ensure_lock()
        async with lock:
            link = self._links.get(lid)
        if link is None:
            raise LinkNotFoundError(f"Link {lid} does not exist")
        return link

    async def find_by_device(self, device_name: str) -> list[Link]:
        lock = await self._ensure_lock()
        async with lock:
            return [link for link in self._links.values() if link.device_name == device_name]

    async def active_links(self) -> Dict[int, Link]:
        lock = await self._ensure_lock()
        async with lock:
            return dict(self._links)
