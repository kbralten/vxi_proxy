"""Embedded configuration GUI web server."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from aiohttp import web

from .config import ConfigurationError, config_to_dict, load_config, save_config


_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class GuiRuntime:
	"""Runtime information about the GUI server."""

	host: str
	port: int


class ConfigGuiServer:
	"""Serve the configuration SPA and REST API."""

	def __init__(
		self,
		config_path: Path,
		host: str,
		port: int,
		reload_callback: Optional[Callable[[], None]] = None,
	) -> None:
		self._config_path = config_path
		self._host = host
		self._port = port
		self._reload_callback = reload_callback

		self._thread: Optional[threading.Thread] = None
		self._loop: Optional[asyncio.AbstractEventLoop] = None
		self._runner: Optional[web.AppRunner] = None
		self._site: Optional[web.TCPSite] = None
		self._bound_port: Optional[int] = None

		self._started = threading.Event()
		self._stopped = threading.Event()
		self._start_error: Optional[Exception] = None

		self._static_root = Path(__file__).resolve().parent / "static" / "gui"

	# ------------------------------------------------------------------
	# Public control surface
	# ------------------------------------------------------------------

	def start(self) -> GuiRuntime:
		if self._thread is not None:
			raise RuntimeError("GUI server already started")

		self._thread = threading.Thread(target=self._thread_main, daemon=True)
		self._thread.start()
		self._started.wait()

		if self._start_error is not None:
			raise RuntimeError("Failed to start GUI server") from self._start_error

		assert self._bound_port is not None
		return GuiRuntime(host=self._host, port=self._bound_port)

	def stop(self) -> None:
		if self._loop is None:
			return

		loop = self._loop

		def _stop_loop() -> None:
			loop.stop()

		loop.call_soon_threadsafe(_stop_loop)
		if self._thread is not None:
			self._thread.join(timeout=5)
		self._stopped.wait(timeout=5)

	# ------------------------------------------------------------------
	# Thread + event loop setup
	# ------------------------------------------------------------------

	def _thread_main(self) -> None:
		loop = asyncio.new_event_loop()
		asyncio.set_event_loop(loop)
		self._loop = loop

		try:
			app = web.Application(middlewares=[self._json_error_middleware])
			self._configure_routes(app)
			runner = web.AppRunner(app)
			loop.run_until_complete(runner.setup())
			self._runner = runner

			site = web.TCPSite(runner, self._host, self._port)
			loop.run_until_complete(site.start())
			self._site = site

			sockets = getattr(site._server, "sockets", [])  # type: ignore[attr-defined]
			if sockets:
				self._bound_port = sockets[0].getsockname()[1]
			else:  # pragma: no cover - defensive guard
				raise RuntimeError("GUI server failed to bind socket")

			self._started.set()
			_LOG.info("Configuration GUI available at http://%s:%s", self._host, self._bound_port)

			loop.run_forever()
		except Exception as exc:  # pragma: no cover - best effort logging
			self._start_error = exc
			_LOG.exception("GUI server failed to start")
			self._started.set()
		finally:
			try:
				if self._runner is not None:
					loop.run_until_complete(self._runner.cleanup())
			finally:
				self._stopped.set()
				asyncio.set_event_loop(None)
				loop.close()

	# ------------------------------------------------------------------
	# Application setup
	# ------------------------------------------------------------------

	def _configure_routes(self, app: web.Application) -> None:
		app.router.add_get("/", self._handle_index)
		app.router.add_get("/api/config", self._handle_get_config)
		app.router.add_post("/api/config", self._handle_update_config)
		app.router.add_post("/api/reload", self._handle_reload)
		if self._static_root.exists():
			app.router.add_static("/static", self._static_root, show_index=False)

	# ------------------------------------------------------------------
	# Request handlers
	# ------------------------------------------------------------------

	async def _handle_index(self, _request: web.Request) -> web.Response:
		return web.FileResponse(self._static_root / "index.html")

	async def _handle_get_config(self, _request: web.Request) -> web.Response:
		config_dict = await asyncio.to_thread(self._read_config)
		return web.json_response(config_dict)

	async def _handle_update_config(self, request: web.Request) -> web.Response:
		try:
			payload = await request.json()
		except json.JSONDecodeError as exc:
			raise web.HTTPBadRequest(text="Invalid JSON payload", reason=str(exc)) from exc

		if not isinstance(payload, dict):
			raise web.HTTPBadRequest(text="Configuration payload must be a JSON object")

		try:
			await asyncio.to_thread(save_config, self._config_path, payload)
		except ConfigurationError as exc:
			_LOG.error("Failed to save configuration: %s", exc)
			raise web.HTTPBadRequest(text="Invalid configuration data") from exc

		return web.json_response({"status": "ok"})

	async def _handle_reload(self, _request: web.Request) -> web.Response:
		if self._reload_callback is None:
			raise web.HTTPForbidden(text="Reload endpoint is disabled")

		loop = asyncio.get_running_loop()
		try:
			await loop.run_in_executor(None, self._reload_callback)
		except ConfigurationError as exc:
			raise web.HTTPBadRequest(text="Invalid configuration data") from exc
		except Exception as exc:  # pragma: no cover - defensive logging
			_LOG.exception("Configuration reload failed")
			raise web.HTTPInternalServerError(text="Configuration reload failed") from exc

		return web.json_response({"status": "ok"})

	# ------------------------------------------------------------------
	# Helpers
	# ------------------------------------------------------------------

	def _read_config(self) -> Dict[str, Any]:
		config = load_config(self._config_path)
		return config_to_dict(config)

	@staticmethod
	@web.middleware
	async def _json_error_middleware(request: web.Request, handler: Callable[[web.Request], web.StreamResponse]) -> web.StreamResponse:
		try:
			return await handler(request)
		except web.HTTPException as exc:
			if exc.content_type == "application/json":
				raise
			payload = {"error": exc.reason or exc.text or exc.status}
			return web.json_response(payload, status=exc.status)

