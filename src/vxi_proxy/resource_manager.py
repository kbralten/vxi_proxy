"""Concurrency primitives for managing exclusive access to backend devices."""

from __future__ import annotations

import asyncio
from typing import Dict, Optional


class DeviceLockedError(RuntimeError):
    """Raised when a device cannot be locked within the requested timeout."""


class DeviceLockOwnershipError(RuntimeError):
    """Raised when a link attempts to release a lock it does not own."""


class ResourceManager:
    """Manage exclusive access to shared backend resources."""

    def __init__(self) -> None:
        self._locks: Dict[str, asyncio.Lock] = {}
        self._owners: Dict[str, Optional[int]] = {}
        self._guard: asyncio.Lock | None = None

    async def _ensure_guard(self) -> asyncio.Lock:
        if self._guard is None:
            self._guard = asyncio.Lock()
        return self._guard

    async def _get_or_create(self, device_id: str) -> asyncio.Lock:
        guard = await self._ensure_guard()
        async with guard:
            existing = self._locks.get(device_id)
            if existing is None:
                existing = asyncio.Lock()
                self._locks[device_id] = existing
                self._owners[device_id] = None
            return existing

    async def lock(self, device_id: str, owner_id: int, timeout: float | None = None) -> None:
        """Acquire an exclusive lock for *device_id* on behalf of *owner_id*."""

        device_lock = await self._get_or_create(device_id)

        guard = await self._ensure_guard()

        async with guard:
            current_owner = self._owners.get(device_id)
            if current_owner == owner_id:
                # Re-entrant acquisition
                return

        try:
            if timeout is None:
                await device_lock.acquire()
            else:
                await asyncio.wait_for(device_lock.acquire(), timeout)
        except asyncio.TimeoutError as exc:
            raise DeviceLockedError(f"Timed out while locking device {device_id!r}") from exc

        async with guard:
            self._owners[device_id] = owner_id

    async def unlock(self, device_id: str, owner_id: int) -> None:
        """Release the lock for *device_id* held by *owner_id*."""

        device_lock = await self._get_or_create(device_id)
        guard = await self._ensure_guard()
        async with guard:
            current_owner = self._owners.get(device_id)
            if current_owner != owner_id:
                raise DeviceLockOwnershipError(
                    f"Link {owner_id} does not own the lock for device {device_id!r}"
                )
            self._owners[device_id] = None
            if device_lock.locked():
                device_lock.release()

    async def force_unlock(self, device_id: str) -> None:
        """Force release of a lock regardless of the owner (used during cleanup)."""

        device_lock = await self._get_or_create(device_id)
        guard = await self._ensure_guard()
        async with guard:
            self._owners[device_id] = None
            if device_lock.locked():
                device_lock.release()

    async def status(self) -> Dict[str, Optional[int]]:
        """Return a snapshot of lock ownership: device_id -> owner_id (or None).

        This coroutine acquires the internal guard to provide a consistent
        snapshot and is intended for debugging/admin purposes.
        """
        guard = await self._ensure_guard()
        async with guard:
            return dict(self._owners)
