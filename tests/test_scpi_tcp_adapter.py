import socket
import asyncio
import sys
from pathlib import Path

# Ensure project src is on sys.path so tests can import package modules
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.adapters.scpi_tcp import ScpiTcpAdapter
from vxi_proxy.adapters.base import AdapterError


class FakeSocket:
    def __init__(self):
        self._buf = bytearray()
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def setsockopt(self, *args, **kwargs):
        pass

    def sendall(self, data: bytes):
        # echo into internal buffer as if remote replied immediately
        # for testing, we simulate echoing back the payload prefixed
        self._buf += data

    def recv(self, size: int) -> bytes:
        if not self._buf:
            raise socket.timeout()
        out = bytes(self._buf[:size])
        self._buf = self._buf[size:]
        return out

    def close(self):
        pass


import unittest


class ScpiTcpAdapterTests(unittest.TestCase):
    def test_write_and_read(self):
        fake = FakeSocket()

        def fake_create_connection(addr, timeout=None):
            return fake

        orig = socket.create_connection
        socket.create_connection = fake_create_connection
        try:
            adapter = ScpiTcpAdapter("test", host="127.0.0.1", port=5555, write_termination="\n", read_termination="\n")

            async def run():
                await adapter.acquire()
                n = await adapter.write(b"*IDN?")
                self.assertGreaterEqual(n, 1)
                data = await adapter.read(1024)
                self.assertIn(b"*IDN?", data)
                adapter.release()

            asyncio.run(run())
        finally:
            socket.create_connection = orig

    def test_missing_settings(self):
        with self.assertRaises(AdapterError):
            ScpiTcpAdapter("bad")


if __name__ == "__main__":
    unittest.main()
