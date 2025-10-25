"""Mock server for exercising the generic regex protocol adapter."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from typing import Optional

LOG = logging.getLogger("mock_generic_protocol")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, args: argparse.Namespace) -> None:
    peer = writer.get_extra_info("peername")
    LOG.info("Client connected: %s", peer)
    mode_state: Optional[str] = None
    try:
        while True:
            data = await reader.readline()
            if not data:
                break
            text = data.decode("ascii", errors="ignore").strip()
            LOG.debug("Received command: %r", text)

            if text.upper() == "STATUS":
                if args.delay > 0:
                    await asyncio.sleep(args.delay)
                if args.malformed:
                    reply = "ERR ???\n"
                else:
                    reply = f"OK TEMP={args.temp:.1f} MODE={mode_state or args.mode}\n"
                writer.write(reply.encode("ascii"))
                await writer.drain()
            elif text.upper().startswith("MODE "):
                mode_state = text.split(" ", 1)[1]
                if args.echo_mode:
                    writer.write(f"OK MODE {mode_state}\n".encode("ascii"))
                    await writer.drain()
                # Fire-and-forget by default (no response)
            elif text.upper() == "PING":
                writer.write(b"PONG\n")
                await writer.drain()
            elif text.upper() == "BADREPLY":
                writer.write(b"THIS_IS_NOT_MATCHING\n")
                await writer.drain()
            else:
                writer.write(b"ERR UNKNOWN\n")
                await writer.drain()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        LOG.info("Client disconnected: %s", peer)


async def run_server(args: argparse.Namespace) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, args), args.host, args.port
    )
    host = args.host or "0.0.0.0"
    addrs = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    LOG.info("Mock generic protocol server listening on %s", addrs)

    stop_event = asyncio.Event()

    def _stop(*_sig) -> None:
        if not stop_event.is_set():
            LOG.info("Stopping mock server")
            stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    await stop_event.wait()

    server.close()
    await server.wait_closed()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address for the mock server")
    parser.add_argument("--port", type=int, default=9100, help="TCP port for the mock server")
    parser.add_argument("--temp", type=float, default=26.5, help="Temperature reported in STATUS replies")
    parser.add_argument("--mode", default="AUTO", help="Default mode reported in STATUS replies")
    parser.add_argument("--malformed", action="store_true", help="Emit malformed reply for STATUS")
    parser.add_argument("--delay", type=float, default=0.0, help="Artificial delay before STATUS replies")
    parser.add_argument("--echo-mode", action="store_true", help="Echo acknowledgement for MODE commands")
    parser.add_argument("--log-level", default="INFO", help="Logging level (e.g. DEBUG)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="[%(asctime)s] %(levelname)s %(message)s")
    try:
        asyncio.run(run_server(args))
    except KeyboardInterrupt:  # pragma: no cover - interactive usage
        LOG.info("Interrupted; exiting")


if __name__ == "__main__":
    main()
