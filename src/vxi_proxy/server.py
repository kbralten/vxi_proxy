"""VXI-11 façade server built on top of the python-vxi11 RPC primitives."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, Optional, TypeVar

from vxi11 import rpc
from vxi11 import vxi11 as vxi11_proto

from .adapters.base import AdapterError, DeviceAdapter
from .adapters.loopback import LoopbackAdapter
from .config import Config, DeviceDefinition, load_config
from .link_manager import LinkManager, LinkNotFoundError
from .resource_manager import (
    DeviceLockOwnershipError,
    DeviceLockedError,
    ResourceManager,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_RECV_SIZE = 1024 * 1024


# The upstream python-vxi11 module references the stdlib ``sys`` module from
# inside ``rpc.TCPServer.session`` without importing it. Inject the dependency
# proactively so that the server thread does not crash when it encounters a
# socket error during testing.
if not hasattr(rpc, "sys"):
    rpc.sys = sys


T = TypeVar("T")


class AsyncRuntime:
    """Run asyncio coroutines from synchronous RPC handlers."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._started = threading.Event()
        self._stopping = threading.Event()

    def start(self) -> None:
        if self._thread.is_alive():
            return
        self._thread.start()
        self._started.wait()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()
        self._stopping.set()

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._stopping.wait(timeout=5)
        self._thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    def run(self, coro: Coroutine[Any, Any, T]) -> T:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()


