"""Start the embedded configuration GUI (standalone).

This script hosts the SPA and REST endpoints (GET /api/config, POST /api/config).
It does not provide a reload callback, so POST /api/reload will return 403.

Usage:
  python scripts/run_gui_standalone.py --config config.yaml --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# If the package isn't installed in the environment, make it importable by
# adding the repository's `src/` directory to sys.path when present. This
# mirrors a typical "src layout" developer workflow and makes the runner
# convenient without requiring an editable install.
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vxi_proxy.gui_server import ConfigGuiServer


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run configuration GUI (standalone)")
    p.add_argument("--config", "-c", type=Path, default=Path("config.yaml"), help="Path to configuration YAML")
    p.add_argument("--host", default="127.0.0.1", help="Host/interface to bind the GUI")
    p.add_argument("--port", type=int, default=8080, help="Port to bind the GUI (0 for ephemeral)")
    return p


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    server = ConfigGuiServer(args.config, args.host, args.port, reload_callback=None)
    try:
        runtime = server.start()
    except Exception as exc:
        print(f"Failed to start GUI server: {exc}", file=sys.stderr)
        return 2

    print(f"Configuration GUI available at http://{runtime.host}:{runtime.port}/")
    print("Press Enter or Ctrl+C to stop")
    try:
        # On Windows and other platforms, simple input loop is reliable for waiting.
        input()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
