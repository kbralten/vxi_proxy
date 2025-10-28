"""Start VXI-11 facade (if available) and the configuration GUI.

This attempts to import `Vxi11ServerFacade` from `vxi_proxy.server`. If it's not
present, the script will print a helpful message and exit with a non-zero code.

The reload callback will try to call `facade.reload_config(...)` if available,
otherwise it will attempt a best-effort assignment to internal state.

Usage:
  python scripts/run_gui_with_facade.py --config config.yaml --host 127.0.0.1 --port 8080
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

# Allow running from checkout without installing the package by adding src/
# to sys.path when present (developer-friendly behavior).
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from vxi_proxy.config import load_config
from vxi_proxy.gui_server import ConfigGuiServer


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Run VXI-11 facade and configuration GUI")
    p.add_argument("--config", "-c", type=Path, default=Path("config.yaml"), help="Path to configuration YAML")
    p.add_argument("--host", default="127.0.0.1", help="Host/interface to bind the GUI")
    p.add_argument("--port", type=int, default=8080, help="Port to bind the GUI (0 for ephemeral)")
    return p


def try_import_facade():
    try:
        # Import lazily so this script can still live in repos that don't expose the facade
        from vxi_proxy.server import Vxi11ServerFacade  # type: ignore

        return Vxi11ServerFacade
    except Exception:  # pragma: no cover - friendly error handling
        return None


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()

    Facade = try_import_facade()
    if Facade is None:
        print("Could not import Vxi11ServerFacade from vxi_proxy.server.")
        print("If you want to run the GUI with the running facade, ensure the project exposes Vxi11ServerFacade at vxi_proxy.server")
        return 2

    # Load initial config (best-effort)
    try:
        load_config(args.config)
    except Exception as exc:  # pragma: no cover - bubble helpful message
        print(f"Failed to load configuration {args.config}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 3

    # Instantiate and start the facade. The exact constructor may vary; we try common patterns.
    try:
        try:
            facade = Facade(args.config)
        except TypeError:
            # Fallback: try without args
            facade = Facade()
    except Exception as exc:  # pragma: no cover - best-effort start
        print(f"Failed to create VXI-11 facade instance: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 4

    # Try to start the facade if a start method is available
    try:
        if hasattr(facade, "start"):
            facade.start()
    except Exception as exc:  # pragma: no cover - best-effort
        print(f"Facade failed to start: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 5

    def reload_callback():
        """Reload configuration from disk and push it into the facade (best-effort).

        The callback raises exceptions on failure; `ConfigGuiServer` will translate
        exceptions into HTTP responses.
        """
        new_cfg = load_config(args.config)

        # Prefer a well-known reload method if present
        if hasattr(facade, "reload_config"):
            facade.reload_config(new_cfg)
            return

        # Best-effort: set internal attributes that many facades use
        if hasattr(facade, "_config"):
            try:
                facade._config = new_cfg
            except Exception:
                raise

        # If facade exposes an adapter factory or a apply_config method, try those too
        if hasattr(facade, "apply_config"):
            try:
                facade.apply_config(new_cfg)
                return
            except Exception:
                # let the outer handler return HTTP 500
                raise

        # Nothing else to do; return silently to indicate success
        return

    def resource_state_callback() -> dict:
        # Return a synchronous snapshot of resource ownership by using the
        # facade's AsyncRuntime to run the ResourceManager.status() coroutine.
        try:
            if hasattr(facade, "_resources") and hasattr(facade, "_runtime"):
                return facade._runtime.run(facade._resources.status())
        except Exception:
            # Best-effort: return empty mapping on failure
            pass
        return {}

    gui = ConfigGuiServer(
        args.config,
        args.host,
        args.port,
        reload_callback=reload_callback,
        resource_state_callback=resource_state_callback,
    )
    try:
        runtime = gui.start()
    except Exception as exc:
        print(f"Failed to start GUI server: {exc}", file=sys.stderr)
        traceback.print_exc()
        try:
            if hasattr(facade, "stop"):
                facade.stop()
        finally:
            return 6

    print(f"Configuration GUI available at http://{runtime.host}:{runtime.port}/")
    print("Press Enter or Ctrl+C to stop")

    try:
        input()
    except KeyboardInterrupt:
        pass
    finally:
        gui.stop()
        if hasattr(facade, "stop"):
            try:
                facade.stop()
            except Exception:
                traceback.print_exc()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
