"""Backend adapter implementations."""

from .base import DeviceAdapter, AdapterError
from .loopback import LoopbackAdapter

__all__ = ["DeviceAdapter", "AdapterError", "LoopbackAdapter"]
