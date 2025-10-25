"""Unit tests for the GenericRegexAdapter."""

import asyncio
import socket
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.adapters.base import AdapterError
from vxi_proxy.adapters.generic_regex import GenericRegexAdapter


class FakeTcpSocket:
    """Minimal TCP socket fake for exercising the adapter."""

    def __init__(self) -> None:
        self.timeout = None
        self.closed = False
        self.sent: list[bytes] = []
        self._responses: list[bytes] = []

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        if not self._responses:
            raise socket.timeout()
        return self._responses.pop(0)

    def queue_response(self, *chunks: bytes) -> None:
        self._responses.extend(chunks)

    def close(self) -> None:
        self.closed = True


class GenericRegexAdapterTests(unittest.TestCase):
    """Validate routing and formatting behaviour of GenericRegexAdapter."""

    @classmethod
    def setUpClass(cls) -> None:  # noqa: D401 - standard unittest hook
        try:
            cls._previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            cls._previous_loop = None
        cls._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(cls._loop)

    @classmethod
    def tearDownClass(cls) -> None:  # noqa: D401 - standard unittest hook
        cls._loop.close()
        if cls._previous_loop is not None and not cls._previous_loop.is_closed():
            asyncio.set_event_loop(cls._previous_loop)
        else:
            asyncio.set_event_loop(asyncio.new_event_loop())

    def setUp(self) -> None:  # noqa: D401 - standard unittest hook
        self._orig_create_connection = socket.create_connection

    def tearDown(self) -> None:  # noqa: D401 - standard unittest hook
        socket.create_connection = self._orig_create_connection

    def _run(self, coro):
        return self.__class__._loop.run_until_complete(coro)

    def test_tcp_request_response_roundtrip(self) -> None:
        fake = FakeTcpSocket()
        fake.queue_response(b"OK TEMP=26.5 MODE=AUTO\n")

        def fake_connect(addr, timeout=None):
            return fake

        socket.create_connection = fake_connect

        mappings = [
            {
                "pattern": r"^STAT$",
                "request_format": "STATUS\n",
                "expects_response": True,
                "response_regex": r"^OK TEMP=(?P<temp>\d+\.\d+) MODE=(?P<mode>\w+)$",
                "response_format": "TEMP=$temp\nMODE=$mode\n",
            }
        ]

        adapter = GenericRegexAdapter(
            "device",
            transport="tcp",
            host="127.0.0.1",
            port=9000,
            mappings=mappings,
            io_timeout=0.1,
        )

        async def scenario():
            await adapter.connect()
            written = await adapter.write(b"STAT")
            self.assertEqual(written, len(b"STAT"))
            self.assertIn(b"STATUS\n", fake.sent)
            payload = await adapter.read(256)
            self.assertEqual(payload.decode("ascii"), "TEMP=26.5\nMODE=AUTO\n")
            await adapter.disconnect()

        self._run(scenario())

    def test_fire_and_forget_clears_buffer(self) -> None:
        fake = FakeTcpSocket()

        def fake_connect(addr, timeout=None):
            return fake

        socket.create_connection = fake_connect

        mappings = [
            {
                "pattern": r"^SET:MODE\s+(\w+)$",
                "request_format": "MODE $1\n",
                "expects_response": False,
            }
        ]

        adapter = GenericRegexAdapter(
            "device",
            transport="tcp",
            host="127.0.0.1",
            port=9100,
            mappings=mappings,
        )

        async def scenario():
            await adapter.write(b"SET:MODE AUTO")
            payload = await adapter.read(128)
            self.assertEqual(payload, b"")
            await adapter.disconnect()

        self._run(scenario())

    def test_no_rule_match_raises(self) -> None:
        fake = FakeTcpSocket()

        def fake_connect(addr, timeout=None):
            return fake

        socket.create_connection = fake_connect

        mappings = [
            {
                "pattern": r"^PING$",
                "request_format": "PING\n",
                "expects_response": False,
            }
        ]

        adapter = GenericRegexAdapter(
            "device",
            transport="tcp",
            host="127.0.0.1",
            port=9200,
            mappings=mappings,
        )

        async def scenario():
            with self.assertRaises(AdapterError):
                await adapter.write(b"UNKNOWN")
            await adapter.disconnect()

        self._run(scenario())

    def test_invalid_rule_rejected(self) -> None:
        mappings = [
            {
                "pattern": r"^STAT$",
                "expects_response": True,
                "response_regex": r".*",
                "response_format": "$missing",
            }
        ]

        with self.assertRaises(AdapterError):
            GenericRegexAdapter(
                "device",
                transport="tcp",
                host="127.0.0.1",
                port=9300,
                mappings=mappings,
            )

    def test_non_newline_terminator(self) -> None:
        fake = FakeTcpSocket()
        # device returns payload and then a prompt '>' with no newline
        fake.queue_response(b"VALUE:123>")

        def fake_connect(addr, timeout=None):
            return fake

        socket.create_connection = fake_connect

        mappings = [
            {
                "pattern": r"^GETVAL$",
                "request_format": "READ\n",
                "expects_response": True,
                "terminator": ">",
                "response_regex": r"^VALUE:(?P<val>\d+)$",
                "response_format": "VAL=$val\n",
            }
        ]

        adapter = GenericRegexAdapter(
            "device",
            transport="tcp",
            host="127.0.0.1",
            port=9400,
            mappings=mappings,
            io_timeout=0.1,
        )

        async def scenario():
            await adapter.connect()
            await adapter.write(b"GETVAL")
            payload = await adapter.read(128)
            self.assertEqual(payload.decode("ascii"), "VAL=123\n")
            await adapter.disconnect()

        self._run(scenario())

if __name__ == "__main__":
    unittest.main()