class AdapterFactory:
    """Instantiate backend adapters based on configuration definitions."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._builders: Dict[str, Callable[[DeviceDefinition], DeviceAdapter]] = {
            "loopback": self._build_loopback,
            "scpi-serial": self._build_scpi_serial,
            "scpi_serial": self._build_scpi_serial,
        }

    def resolve(self, device_name: str) -> DeviceDefinition:
        try:
            return self._config.devices[device_name]
        except KeyError as exc:
            raise KeyError(f"Device {device_name!r} not defined in configuration") from exc

    def build(self, definition: DeviceDefinition) -> DeviceAdapter:
        builder = self._builders.get(definition.type)
        if builder is None:
            raise AdapterError(f"No adapter implementation available for type {definition.type!r}")
        return builder(definition)

    def _build_loopback(self, definition: DeviceDefinition) -> DeviceAdapter:
        return LoopbackAdapter(definition.name)

    def _build_scpi_serial(self, definition: DeviceDefinition) -> DeviceAdapter:
        settings = dict(definition.settings)
        # Lazy import to avoid requiring pyserial unless this adapter is used
        from .adapters.scpi_serial import ScpiSerialAdapter  # type: ignore
        return ScpiSerialAdapter(definition.name, **settings)


class Vxi11CoreServer(rpc.TCPServer):
    """Implements the VXI-11 DEVICE_CORE program."""

    def __init__(
        self,
        host: str,
        port: int,
        config: Config,
        adapter_factory: AdapterFactory,
        runtime: AsyncRuntime,
        link_manager: LinkManager,
        resource_manager: ResourceManager,
        max_recv_size: int = DEFAULT_MAX_RECV_SIZE,
    ) -> None:
        super().__init__(host, vxi11_proto.DEVICE_CORE_PROG, vxi11_proto.DEVICE_CORE_VERS, port)
        self._config = config
        self._adapter_factory = adapter_factory
        self._runtime = runtime
        self._links = link_manager
        self._resources = resource_manager
        self._max_recv_size = max_recv_size
        self._client_threads: set[threading.Thread] = set()
        self._client_threads_lock = threading.Lock()

    # rpc.TCPServer hook -------------------------------------------------

    def loop(self) -> None:
        self.sock.listen()
        while True:
            try:
                connection = self.sock.accept()
            except OSError:
                break
            worker = threading.Thread(
                target=self._session_worker, args=(connection,), daemon=True
            )
            with self._client_threads_lock:
                self._client_threads.add(worker)
            worker.start()

    def _session_worker(self, connection: tuple[Any, Any]) -> None:
        sock, address = connection
        try:
            super().session((sock, address))
        finally:
            try:
                sock.close()
            except Exception:  # pragma: no cover - defensive cleanup
                LOGGER.warning("Failed to close client socket", exc_info=True)
            with self._client_threads_lock:
                self._client_threads.discard(threading.current_thread())

    # rpc.TCPServer hook -------------------------------------------------

    def addpackers(self) -> None:
        self.packer = vxi11_proto.Packer()
        self.unpacker = vxi11_proto.Unpacker(b"")

    # RPC method handlers ------------------------------------------------

    def handle_10(self) -> None:
        """Handle CREATE_LINK."""

        client_id, lock_device, lock_timeout_ms, device_name_raw = (
            self.unpacker.unpack_create_link_parms()
        )
        device_name = self._decode_device_name(device_name_raw)

        LOGGER.info("create_link request client_id=%s device=%s", client_id, device_name)

        error = vxi11_proto.ERR_NO_ERROR
        link_id = 0

        try:
            definition = self._adapter_factory.resolve(device_name)
            adapter = self._adapter_factory.build(definition)
            # connect() is intentionally lightweight; opening serial ports is
            # deferred until the adapter actually acquires the device lock.
            self._runtime.run(adapter.connect())
            link = self._runtime.run(self._links.create_link(device_name, adapter, client_id))
            link_id = link.lid
            if lock_device:
                timeout_s = lock_timeout_ms / 1000 if lock_timeout_ms else None
                try:
                    # Acquire global resource lock first
                    self._runtime.run(self._resources.lock(device_name, link_id, timeout_s))
                    # Now open the adapter (may raise AdapterError)
                    try:
                        self._runtime.run(adapter.acquire())
                        link.has_lock = True
                    except AdapterError:
                        # Failed to open device; cleanup: release resource and destroy link
                        try:
                            self._runtime.run(self._resources.unlock(device_name, link_id))
                        except Exception:
                            LOGGER.exception("Failed to release resource after adapter acquire failure")
                        try:
                            self._runtime.run(self._links.destroy_link(link_id))
                        except Exception:
                            LOGGER.exception("Failed to destroy link after adapter acquire failure")
                        link_id = 0
                        error = vxi11_proto.ERR_OUT_OF_RESOURCES
                except DeviceLockedError:
                    error = vxi11_proto.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK
        except KeyError:
            LOGGER.warning("Unknown device %s requested", device_name)
            error = vxi11_proto.ERR_DEVICE_NOT_ACCESSIBLE
        except AdapterError as exc:
            LOGGER.exception("Adapter creation failed for %s", device_name)
            error = vxi11_proto.ERR_OUT_OF_RESOURCES
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Unexpected failure during create_link")
            error = vxi11_proto.ERR_OUT_OF_RESOURCES

        self.turn_around()
        self.packer.pack_create_link_resp((error, link_id, 0, self._max_recv_size))

    def handle_11(self) -> None:
        """Handle DEVICE_WRITE."""

        link_id, timeout_ms, lock_timeout_ms, flags, data = (
            self.unpacker.unpack_device_write_parms()
        )
        LOGGER.debug(
            "device_write lid=%s len=%s timeout_ms=%s lock_timeout_ms=%s flags=0x%x",
            link_id,
            len(data),
            timeout_ms,
            lock_timeout_ms,
            flags,
        )

        error = vxi11_proto.ERR_NO_ERROR
        bytes_written = 0

        try:
            link = self._runtime.run(self._links.get(link_id))
            if getattr(link.adapter, "requires_lock", False) and not link.has_lock:
                raise DeviceLockOwnershipError("Lock required for device access")
            bytes_written = self._runtime.run(link.adapter.write(data))
        except LinkNotFoundError:
            error = vxi11_proto.ERR_INVALID_LINK_IDENTIFIER
        except DeviceLockOwnershipError:
            error = vxi11_proto.ERR_NO_LOCK_HELD_BY_THIS_LINK
        except asyncio.TimeoutError:
            error = vxi11_proto.ERR_IO_TIMEOUT
        except AdapterError:
            LOGGER.exception("Adapter write failed for lid=%s", link_id)
            error = vxi11_proto.ERR_IO_ERROR
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Unexpected failure during device_write")
            error = vxi11_proto.ERR_IO_ERROR

        self.turn_around()
        self.packer.pack_device_write_resp((error, bytes_written))

    def handle_12(self) -> None:
        """Handle DEVICE_READ."""

        link_id, request_size, timeout_ms, lock_timeout_ms, flags, term_char = (
            self.unpacker.unpack_device_read_parms()
        )
        LOGGER.debug(
            "device_read lid=%s request_size=%s timeout_ms=%s", link_id, request_size, timeout_ms
        )

        error = vxi11_proto.ERR_NO_ERROR
        reason = vxi11_proto.RX_END
        payload = b""

        try:
            link = self._runtime.run(self._links.get(link_id))
            if getattr(link.adapter, "requires_lock", False) and not link.has_lock:
                raise DeviceLockOwnershipError("Lock required for device access")
            payload = self._runtime.run(link.adapter.read(request_size))
            if not payload:
                reason = 0
        except LinkNotFoundError:
            error = vxi11_proto.ERR_INVALID_LINK_IDENTIFIER
        except DeviceLockOwnershipError:
            error = vxi11_proto.ERR_NO_LOCK_HELD_BY_THIS_LINK
        except asyncio.TimeoutError:
            error = vxi11_proto.ERR_IO_TIMEOUT
        except AdapterError:
            LOGGER.exception("Adapter read failed for lid=%s", link_id)
            error = vxi11_proto.ERR_IO_ERROR
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("Unexpected failure during device_read")
            error = vxi11_proto.ERR_IO_ERROR

        self.turn_around()
        self.packer.pack_device_read_resp((error, reason, payload))

    def handle_18(self) -> None:
        """Handle DEVICE_LOCK."""

        link_id, flags, lock_timeout_ms = self.unpacker.unpack_device_lock_parms()
        LOGGER.debug("device_lock lid=%s lock_timeout_ms=%s", link_id, lock_timeout_ms)

        error = vxi11_proto.ERR_NO_ERROR

        try:
            link = self._runtime.run(self._links.get(link_id))
            timeout_s = lock_timeout_ms / 1000 if lock_timeout_ms else None
            # Acquire global resource lock
            self._runtime.run(self._resources.lock(link.device_name, link.lid, timeout_s))
            # Acquire adapter (open serial port) after global lock
            try:
                self._runtime.run(link.adapter.acquire())
                link.has_lock = True
            except AdapterError:
                # Failed to open device; release global lock and report error
                try:
                    self._runtime.run(self._resources.unlock(link.device_name, link.lid))
                except Exception:
                    LOGGER.exception("Failed to release resource after adapter acquire failure")
                raise
        except LinkNotFoundError:
            error = vxi11_proto.ERR_INVALID_LINK_IDENTIFIER
        except DeviceLockedError:
            error = vxi11_proto.ERR_DEVICE_LOCKED_BY_ANOTHER_LINK
        except Exception:  # pragma: no cover
            LOGGER.exception("Unexpected failure during device_lock")
            error = vxi11_proto.ERR_IO_ERROR

        self.turn_around()
        self.packer.pack_device_error(error)

    def handle_19(self) -> None:
        """Handle DEVICE_UNLOCK."""

        link_id = self.unpacker.unpack_device_link()
        LOGGER.debug("device_unlock lid=%s", link_id)

        error = vxi11_proto.ERR_NO_ERROR

        try:
            link = self._runtime.run(self._links.get(link_id))
            if not link.has_lock:
                raise DeviceLockOwnershipError("Link does not hold lock")
            # Release global resource lock
            self._runtime.run(self._resources.unlock(link.device_name, link.lid))
            # Close adapter resources (run release inside the AsyncRuntime to safely touch asyncio primitives)
            try:
                self._runtime.run(self._async_adapter_release(link.adapter))
            except Exception:
                LOGGER.exception("Adapter release failed during device_unlock")
            link.has_lock = False
        except LinkNotFoundError:
            error = vxi11_proto.ERR_INVALID_LINK_IDENTIFIER
        except DeviceLockOwnershipError:
            error = vxi11_proto.ERR_NO_LOCK_HELD_BY_THIS_LINK
        except Exception:  # pragma: no cover
            LOGGER.exception("Unexpected failure during device_unlock")
            error = vxi11_proto.ERR_IO_ERROR

        self.turn_around()
        self.packer.pack_device_error(error)

    def handle_23(self) -> None:
        """Handle DESTROY_LINK."""

        link_id = self.unpacker.unpack_device_link()
        LOGGER.info("destroy_link lid=%s", link_id)

        error = vxi11_proto.ERR_NO_ERROR

        try:
            link = self._runtime.run(self._links.get(link_id))
            if link.has_lock:
                # Force release global lock
                self._runtime.run(self._resources.force_unlock(link.device_name))
                # Release adapter resources
                try:
                    self._runtime.run(self._async_adapter_release(link.adapter))
                except Exception:
                    LOGGER.exception("Adapter release failed during destroy_link")
            self._runtime.run(self._links.destroy_link(link_id))
        except LinkNotFoundError:
            error = vxi11_proto.ERR_INVALID_LINK_IDENTIFIER
        except Exception:  # pragma: no cover
            LOGGER.exception("Unexpected failure during destroy_link")
            error = vxi11_proto.ERR_IO_ERROR

        self.turn_around()
        self.packer.pack_device_error(error)

    # Unsupported calls -----------------------------------------------

    def handle_13(self) -> None:  # DEVICE_READSTB
        self._handle_not_supported()

    def handle_14(self) -> None:  # DEVICE_TRIGGER
        self._handle_not_supported()

    def handle_15(self) -> None:  # DEVICE_CLEAR
        self._handle_not_supported()

    def handle_16(self) -> None:  # DEVICE_REMOTE
        self._handle_not_supported()

    def handle_17(self) -> None:  # DEVICE_LOCAL
        self._handle_not_supported()

    def handle_20(self) -> None:  # DEVICE_ENABLE_SRQ
        self._handle_not_supported()

    def handle_22(self) -> None:  # DEVICE_DOCMD
        self._handle_not_supported()

    def handle_25(self) -> None:  # CREATE_INTR_CHAN
        self._handle_not_supported()

    def handle_26(self) -> None:  # DESTROY_INTR_CHAN
        self._handle_not_supported()

    # Internal helpers ------------------------------------------------

    def _handle_not_supported(self) -> None:
        self.turn_around()
        self.packer.pack_device_error(vxi11_proto.ERR_OPERATION_NOT_SUPPORTED)

    async def _async_adapter_release(self, adapter: DeviceAdapter) -> None:
        """Coroutine helper to call adapter.release() safely inside AsyncRuntime."""
        try:
            # adapter.release is synchronous in adapters; run in thread if it blocks
            await asyncio.to_thread(adapter.release)
        except Exception:
            LOGGER.exception("Exception while releasing adapter")

    @staticmethod
    def _decode_device_name(raw: bytes | str) -> str:
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)


@dataclass(slots=True)
class ServerContext:
    config_path: Path
    server: Vxi11CoreServer
    runtime: AsyncRuntime


class Vxi11ServerFacade:
    """High-level façade that wires configuration, adapters, and RPC server."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path
        self._config = load_config(config_path)
        self._runtime = AsyncRuntime()
        self._resources = ResourceManager()
        self._links = LinkManager()
        self._adapter_factory = AdapterFactory(self._config)
        self._server: Optional[Vxi11CoreServer] = None

    def start(self) -> ServerContext:
        """Start the façade and return context with server references."""

        self._runtime.start()
        server = Vxi11CoreServer(
            host=self._config.server.host,
            port=self._config.server.port,
            config=self._config,
            adapter_factory=self._adapter_factory,
            runtime=self._runtime,
            link_manager=self._links,
            resource_manager=self._resources,
        )
        if self._config.server.portmapper_enabled:
            try:
                server.register()
            except Exception:  # pragma: no cover - portmapper not always available
                LOGGER.warning(
                    "Portmapper registration failed; continuing with direct TCP access only",
                    exc_info=True,
                )
        self._server = server
        LOGGER.info(
            "VXI-11 core service listening on %s:%s", server.host, server.port
        )
        return ServerContext(config_path=self._config_path, server=server, runtime=self._runtime)

    def serve_forever(self) -> None:
        ctx = self.start()
        try:
            ctx.server.loop()
        except KeyboardInterrupt:
            LOGGER.info("Shutting down façade (Ctrl+C)")
        finally:
            self.stop()

    def stop(self) -> None:
        if self._server is None:
            return
        try:
            try:
                if self._config.server.portmapper_enabled:
                    self._server.unregister()
            except Exception:  # pragma: no cover - defensive cleanup
                LOGGER.warning("Failed to unregister VXI-11 program", exc_info=True)
            try:
                self._server.sock.close()
            except Exception:  # pragma: no cover - defensive cleanup
                LOGGER.warning("Failed to close VXI-11 server socket", exc_info=True)
        finally:
            self._runtime.stop()
            self._server = None


def run_from_cli(config_path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(message)s")
    facade = Vxi11ServerFacade(config_path)

    def _handle_shutdown(signum: int, _frame: object) -> None:
        LOGGER.info("Received signal %s, shutting down", signum)
        facade.stop()

    signal.signal(signal.SIGTERM, _handle_shutdown)

    facade.serve_forever()
