"""Compatibility implementation of the stdlib xdrlib module for Python 3.13."""

from __future__ import annotations

import struct
from functools import wraps
from io import BytesIO
from typing import Any, Callable, Iterable, Sequence

__all__ = ["Error", "ConversionError", "Packer", "Unpacker"]


class Error(Exception):
    """Base error for XDR packing/unpacking issues."""

    def __init__(self, msg: str) -> None:
        self.msg = msg

    def __repr__(self) -> str:
        return repr(self.msg)

    def __str__(self) -> str:
        return str(self.msg)


class ConversionError(Error):
    """Raised when a value cannot be represented in XDR."""


def raise_conversion_error(function: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    """Wrap struct.error and convert to ConversionError."""

    @wraps(function)
    def result(self: Any, value: Any) -> Any:
        try:
            return function(self, value)
        except struct.error as exc:
            raise ConversionError(exc.args[0]) from None

    return result


class Packer:
    """Serialize Python values into XDR byte sequences."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.__buf = BytesIO()

    def get_buffer(self) -> bytes:
        return self.__buf.getvalue()

    get_buf = get_buffer

    @raise_conversion_error
    def pack_uint(self, value: int) -> None:
        self.__buf.write(struct.pack(">L", value))

    @raise_conversion_error
    def pack_int(self, value: int) -> None:
        self.__buf.write(struct.pack(">l", value))

    pack_enum = pack_int

    def pack_bool(self, value: bool) -> None:
        self.__buf.write(b"\0\0\0\1" if value else b"\0\0\0\0")

    def pack_uhyper(self, value: int) -> None:
        self.pack_uint((value >> 32) & 0xFFFFFFFF)
        self.pack_uint(value & 0xFFFFFFFF)

    pack_hyper = pack_uhyper

    @raise_conversion_error
    def pack_float(self, value: float) -> None:
        self.__buf.write(struct.pack(">f", value))

    @raise_conversion_error
    def pack_double(self, value: float) -> None:
        self.__buf.write(struct.pack(">d", value))

    def pack_fstring(self, size: int, data: bytes) -> None:
        if size < 0:
            raise ValueError("fstring size must be nonnegative")
        payload = data[:size]
        padded = ((size + 3) // 4) * 4
        payload += b"\0" * (padded - len(payload))
        self.__buf.write(payload)

    pack_fopaque = pack_fstring

    def pack_string(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        length = len(data)
        self.pack_uint(length)
        self.pack_fstring(length, data)

    pack_opaque = pack_string
    pack_bytes = pack_string

    def pack_list(self, values: Iterable[Any], pack_item: Callable[[Any], None]) -> None:
        for item in values:
            self.pack_uint(1)
            pack_item(item)
        self.pack_uint(0)

    def pack_farray(self, count: int, values: Sequence[Any], pack_item: Callable[[Any], None]) -> None:
        if len(values) != count:
            raise ValueError("wrong array size")
        for item in values:
            pack_item(item)

    def pack_array(self, values: Sequence[Any], pack_item: Callable[[Any], None]) -> None:
        length = len(values)
        self.pack_uint(length)
        self.pack_farray(length, values, pack_item)


class Unpacker:
    """Deserialize XDR byte sequences into Python values."""

    def __init__(self, data: bytes | str) -> None:
        self.reset(data)

    def reset(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.__buf = data
        self.__pos = 0

    def get_position(self) -> int:
        return self.__pos

    def set_position(self, position: int) -> None:
        self.__pos = position

    def get_buffer(self) -> bytes:
        return self.__buf

    def done(self) -> None:
        if self.__pos < len(self.__buf):
            raise Error("unextracted data remains")

    def unpack_uint(self) -> int:
        start = self.__pos
        self.__pos = end = start + 4
        data = self.__buf[start:end]
        if len(data) < 4:
            raise EOFError
        return struct.unpack(">L", data)[0]

    def unpack_int(self) -> int:
        start = self.__pos
        self.__pos = end = start + 4
        data = self.__buf[start:end]
        if len(data) < 4:
            raise EOFError
        return struct.unpack(">l", data)[0]

    unpack_enum = unpack_int

    def unpack_bool(self) -> bool:
        return bool(self.unpack_int())

    def unpack_uhyper(self) -> int:
        hi = self.unpack_uint()
        lo = self.unpack_uint()
        return (hi << 32) | lo

    def unpack_hyper(self) -> int:
        value = self.unpack_uhyper()
        if value >= 0x8000000000000000:
            value -= 0x10000000000000000
        return value

    def unpack_float(self) -> float:
        start = self.__pos
        self.__pos = end = start + 4
        data = self.__buf[start:end]
        if len(data) < 4:
            raise EOFError
        return struct.unpack(">f", data)[0]

    def unpack_double(self) -> float:
        start = self.__pos
        self.__pos = end = start + 8
        data = self.__buf[start:end]
        if len(data) < 8:
            raise EOFError
        return struct.unpack(">d", data)[0]

    def unpack_fstring(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("fstring size must be nonnegative")
        start = self.__pos
        padded = ((size + 3) // 4) * 4
        end = start + padded
        if end > len(self.__buf):
            raise EOFError
        self.__pos = end
        return self.__buf[start : start + size]

    unpack_fopaque = unpack_fstring

    def unpack_string(self) -> bytes:
        length = self.unpack_uint()
        return self.unpack_fstring(length)

    unpack_opaque = unpack_string
    unpack_bytes = unpack_string

    def unpack_list(self, unpack_item: Callable[[], Any]) -> list[Any]:
        items = []
        while True:
            marker = self.unpack_uint()
            if marker == 0:
                break
            if marker != 1:
                raise ConversionError(f"0 or 1 expected, got {marker!r}")
            items.append(unpack_item())
        return items

    def unpack_farray(self, count: int, unpack_item: Callable[[], Any]) -> list[Any]:
        return [unpack_item() for _ in range(count)]

    def unpack_array(self, unpack_item: Callable[[], Any]) -> list[Any]:
        length = self.unpack_uint()
        return self.unpack_farray(length, unpack_item)
