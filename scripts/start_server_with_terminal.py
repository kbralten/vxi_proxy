"""Launch the VXI-11 façade server and automatically attach the interactive terminal."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.server import Vxi11ServerFacade  # type: ignore[import]
from vxi_proxy.terminal import main as terminal_main  # type: ignore[import]

CONFIG_DEVICE = "loopback0"


def _wait_for_server(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    last_error: Optional[OSError] = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05)
    raise TimeoutError(f"Server {host}:{port} did not become ready") from last_error


def main() -> int:
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        config_path = PROJECT_ROOT / "config.example.yaml"
    facade = Vxi11ServerFacade(config_path)
    ctx = facade.start()
    worker = threading.Thread(target=ctx.server.loop, daemon=True)
    worker.start()

    try:
        connect_host = ctx.server.host
        if connect_host in {"0.0.0.0", ""}:
            connect_host = "127.0.0.1"
        elif connect_host == "::":
            connect_host = "::1"

        _wait_for_server(connect_host, ctx.server.port)
        print(f"Server running on {ctx.server.host}:{ctx.server.port}")
        print("Starting terminal; press Ctrl+C to exit.")
        exit_code = terminal_main(
            [
                "--host",
                connect_host,
                "--port",
                str(ctx.server.port),
                "--device",
                CONFIG_DEVICE,
                "--lock",
            ]
        )
        return exit_code
    finally:
        facade.stop()
        worker.join(timeout=1.0)


if __name__ == "__main__":
    sys.exit(main())
