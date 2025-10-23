"""Lightweight asyncio-based mock SCPI TCP server for integration testing.

Responds to basic SCPI queries like *IDN? and MEAS:VOLT?
"""

from __future__ import annotations

import argparse
import asyncio
import logging

_LOG = logging.getLogger(__name__)


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    addr = writer.get_extra_info("peername")
    _LOG.info("Client connected %s", addr)
    try:
        while not reader.at_eof():
            try:
                data = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                break
            except Exception:
                # Fallback: try a small read
                data = await reader.read(4096)
                if not data:
                    break
            line = data.strip().decode("utf-8", errors="ignore")
            _LOG.debug("Received: %r", line)
            resp = None
            if line.upper() == "*IDN?":
                resp = "Mock Instruments Inc.,SCPI-TCP-1000,SIMTCP,1.0.0\n"
            elif line.upper().startswith("MEAS:VOLT"):
                resp = "5.0000\n"
            elif line.upper() == "*RST":
                resp = "OK\n"
            else:
                # Echo back for simple tests
                resp = f"ECHO: {line}\n"
            writer.write(resp.encode("utf-8"))
            await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        _LOG.info("Client disconnected %s", addr)


async def run_server(host: str, port: int) -> None:
    server = await asyncio.start_server(handle_client, host, port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    _LOG.info("Mock SCPI-TCP server listening on %s", addrs)
    async with server:
        await server.serve_forever()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    args = parser.parse_args()
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        _LOG.info("Mock server stopped")


if __name__ == "__main__":
    main()
