"""Command-line entry point for the VXI proxy façade."""

from __future__ import annotations

import argparse
from pathlib import Path

from .server import run_from_cli


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the VXI-11 façade server")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to the gateway configuration file",
    )
    args = parser.parse_args()
    run_from_cli(args.config)


if __name__ == "__main__":
    main()
