#!/usr/bin/env python3
"""Mock MODBUS server supporting TCP, RTU, and ASCII transports.

The RTU and ASCII servers listen on TCP ports so clients may connect using
``pyserial``'s ``socket://`` URL. All transports share a common in-memory
datastore so changes made via one interface are visible to the others.
"""

from __future__ import annotations

import argparse
import asyncio
import binascii
import logging
import struct
import threading
from typing import Dict, Iterable, List, Optional, Tuple

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer


_DEFAULT_UNITS = (1, 2)


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def _lrc(data: bytes) -> int:
    total = sum(data) & 0xFF
    return ((-total) & 0xFF)


class SharedDataStore:
    """Thread-safe MODBUS datastore shared across server transports."""

    def __init__(self, unit_ids: Iterable[int]) -> None:
        self._lock = threading.Lock()
        self._tables: Dict[int, Dict[str, List[int]]] = {}
        for unit in unit_ids:
            tables = {
                "co": [0] * 128,
                "di": [0] * 128,
                "hr": [0] * 256,
                "ir": [0] * 128,
            }

            temp_regs = struct.unpack(">HH", struct.pack(">f", 25.5))
            tables["hr"][0:2] = list(temp_regs)

            setpoint_regs = struct.unpack(">HH", struct.pack(">f", 20.0))
            tables["hr"][100:102] = list(setpoint_regs)

            voltage = 12345
            tables["ir"][0:2] = [(voltage >> 16) & 0xFFFF, voltage & 0xFFFF]

            self._tables[unit] = tables

    def _table(self, unit: int, kind: str) -> List[int]:
        try:
            return self._tables[unit][kind]
        except KeyError as exc:  # pragma: no cover - defensive guard
            raise ValueError(f"Unknown unit {unit} or table {kind!r}") from exc

    def read(self, unit: int, kind: str, address: int, count: int) -> List[int]:
        with self._lock:
            table = self._table(unit, kind)
            return list(table[address : address + count])

    def write(self, unit: int, kind: str, address: int, values: List[int]) -> None:
        with self._lock:
            table = self._table(unit, kind)
            for idx, value in enumerate(values):
                table[address + idx] = value & 0xFFFF


class StoreBackedDataBlock(ModbusSequentialDataBlock):
    """Proxy pymodbus data block that forwards to :class:`SharedDataStore`."""

    def __init__(self, store: SharedDataStore, unit: int, kind: str, size: int) -> None:
        super().__init__(0, [0] * size)
        self._store = store
        self._unit = unit
        self._kind = kind

    def getValues(self, address: int, count: int = 1) -> List[int]:  # type: ignore[override]
        return self._store.read(self._unit, self._kind, address, count)

    def setValues(self, address: int, values: List[int]) -> None:  # type: ignore[override]
        self._store.write(self._unit, self._kind, address, list(values))


def create_datastore(unit_ids: Iterable[int]) -> Tuple[SharedDataStore, ModbusServerContext]:
    store = SharedDataStore(unit_ids)
    slaves: Dict[int, ModbusSlaveContext] = {}
    for unit in unit_ids:
        slaves[unit] = ModbusSlaveContext(
            di=StoreBackedDataBlock(store, unit, "di", 128),
            co=StoreBackedDataBlock(store, unit, "co", 128),
            hr=StoreBackedDataBlock(store, unit, "hr", 256),
            ir=StoreBackedDataBlock(store, unit, "ir", 128),
        )
    context = ModbusServerContext(slaves=slaves, single=False)
    return store, context


def _parse_rtu_frame(frame: bytes) -> Tuple[int, int, bytes]:
    if len(frame) < 4:
        raise ValueError("RTU frame too short")
    if _crc16(frame[:-2]) != (frame[-2] | (frame[-1] << 8)):
        raise ValueError("RTU CRC mismatch")
    return frame[0], frame[1], frame[2:-2]


def _parse_ascii_frame(frame: bytes) -> Tuple[int, int, bytes]:
    if not frame.startswith(b":") or not frame.endswith(b"\r\n"):
        raise ValueError("ASCII frame delimiters invalid")
    payload = binascii.unhexlify(frame[1:-2])
    if len(payload) < 3:
        raise ValueError("ASCII payload too short")
    if payload[-1] != _lrc(payload[:-1]):
        raise ValueError("ASCII LRC mismatch")
    return payload[0], payload[1], payload[2:-1]


def _build_rtu_response(unit: int, function: int, payload: bytes) -> bytes:
    body = bytes([unit, function]) + payload
    crc = _crc16(body)
    return body + struct.pack("<H", crc)


def _build_ascii_response(unit: int, function: int, payload: bytes) -> bytes:
    body = bytes([unit, function]) + payload
    checksum = _lrc(body)
    return b":" + binascii.hexlify(body + bytes([checksum])).upper() + b"\r\n"


