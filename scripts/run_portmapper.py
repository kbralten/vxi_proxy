#!/usr/bin/env python3
"""Run the minimal user-space portmapper (rpcbind) for VXI-11.

This standalone script is intended to be executed as root/Administrator
when binding to privileged port 111. It can also be used for local testing
on a high port by passing --port.

Example:
  # run as root to bind TCP/UDP 111
  python scripts/run_portmapper.py --vxi-port 1024

  # run as unprivileged user on high port for testing
  python scripts/run_portmapper.py --port 11111 --vxi-port 1024
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Optional

from vxi_proxy.portmapper import PortMapperServer


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_portmapper")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the portmapper")
    parser.add_argument("--port", type=int, default=111, help="Port to bind (default: 111)")
    parser.add_argument("--vxi-port", type=int, default=None, help="VXI-11 server port to return for GETPORT")
    parser.add_argument("--no-udp", action="store_true", help="Disable UDP listener")
    parser.add_argument("--no-tcp", action="store_true", help="Disable TCP listener")
    parser.add_argument("--config", type=Path, default=None, help="Optional config.yaml to read VXI port from")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    enable_udp = not args.no_udp
    enable_tcp = not args.no_tcp

    pm = PortMapperServer(host=args.host, port=args.port, vxi_port=args.vxi_port, enable_udp=enable_udp, enable_tcp=enable_tcp, config_path=args.config)

    def _stop(signum, frame):
        logging.getLogger(__name__).info("Stopping portmapper (signal=%s)", signum)
        pm.stop()

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except Exception:
        # Not all platforms have SIGTERM
        pass

    try:
        pm.start()
        logging.getLogger(__name__).info("Portmapper running on %s:%d (UDP=%s TCP=%s). Press Ctrl-C to stop.", args.host, args.port, enable_udp, enable_tcp)
        # Wait until stopped via signal
        # PortMapperServer uses threads; just wait on the main thread
        while True:
            try:
                signal.pause()
            except AttributeError:
                # Windows doesn't have signal.pause(); sleep in a loop
                import time

                while True:
                    if not any(t.is_alive() for t in pm._threads):
                        break
                    time.sleep(0.5)
                break
            except KeyboardInterrupt:
                break
    except Exception as exc:
        logging.getLogger(__name__).exception("Portmapper failed: %s", exc)
        return 2
    finally:
        pm.stop()

    logging.getLogger(__name__).info("Portmapper stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
