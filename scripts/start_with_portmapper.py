"""Start the minimal portmapper and the VXI-11 facade for local testing.

This script is for developer convenience — it will:
- add `src/` to PYTHONPATH when run from the repository root
- start the user-space `PortMapperServer` (optional)
- start the VXI-11 facade via the existing `run_from_cli` entrypoint

Usage:
    python scripts/start_with_portmapper.py [--config PATH] [--no-portmapper]

Note: binding to TCP/UDP port 111 requires elevated privileges on many
platforms. If you cannot bind to 111, run the portmapper on an alternate
port for local testing (use --portmap-port).
"""
from __future__ import annotations

from pathlib import Path
import sys
import argparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.portmapper import PortMapperServer  # type: ignore
from vxi_proxy.server import run_from_cli  # type: ignore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", type=Path, default=PROJECT_ROOT / "config.yaml")
    parser.add_argument("--no-portmapper", action="store_true", help="Do not start the portmapper")
    parser.add_argument("--portmap-port", type=int, default=111, help="Port for the portmapper to bind (default 111)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host/interface for portmapper")
    args = parser.parse_args()

    config_path = args.config
    if not config_path.exists():
        alt = PROJECT_ROOT / "config.example.yaml"
        if alt.exists():
            config_path = alt

    pmap = None
    if not args.no_portmapper:
        try:
            pmap = PortMapperServer(host=args.host, port=args.portmap_port, vxi_port=None, config_path=config_path)
            pmap.start()
            print(f"Portmapper started on {args.host}:{args.portmap_port} (may require elevated privileges to bind 111)")
        except Exception as exc:
            print(f"Warning: failed to start portmapper: {exc}")

    try:
        # run_from_cli will run the facade and block until shutdown
        run_from_cli(config_path)
    except KeyboardInterrupt:
        print("Interrupted, shutting down...")
    finally:
        if pmap is not None:
            try:
                pmap.stop()
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