def _handle_modbus_operation(store: SharedDataStore, unit: int, function: int, payload: bytes) -> bytes:
    if function in (0x03, 0x04):
        address, count = struct.unpack(">HH", payload[:4])
        table = "hr" if function == 0x03 else "ir"
        registers = store.read(unit, table, address, count)
        data = bytearray([len(registers) * 2])
        for value in registers:
            data += struct.pack(">H", value)
        return bytes(data)

    if function == 0x06:
        address, value = struct.unpack(">HH", payload[:4])
        store.write(unit, "hr", address, [value])
        return payload[:4]

    if function == 0x10:
        address, count, byte_count = struct.unpack(">HHB", payload[:5])
        values = [struct.unpack(">H", payload[5 + idx * 2 : 7 + idx * 2])[0] for idx in range(count)]
        if byte_count != len(values) * 2:
            raise ValueError("Byte count mismatch in write multiple registers")
        store.write(unit, "hr", address, values)
        return struct.pack(">HH", address, count)

    raise ValueError(f"Unsupported function code 0x{function:02X}")


def process_modbus_request(store: SharedDataStore, protocol: str, frame: bytes) -> Optional[bytes]:
    try:
        if protocol == "rtu":
            unit, function, payload = _parse_rtu_frame(frame)
        else:
            unit, function, payload = _parse_ascii_frame(frame)

        response_payload = _handle_modbus_operation(store, unit, function, payload)
        if protocol == "rtu":
            return _build_rtu_response(unit, function, response_payload)
        return _build_ascii_response(unit, function, response_payload)
    except ValueError as exc:
        logging.debug("Failed to process %s frame: %s", protocol.upper(), exc)
        return None


def _rtu_expected_length(buffer: bytearray) -> Optional[int]:
    if len(buffer) < 2:
        return None
    function = buffer[1]
    if function in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
        return 8
    if function in (0x0F, 0x10):
        if len(buffer) >= 7:
            byte_count = buffer[6]
            return 9 + byte_count
        return None
    if function >= 0x80:
        return 5
    return None


def _try_extract_rtu_frame(buffer: bytearray) -> Optional[Tuple[bytes, int]]:
    expected = _rtu_expected_length(buffer)
    if expected is None or len(buffer) < expected:
        return None
    frame = bytes(buffer[:expected])
    return frame, expected


async def run_rtu_serial_server(store: SharedDataStore, host: str, port: int) -> None:
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logging.info("RTU client connected: %s", peer)
        buffer = bytearray()
        try:
            while True:
                chunk = await reader.read(256)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    extracted = _try_extract_rtu_frame(buffer)
                    if not extracted:
                        break
                    frame, consumed = extracted
                    del buffer[:consumed]
                    response = process_modbus_request(store, "rtu", frame)
                    if response:
                        writer.write(response)
                        await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.debug("RTU client error: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logging.info("RTU client disconnected: %s", peer)

    server = await asyncio.start_server(handle_client, host, port)
    logging.info("Mock MODBUS-RTU serial server on %s:%s (use socket://)", host, port)
    async with server:
        await server.serve_forever()


async def run_ascii_serial_server(store: SharedDataStore, host: str, port: int) -> None:
    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        logging.info("ASCII client connected: %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                # Ensure the frame includes the colon start sentinel.
                start = line.find(b":")
                if start == -1:
                    continue
                frame = line[start:]
                if not frame.endswith(b"\n"):
                    continue
                response = process_modbus_request(store, "ascii", frame)
                if response:
                    writer.write(response)
                    await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            logging.debug("ASCII client error: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logging.info("ASCII client disconnected: %s", peer)

    server = await asyncio.start_server(handle_client, host, port)
    logging.info("Mock MODBUS-ASCII serial server on %s:%s (use socket://)", host, port)
    async with server:
        await server.serve_forever()


async def run_servers(args: argparse.Namespace) -> None:
    unit_ids = [int(u.strip()) for u in args.units.split(",") if u.strip()] or list(_DEFAULT_UNITS)
    store, context = create_datastore(unit_ids)

    tasks = []

    if not args.no_tcp:
        logging.info(
            "Starting mock MODBUS-TCP server on %s:%s", args.host, args.port
        )
        tasks.append(
            asyncio.create_task(
                StartAsyncTcpServer(context=context, address=(args.host, args.port))
            )
        )

    if args.rtu_port:
        tasks.append(asyncio.create_task(run_rtu_serial_server(store, args.serial_host, args.rtu_port)))

    if args.ascii_port:
        tasks.append(asyncio.create_task(run_ascii_serial_server(store, args.serial_host, args.ascii_port)))

    if not tasks:
        logging.error("No server transports enabled; exiting")
        return

    await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock MODBUS server for integration tests")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for MODBUS-TCP server")
    parser.add_argument("--port", type=int, default=5020, help="TCP port for MODBUS-TCP server")
    parser.add_argument("--no-tcp", action="store_true", help="Disable MODBUS-TCP server")
    parser.add_argument("--serial-host", default="127.0.0.1", help="Bind address for virtual serial servers")
    parser.add_argument("--rtu-port", type=int, help="TCP port for MODBUS-RTU virtual serial server")
    parser.add_argument("--ascii-port", type=int, help="TCP port for MODBUS-ASCII virtual serial server")
    parser.add_argument("--units", default="1,2", help="Comma-separated MODBUS unit identifiers")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")

    try:
        asyncio.run(run_servers(args))
    except KeyboardInterrupt:  # pragma: no cover - manual stop
        logging.info("Mock MODBUS server stopped")


if __name__ == "__main__":
    main()
