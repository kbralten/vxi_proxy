"""Container entrypoint: start the VXI-11 facade (if available) and the GUI.

Environment variables:
- CONFIG_PATH: path to config YAML (default: /app/config.yaml)
- GUI_HOST: host/interface for GUI server (default: 0.0.0.0)
- GUI_PORT: port for GUI server (default: 8080)
- DISABLE_FACADE: set to "1" to skip starting the facade
- SERVER_HOST_OVERRIDE: set a host override for server.host in config (default: 0.0.0.0)
- DISABLE_SERVER_HOST_OVERRIDE: set to "1" to leave server.host unchanged
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

# Add src/ to sys.path when running from source checkout
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vxi_proxy.config import config_to_dict, load_config, save_config, ConfigurationError
from vxi_proxy.gui_server import ConfigGuiServer


def try_import_facade():
    try:
        from vxi_proxy.server import Vxi11ServerFacade  # type: ignore

        return Vxi11ServerFacade
    except Exception:
        return None


def maybe_override_server_host(config_path: Path, host: str, enabled: bool) -> None:
    if not enabled:
        return
    cfg = load_config(config_path)
    raw = config_to_dict(cfg)
    raw.setdefault("server", {})["host"] = host
    save_config(config_path, raw)


def ensure_default_config(config_path: Path) -> None:
    """Create a minimal default config if none exists yet.

    The default binds the facade and GUI to 0.0.0.0 and exposes empty devices/mappings.
    """
    if config_path.exists():
        return
    config_path.parent.mkdir(parents=True, exist_ok=True)
    default_raw = {
        "server": {
            "host": "0.0.0.0",
            "port": 1024,
            "portmapper_enabled": False,
            "gui": {"enabled": True, "host": "0.0.0.0", "port": 8080},
        },
        "devices": {},
        "mappings": {},
    }
    # save_config validates the structure before writing
    save_config(config_path, default_raw)


def main() -> int:
    config_path = Path(os.getenv("CONFIG_PATH", "/app/config.yaml"))
    gui_host = os.getenv("GUI_HOST", "0.0.0.0")
    try:
        gui_port = int(os.getenv("GUI_PORT", "8080"))
    except ValueError:
        gui_port = 8080

    disable_facade = os.getenv("DISABLE_FACADE") == "1"
    override_host_enabled = os.getenv("DISABLE_SERVER_HOST_OVERRIDE") != "1"
    server_host_override = os.getenv("SERVER_HOST_OVERRIDE", "0.0.0.0")

    # Ensure a config file exists to avoid boot failures in fresh images
    try:
        ensure_default_config(config_path)
    except ConfigurationError as exc:
        print(f"[entrypoint] Failed to write default config: {exc}", file=sys.stderr)
        return 1

    # Apply host override to bind the facade publicly inside the container
    try:
        maybe_override_server_host(config_path, server_host_override, override_host_enabled)
    except Exception as exc:
        print(f"[entrypoint] Warning: failed to apply server.host override: {exc}", file=sys.stderr)

    Facade = None if disable_facade else try_import_facade()

    # Start facade if available
    facade = None
    if Facade is not None:
        try:
            try:
                facade = Facade(config_path)
            except TypeError:
                facade = Facade()
            if hasattr(facade, "start"):
                facade.start()
            print("[entrypoint] Facade started")
        except Exception as exc:
            print(f"[entrypoint] Warning: failed to start facade: {exc}", file=sys.stderr)
            facade = None
    else:
        if not disable_facade:
            print("[entrypoint] Vxi11ServerFacade not found; starting GUI only")

    # Define reload callback used by GUI endpoint
    def reload_callback():
        from vxi_proxy.config import load_config as _load

        new_cfg = _load(config_path)
        if facade is None:
            return
        if hasattr(facade, "reload_config"):
            facade.reload_config(new_cfg)
            return
        if hasattr(facade, "apply_config"):
            facade.apply_config(new_cfg)
            return
        if hasattr(facade, "_config"):
            facade._config = new_cfg

    gui = ConfigGuiServer(config_path=config_path, host=gui_host, port=gui_port, reload_callback=reload_callback)
    runtime = gui.start()
    print(f"[entrypoint] GUI available at http://{runtime.host}:{runtime.port}/")

    stop_event = threading.Event()

    def handle_signal(_sig, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    finally:
        try:
            gui.stop()
        finally:
            if facade is not None and hasattr(facade, "stop"):
                try:
                    facade.stop()
                except Exception:
                    pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
