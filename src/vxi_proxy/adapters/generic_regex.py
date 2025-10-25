"""Generic regex-driven adapter for bespoke ASCII protocols."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
import math
from typing import Any, Iterable, Optional, cast

from .base import AdapterError, DeviceAdapter

try:  # pragma: no cover - pyserial may be absent when tests patch it
    import serial as _serial  # type: ignore
except Exception:  # pragma: no cover - lazy import behaviour mirrors scpi_serial
    _serial = None

serial = cast(Any, _serial)


LOG = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"\$(\w+)|\$\{(\w+)\}")


@dataclass(slots=True)
class _CompiledRule:
    pattern_text: str
    pattern: re.Pattern[str]
    request_template: str
    expects_response: bool
    response_pattern_text: Optional[str]
    response_pattern: Optional[re.Pattern[str]]
    response_template: Optional[str]
    request_tokens: tuple[str, ...]
    response_tokens: tuple[str, ...]
    terminator: Optional[str]
    # scaling factors: applied to writes (scale) and reads (response_scale)
    scale: Optional[float]
    response_scale: Optional[float]
    # inferred widths for numeric groups from the response pattern (name -> width)
    group_widths: dict[str, int]
    payload_width: Optional[int]


class GenericRegexAdapter(DeviceAdapter):
    """Adapter that maps SCPI-like commands via configurable regex rules."""

    def __init__(self, name: str, **settings: Any) -> None:
        super().__init__(name)

        transport = str(settings.get("transport", "tcp")).lower()
        if transport not in {"tcp", "serial"}:
            raise AdapterError("generic-regex 'transport' must be 'tcp' or 'serial'")
        self._transport = transport

        self._encoding = str(settings.get("encoding", "ascii"))
        self._io_timeout = float(settings.get("io_timeout", 1.0))
        self._connect_timeout = float(settings.get("connect_timeout", 1.0))
        self._max_response_bytes = int(settings.get("max_response_bytes", 4096))
        if self._max_response_bytes <= 0:
            raise AdapterError("max_response_bytes must be positive")

        # Default locking mirrors transport expectations
        default_requires_lock = False if transport == "tcp" else True
        self.requires_lock = bool(settings.get("requires_lock", default_requires_lock))

        default_chunk = 1024 if transport == "tcp" else 16
        self._recv_chunk_size = max(1, int(settings.get("recv_chunk_size", default_chunk)))

        # Transport-specific configuration
        if transport == "tcp":
            host = settings.get("host")
            port = settings.get("port")
            if not host:
                raise AdapterError("generic-regex tcp transport requires 'host'")
            if port is None:
                raise AdapterError("generic-regex tcp transport requires 'port'")
            try:
                self._tcp_host = str(host)
                self._tcp_port = int(port)
            except Exception as exc:  # pragma: no cover - defensive conversion
                raise AdapterError("Invalid TCP host/port configuration") from exc
            self._socket: Optional[socket.socket] = None
        else:
            port_name = settings.get("serial_port") or settings.get("port")
            if not port_name:
                raise AdapterError("generic-regex serial transport requires 'serial_port' or 'port'")
            self._serial_port = str(port_name)
            self._serial_settings = {
                "baudrate": int(settings.get("baudrate", 9600)),
                "bytesize": int(settings.get("bytesize", 8)),
                "parity": str(settings.get("parity", "N")),
                "stopbits": float(settings.get("stopbits", 1)),
                "timeout": self._io_timeout,
                "write_timeout": self._io_timeout,
            }
            self._serial_handle: Any | None = None

        mappings = settings.get("mappings")
        if not isinstance(mappings, Iterable):
            raise AdapterError("generic-regex requires a list of mapping rules")

        self._rules = self._compile_rules(list(mappings))

        # Buffer exposed via read()
        self._read_buffer: str = ""

        # Internal lock for concurrent write/read operations when not using VXI locks
        self._io_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def connect(self) -> None:
        # Parameter validation occurs in __init__
        return None

    async def acquire(self) -> None:
        await super().acquire()
        await self._ensure_connected()

    async def disconnect(self) -> None:
        await asyncio.to_thread(self._close_transport)

    def release(self) -> None:
        self._close_transport()
        super().release()

    async def write(self, data: bytes) -> int:
        command = data.decode(self._encoding, errors="strict").strip()
        if not command:
            raise AdapterError("Empty command received")

        rule, match = self._match_rule(command)
        request_text = self._render_template(
            rule.request_template, match, rule.request_tokens, rule, is_request=True
        )
        payload = request_text.encode(self._encoding, errors="strict")

        async with self._io_lock:
            await self._ensure_connected()
            await self._send(payload)

            if rule.expects_response:
                if rule.response_pattern is None or rule.response_template is None:
                    raise AdapterError("Rule expects a response but no response parser configured")
                response_text, response_match = await self._receive_response(rule)
                formatted = self._render_template(
                    rule.response_template,
                    response_match,
                    rule.response_tokens,
                    rule,
                    is_request=False,
                )
                self._read_buffer = formatted
                LOG.debug(
                    "generic_regex.write: command=%r request=%r response=%r formatted=%r",
                    command,
                    request_text,
                    response_text,
                    formatted,
                )
            else:
                self._read_buffer = ""

        return len(data)

    async def read(self, request_size: int) -> bytes:
        buffer = self._read_buffer
        self._read_buffer = ""
        if not buffer:
            return b""
        truncated = buffer[: max(1, request_size)] if request_size > 0 else buffer
        return truncated.encode(self._encoding, errors="strict")

    # ------------------------------------------------------------------
    def _compile_rules(self, rules: list[Any]) -> tuple[_CompiledRule, ...]:
        compiled: list[_CompiledRule] = []
        if not rules:
            raise AdapterError("generic-regex requires at least one mapping rule")

        for idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise AdapterError(f"Rule #{idx} must be a mapping")
            pattern_text = rule.get("pattern")
            request_format = rule.get("request_format")
            expects_response = bool(rule.get("expects_response", False))
            response_regex = rule.get("response_regex")
            response_format = rule.get("response_format")

            if not isinstance(pattern_text, str) or not pattern_text:
                raise AdapterError(f"Rule #{idx} missing 'pattern'")
            if not isinstance(request_format, str) or not request_format:
                raise AdapterError(f"Rule #{idx} missing 'request_format'")

            try:
                pattern = re.compile(pattern_text)
            except re.error as exc:
                raise AdapterError(f"Rule #{idx} has invalid pattern: {exc}") from exc

            request_tokens = self._extract_tokens(request_format)
            self._validate_tokens(pattern, request_tokens, idx, "request_format")

            response_pattern_obj: Optional[re.Pattern[str]] = None
            response_tokens: tuple[str, ...] = tuple()
            terminator_val: Optional[str]
            scale_val: Optional[float] = None
            response_scale_val: Optional[float] = None
            group_widths: dict[str, int] = {}
            payload_width: Optional[int] = None

            # optional scaling configuration (apply for reads and writes)
            if "scale" in rule:
                try:
                    scale_val = float(rule.get("scale"))
                except Exception:  # pragma: no cover - defensive
                    raise AdapterError(f"Rule #{idx} has invalid 'scale' value")
            if "response_scale" in rule:
                try:
                    response_scale_val = float(rule.get("response_scale"))
                except Exception:  # pragma: no cover - defensive
                    raise AdapterError(f"Rule #{idx} has invalid 'response_scale' value")
            if "payload_width" in rule:
                try:
                    payload_width = int(rule.get("payload_width"))
                except Exception:
                    raise AdapterError(f"Rule #{idx} has invalid 'payload_width' value")

            if expects_response:
                # terminator may be provided to indicate a device prompt or
                # terminator character (e.g. '>' or '\n'). Default to newline
                # for backward compatibility.
                terminator_val = rule.get("terminator", "\n")
                if terminator_val is not None and not isinstance(terminator_val, str):
                    raise AdapterError(f"Rule #{idx} has invalid 'terminator' value; must be a string")

                # optional scaling configuration
                if "scale" in rule:
                    try:
                        scale_val = float(rule.get("scale"))
                    except Exception as exc:  # pragma: no cover - defensive
                        raise AdapterError(f"Rule #{idx} has invalid 'scale' value") from exc
                if "response_scale" in rule:
                    try:
                        response_scale_val = float(rule.get("response_scale"))
                    except Exception as exc:  # pragma: no cover - defensive
                        raise AdapterError(f"Rule #{idx} has invalid 'response_scale' value") from exc

                if not isinstance(response_regex, str) or not response_regex:
                    raise AdapterError(f"Rule #{idx} expects a response but missing 'response_regex'")
                if not isinstance(response_format, str) or not response_format:
                    raise AdapterError(f"Rule #{idx} expects a response but missing 'response_format'")
                try:
                    response_pattern_obj = re.compile(response_regex)
                except re.error as exc:
                    raise AdapterError(f"Rule #{idx} has invalid response_regex: {exc}") from exc
                response_tokens = self._extract_tokens(response_format)
                self._validate_tokens(response_pattern_obj, response_tokens, idx, "response_format")
                # attempt to infer numeric widths for named groups like (?P<payload>\d{5})
                try:
                    for m in re.finditer(r"\(\?P<(?P<name>\w+)>(?P<pat>[^)]+)\)", response_regex):
                        name = m.group("name")
                        pat = m.group("pat")
                        wmatch = re.match(r"\\d\{(?P<width>\d+)\}", pat)
                        if wmatch:
                            group_widths[name] = int(wmatch.group("width"))
                except Exception:
                    # best-effort only; failure to infer widths is non-fatal
                    group_widths = {}
                # If payload_width not explicitly set, try to infer from named group 'payload'
                if payload_width is None:
                    payload_width = group_widths.get("payload")
            else:
                # If rule does not expect a response, still allow payload_width inference
                if payload_width is None and isinstance(response_regex, str):
                    try:
                        for m in re.finditer(r"\(\?P<(?P<name>\w+)>(?P<pat>[^)]+)\)", response_regex or ""):
                            name = m.group("name")
                            pat = m.group("pat")
                            wmatch = re.match(r"\\d\{(?P<width>\d+)\}", pat)
                            if wmatch and name == "payload":
                                payload_width = int(wmatch.group("width"))
                    except Exception:
                        pass
                if response_regex or response_format:
                    # Allow but warn to help with debugging misconfiguration
                    LOG.debug(
                        "generic_regex rule #%s ignores response fields because expects_response is false",
                        idx,
                    )
                response_pattern_obj = None
                response_format = None
                terminator_val = None

            # default payload width to 5 when a scale is configured and no width was provided
            if payload_width is None and scale_val is not None:
                payload_width = 5

            compiled.append(
                _CompiledRule(
                    pattern_text=pattern_text,
                    pattern=pattern,
                    request_template=request_format,
                    expects_response=expects_response,
                    response_pattern_text=response_regex if isinstance(response_regex, str) else None,
                    response_pattern=response_pattern_obj,
                    response_template=response_format,
                    terminator=terminator_val,
                    request_tokens=request_tokens,
                    response_tokens=response_tokens,
                    scale=scale_val,
                    response_scale=response_scale_val,
                    group_widths=group_widths,
                    payload_width=payload_width,
                )
            )

        return tuple(compiled)

    def _extract_tokens(self, template: str) -> tuple[str, ...]:
        tokens: list[str] = []
        for match in _TOKEN_PATTERN.finditer(template):
            key = match.group(1) or match.group(2)
            tokens.append(key)
        return tuple(tokens)

    def _validate_tokens(
        self,
        pattern: re.Pattern[str],
        tokens: Iterable[str],
        rule_index: int,
        field_name: str,
    ) -> None:
        group_count = pattern.groups
        group_names = set(pattern.groupindex.keys())
        for token in tokens:
            if token.isdigit():
                if int(token) > group_count:
                    raise AdapterError(
                        f"Rule #{rule_index} {field_name} references group ${token} but pattern has only {group_count} group(s)"
                    )
            else:
                if token not in group_names:
                    raise AdapterError(
                        f"Rule #{rule_index} {field_name} references group ${token} but pattern defines no such named group"
                    )

    def _match_rule(self, command: str) -> tuple[_CompiledRule, re.Match[str]]:
        for rule in self._rules:
            match = rule.pattern.match(command)
            if match:
                return rule, match
        raise AdapterError(f"No generic-regex rule matched command: {command!r}")

    def _render_template(
        self,
        template: str,
        match: re.Match[str],
        tokens: Iterable[str],
        rule: Optional[_CompiledRule] = None,
        is_request: bool = True,
    ) -> str:
        def _replacement(m: re.Match[str]) -> str:
            key = m.group(1) or m.group(2)
            try:
                if key.isdigit():
                    value = match.group(int(key))
                else:
                    value = match.group(key)
            except IndexError as exc:
                raise AdapterError(f"Template referenced unknown group ${key}") from exc
            if value is None:
                raise AdapterError(f"Template group ${key} produced no value")
            # apply scaling/formatting for request (writes)
            if is_request and rule is not None and rule.scale is not None:
                # user provided a float-like input which we must scale to an integer
                try:
                    f = float(value)
                except Exception as exc:
                    raise AdapterError(f"Failed to convert template group ${key} value {value!r} to float for scaling") from exc
                scaled = int(round(f * rule.scale))
                # determine width (zero-pad) if available from inferred group widths
                width = None
                if key.isdigit():
                    # use explicit payload_width when present for numeric-index tokens
                    width = rule.payload_width if hasattr(rule, "payload_width") else None
                else:
                    width = rule.group_widths.get(key)
                if width is not None:
                    return f"{scaled:0{width}d}"
                return str(scaled)

            # apply scaling for responses: integer payload -> human float
            if (not is_request) and rule is not None and rule.response_scale is not None:
                # try to extract a numeric portion from the value (handle prefixes like 'C')
                num_match = re.search(r"-?\d+", str(value))
                if not num_match:
                    # nothing numeric to scale; return original
                    return str(value)
                try:
                    intval = int(num_match.group(0))
                except Exception:
                    return str(value)
                scaled_float = intval / rule.response_scale
                # if response_scale is a positive power-of-10 integer, format with fixed decimals
                try:
                    if rule.response_scale > 0:
                        log10 = math.log10(rule.response_scale)
                        if abs(round(log10) - log10) < 1e-9:
                            decimals = int(round(log10))
                        else:
                            decimals = None
                    else:
                        decimals = None
                except Exception:
                    decimals = None
                if decimals is not None:
                    fmt = f"{{:.{decimals}f}}"
                    return fmt.format(scaled_float)
                return str(scaled_float)

            return str(value)

        rendered = _TOKEN_PATTERN.sub(_replacement, template)
        return rendered

    async def _ensure_connected(self) -> None:
        if self._transport == "tcp":
            if self._socket is None:
                await self._open_tcp()
        else:
            if self._serial_handle is None:
                await self._open_serial()

    async def _open_tcp(self) -> None:
        def _connect() -> socket.socket:
            sock = socket.create_connection(
                (self._tcp_host, self._tcp_port),
                timeout=self._connect_timeout,
            )
            sock.settimeout(self._io_timeout)
            LOG.debug("generic_regex._open_tcp: connected to %s:%s", self._tcp_host, self._tcp_port)
            return sock

        try:
            self._socket = await asyncio.to_thread(_connect)
        except Exception as exc:
            raise AdapterError(f"Failed to connect to {self._tcp_host}:{self._tcp_port}: {exc}") from exc

    async def _open_serial(self) -> None:
        serial_mod = serial
        if serial_mod is None:
            raise AdapterError("pyserial is required for generic-regex serial transport")

        def _connect() -> Any:
            return serial_mod.Serial(
                port=self._serial_port,
                **self._serial_settings,
            )

        try:
            self._serial_handle = await asyncio.to_thread(_connect)
        except Exception as exc:
            raise AdapterError(f"Failed to open serial port {self._serial_port}: {exc}") from exc

    def _close_transport(self) -> None:
        if self._transport == "tcp":
            sock = self._socket
            self._socket = None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        else:
            handle = self._serial_handle
            self._serial_handle = None
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass

    async def _send(self, payload: bytes) -> None:
        if self._transport == "tcp":
            sock = self._socket
            if sock is None:
                raise AdapterError("TCP socket is not connected")
            def _do_send() -> None:
                try:
                    sock.sendall(payload)
                    try:
                        LOG.debug("generic_regex._send: tcp sent %d bytes payload=%r", len(payload), payload.decode(self._encoding))
                    except Exception:
                        LOG.debug("generic_regex._send: tcp sent %d bytes (binary)", len(payload))
                except socket.timeout as exc:
                    raise asyncio.TimeoutError("Timed out while sending request") from exc
                except OSError as exc:
                    raise AdapterError(f"TCP send failed: {exc}") from exc

            await asyncio.to_thread(_do_send)
        else:
            handle = self._serial_handle
            if handle is None:
                raise AdapterError("Serial port is not connected")
            def _do_send() -> None:
                try:
                    handle.write(payload)
                    if hasattr(handle, "flush"):
                        handle.flush()
                    try:
                        LOG.debug("generic_regex._send: serial sent %d bytes payload=%r", len(payload), payload.decode(self._encoding))
                    except Exception:
                        LOG.debug("generic_regex._send: serial sent %d bytes (binary)", len(payload))
                except Exception as exc:
                    serial_mod = serial
                    if serial_mod is not None and hasattr(serial_mod, "SerialTimeoutException"):
                        timeout_exc = serial_mod.SerialTimeoutException  # type: ignore[attr-defined]
                        if isinstance(exc, timeout_exc):
                            raise asyncio.TimeoutError("Timed out while sending request") from exc
                    raise AdapterError(f"Serial write failed: {exc}") from exc

            await asyncio.to_thread(_do_send)

    async def _receive_chunk(self) -> bytes:
        if self._transport == "tcp":
            sock = self._socket
            if sock is None:
                raise AdapterError("TCP socket is not connected")

            def _do_recv() -> bytes:
                try:
                    LOG.debug("generic_regex._receive_chunk: tcp recv requesting %d bytes", self._recv_chunk_size)
                    data = sock.recv(self._recv_chunk_size)
                    LOG.debug("generic_regex._receive_chunk: tcp recv got %d bytes", len(data) if data is not None else -1)
                    return data
                except socket.timeout as exc:
                    raise asyncio.TimeoutError("Timed out while waiting for response") from exc
                except OSError as exc:
                    raise AdapterError(f"TCP receive failed: {exc}") from exc

            chunk = await asyncio.to_thread(_do_recv)
            if chunk == b"":
                raise AdapterError("TCP connection closed by peer")
            return chunk
        else:
            handle = self._serial_handle
            if handle is None:
                raise AdapterError("Serial port is not connected")

            def _do_recv() -> bytes:
                try:
                    data = handle.read(self._recv_chunk_size)
                    LOG.debug("generic_regex._receive_chunk: serial read got %s bytes", len(data) if data is not None else -1)
                except Exception as exc:
                    serial_mod = serial
                    if serial_mod is not None and hasattr(serial_mod, "SerialTimeoutException"):
                        timeout_exc = serial_mod.SerialTimeoutException  # type: ignore[attr-defined]
                        if isinstance(exc, timeout_exc):
                            raise asyncio.TimeoutError("Timed out while waiting for response") from exc
                    raise AdapterError(f"Serial read failed: {exc}") from exc
                if not data:
                    raise asyncio.TimeoutError("Serial read timeout")
                return data

            return await asyncio.to_thread(_do_recv)

    async def _receive_response(self, rule: _CompiledRule) -> tuple[str, re.Match[str]]:
        buffer = bytearray()
        while len(buffer) < self._max_response_bytes:
            try:
                chunk = await self._receive_chunk()
            except asyncio.TimeoutError:
                LOG.debug("generic_regex._receive_response: timeout while reading, buffer_len=%d", len(buffer))
                if buffer:
                    break
                raise
            buffer.extend(chunk)
            try:
                text = buffer.decode(self._encoding, errors="strict")
            except UnicodeDecodeError as exc:
                raise AdapterError(f"Response decoding failed: {exc}") from exc

            # If a terminator is configured, wait until we observe it before
            # attempting to fullmatch the response. This supports devices that
            # use prompts (e.g. '>') rather than a newline. If no terminator is
            # configured, fall back to the previous behaviour of stripping
            # trailing CR/LF and trying a fullmatch on the available text.
            text_raw = text
            terminator = rule.terminator
            if terminator:
                if terminator in text_raw:
                    # Use the payload up to the first terminator occurrence
                    idx = text_raw.find(terminator)
                    candidate = text_raw[:idx].rstrip("\r\n")
                    LOG.debug("generic_regex._receive_response: terminator found, candidate=%r", candidate)
                    assert rule.response_pattern is not None  # for mypy
                    match = rule.response_pattern.fullmatch(candidate)
                    if match:
                        LOG.debug("generic_regex._receive_response: regex matched")
                        return candidate, match
                    # If we saw a terminator but the payload didn't match the
                    # expected regex, fail fast to surface misconfiguration
                    raise AdapterError(
                        "Response did not match expected pattern "
                        f"{rule.response_pattern_text!r} after terminator-terminated read"
                    )
                # Terminator not yet observed -- continue reading until it is
                LOG.debug("generic_regex._receive_response: terminator %r not yet seen, buffer_len=%d", terminator, len(buffer))
                continue
            else:
                candidate = text_raw.rstrip("\r\n")
                LOG.debug("generic_regex._receive_response: candidate=%r", candidate)
                assert rule.response_pattern is not None  # for mypy
                match = rule.response_pattern.fullmatch(candidate)
                if match:
                    LOG.debug("generic_regex._receive_response: regex matched")
                    return candidate, match

        raise AdapterError(
            "Response did not match expected pattern "
            f"{rule.response_pattern_text!r} after reading {len(buffer)} byte(s)"
        )
