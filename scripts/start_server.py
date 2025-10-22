"""Launch the VXI-11 façade server using the example configuration."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from vxi_proxy.server import run_from_cli  # type: ignore[import]


def main() -> None:
    config_path = PROJECT_ROOT / "config.example.yaml"
    run_from_cli(config_path)


if __name__ == "__main__":
    main()
