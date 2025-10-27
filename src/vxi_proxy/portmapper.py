"""Minimal ONC RPC Portmapper (rpcbind) to serve VXI-11 GETPORT.

This is a very small user-space implementation that listens on UDP and TCP
port 111 and implements just enough of the portmapper v2 protocol to answer
PMAPPROC_NULL (0) and PMAPPROC_GETPORT (3) for the VXI-11 program numbers.

- It returns a configured TCP port for the following VXI-11 programs:
  * 0x0607AF (395183) - DEVICE_CORE
  * 0x0607B0 (395184) - DEVICE_ASYNC
  * 0x0607B1 (395185) - DEVICE_INTR
- For other programs or protocols (e.g., UDP requests for GETPORT), it returns 0.

This is intentionally minimal and not a full rpcbind replacement.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import Optional
from pathlib import Path

from xdrlib import Packer, Unpacker

_LOG = logging.getLogger(__name__)

# ONC RPC constants
MSG_CALL = 0
MSG_REPLY = 1
REPLY_MSG_ACCEPTED = 0

AUTH_NULL = 0

ACCEPTSTAT_SUCCESS = 0

PMAP_PROG = 100000
PMAP_VERS = 2
PMAPPROC_NULL = 0
PMAPPROC_GETPORT = 3

IPPROTO_TCP = 6
IPPROTO_UDP = 17

# VXI-11 program numbers
VXI11_DEVICE_CORE = 0x0607AF  # 395183
VXI11_DEVICE_ASYNC = 0x0607B0  # 395184
VXI11_DEVICE_INTR = 0x0607B1  # 395185
VXI11_PROGRAMS = {VXI11_DEVICE_CORE, VXI11_DEVICE_ASYNC, VXI11_DEVICE_INTR}
VXI11_PROGRAMS_TCP = {VXI11_DEVICE_CORE, VXI11_DEVICE_ASYNC}  # INTR returns 0 (unsupported)


def _pack_reply_header(xid: int, packer: Packer) -> None:
    packer.pack_uint(xid)
    packer.pack_uint(MSG_REPLY)
    packer.pack_uint(REPLY_MSG_ACCEPTED)
    # Verifier: AUTH_NULL
    packer.pack_uint(AUTH_NULL)
    packer.pack_uint(0)  # length
    # Accept status: SUCCESS
    packer.pack_uint(ACCEPTSTAT_SUCCESS)


def _build_null_reply(xid: int) -> bytes:
    p = Packer()
    _pack_reply_header(xid, p)
    # no body for void result
    return p.get_buffer()


def _build_getport_reply(xid: int, port: int) -> bytes:
    p = Packer()
    _pack_reply_header(xid, p)
    p.pack_uint(port)
    return p.get_buffer()


def _read_rpc_call(data: bytes):
    up = Unpacker(data)
    xid = up.unpack_uint()
    msg_type = up.unpack_uint()
    if msg_type != MSG_CALL:
        raise ValueError("Not an RPC CALL message")
    rpc_vers = up.unpack_uint()
    if rpc_vers != 2:
        raise ValueError("Unsupported RPC version")
    prog = up.unpack_uint()
    vers = up.unpack_uint()
    proc = up.unpack_uint()
    # Credentials
    _cred_flavor = up.unpack_uint()
    cred_len = up.unpack_uint()
    up.unpack_fopaque(cred_len)
    # Verifier
    _verf_flavor = up.unpack_uint()
    verf_len = up.unpack_uint()
    up.unpack_fopaque(verf_len)
    return xid, prog, vers, proc, up


class PortMapperServer:
    """Very small portmapper serving only GETPORT for VXI-11 programs."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 111,
        vxi_port: Optional[int] = None,
        enable_udp: bool = True,
        enable_tcp: bool = True,
        config_path: Optional[Path] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._vxi_port = self._resolve_vxi_port(vxi_port, config_path)
        self._udp = enable_udp
        self._tcp = enable_tcp
        self._udp_sock: Optional[socket.socket] = None
        self._tcp_sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        _LOG.info("Portmapper will report VXI-11 TCP port %d", self._vxi_port)

    @staticmethod
    def _resolve_vxi_port(vxi_port: Optional[int], config_path: Optional[Path]) -> int:
        # Explicit value wins
        if vxi_port is not None:
            try:
                return int(vxi_port)
            except Exception:
                return 1024
        # Try provided config path, then common defaults
        candidates = []
        if isinstance(config_path, Path):
            candidates.append(config_path)
        candidates.extend([Path("/app/config.yaml"), Path("config.yaml")])
        for p in candidates:
            try:
                if not p.exists():
                    continue
                # Lazy import to avoid heavy dependency at module import time
                from vxi_proxy.config import load_config  # type: ignore

                cfg = load_config(p)
                port = int(getattr(cfg.server, "port", 0) or 0)
                if port > 0:
                    return port
            except Exception:
                continue
        # Default if config missing/invalid
        return 1024

    # -----------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------
    def start(self) -> None:
        if self._udp:
            try:
                us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                us.bind((self._host, self._port))
                self._udp_sock = us
                t = threading.Thread(target=self._udp_loop, daemon=True)
                t.start()
                self._threads.append(t)
                _LOG.info("Portmapper UDP listening on %s:%d", self._host, self._port)
            except OSError as exc:
                _LOG.warning("Portmapper UDP bind failed: %s", exc)
        if self._tcp:
            try:
                ts = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ts.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                ts.bind((self._host, self._port))
                ts.listen(16)
                self._tcp_sock = ts
                t = threading.Thread(target=self._tcp_loop, daemon=True)
                t.start()
                self._threads.append(t)
                _LOG.info("Portmapper TCP listening on %s:%d", self._host, self._port)
            except OSError as exc:
                _LOG.warning("Portmapper TCP bind failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._udp_sock is not None:
                self._udp_sock.close()
        finally:
            if self._tcp_sock is not None:
                try:
                    self._tcp_sock.close()
                except Exception:
                    pass
        for t in self._threads:
            t.join(timeout=1.0)

    # -----------------------------------------------------
    # UDP handling
    # -----------------------------------------------------
    def _udp_loop(self) -> None:
        assert self._udp_sock is not None
        sock = self._udp_sock
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except OSError:
                break
            try:
                reply = self._handle_call(data)
                if reply is not None:
                    sock.sendto(reply, addr)
            except Exception as exc:
                _LOG.debug("UDP handling error: %s", exc)

    # -----------------------------------------------------
    # TCP handling
    # -----------------------------------------------------
    def _tcp_loop(self) -> None:
        assert self._tcp_sock is not None
        lsock = self._tcp_sock
        while not self._stop.is_set():
            try:
                conn, _addr = lsock.accept()
            except OSError:
                break
            t = threading.Thread(target=self._tcp_client, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _tcp_client(self, conn: socket.socket) -> None:
        with conn:
            try:
                data = self._read_rpc_record(conn)
                if not data:
                    return
                reply = self._handle_call(data)
                if reply is None:
                    return
                self._write_rpc_record(conn, reply)
            except Exception as exc:
                _LOG.debug("TCP client error: %s", exc)

    @staticmethod
    def _read_rpc_record(conn: socket.socket) -> bytes:
        # Read 4-byte record marker
        hdr = conn.recv(4)
        if len(hdr) < 4:
            return b""
        (rm,) = struct.unpack("!I", hdr)
        last = bool(rm & 0x80000000)
        n = rm & 0x7FFFFFFF
        chunks = []
        to_read = n
        while to_read > 0:
            chunk = conn.recv(to_read)
            if not chunk:
                break
            chunks.append(chunk)
            to_read -= len(chunk)
        data = b"".join(chunks)
        # We assume single-fragment messages for simplicity (last==1)
        return data if last else data

    @staticmethod
    def _write_rpc_record(conn: socket.socket, payload: bytes) -> None:
        n = len(payload)
        rm = 0x80000000 | n  # last fragment flag + length
        conn.sendall(struct.pack("!I", rm) + payload)

    # -----------------------------------------------------
    # Core handler
    # -----------------------------------------------------
    def _handle_call(self, data: bytes) -> Optional[bytes]:
        xid, prog, vers, proc, up = _read_rpc_call(data)
        if prog != PMAP_PROG or vers != PMAP_VERS:
            # Not a portmap call we handle; ignore silently
            return None
        if proc == PMAPPROC_NULL:
            return _build_null_reply(xid)
        if proc == PMAPPROC_GETPORT:
            # mapping: prog, vers, prot, port
            m_prog = up.unpack_uint()
            # skip vers
            up.unpack_uint()
            m_prot = up.unpack_uint()
            _m_port = up.unpack_uint()
            # Only CORE and ASYNC return a TCP port; INTR is not supported, return 0
            if m_prog in VXI11_PROGRAMS_TCP and m_prot == IPPROTO_TCP:
                return _build_getport_reply(xid, int(self._vxi_port))
            return _build_getport_reply(xid, 0)
        # Unhandled procedure: success with default result (void)
        return _build_null_reply(xid)
