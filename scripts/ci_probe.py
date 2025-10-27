"""CI probe to validate the minimal portmapper inside the container.

This script is intended to be executed inside the running container (via
`docker exec vxi-ci python /app/scripts/ci_probe.py`) and will:
- load /app/config.yaml to find server.port
- send a UDP PMAPPROC_GETPORT for VXI-11 CORE/ASYNC/INTR and validate
- send a TCP PMAPPROC_GETPORT (record-marked) for CORE/ASYNC/INTR and validate

Exit code 0 = success, non-zero = failure.
"""
from __future__ import annotations

from xdrlib import Packer, Unpacker
from pathlib import Path
import socket
import random
import struct
import sys

try:
    from vxi_proxy.config import load_config
except Exception as exc:
    print(f"import error: {exc}")
    sys.exit(1)

PMAP_PROG = 100000
PMAP_VERS = 2
PMAPPROC_GETPORT = 3
MSG_CALL = 0
AUTH_NULL = 0
IPPROTO_TCP = 6
VXI11_DEVICE_CORE = 0x0607AF
VXI11_DEVICE_ASYNC = 0x0607B0
VXI11_DEVICE_INTR = 0x0607B1


def get_expected_port() -> int:
    try:
        cfg = load_config(Path('/app/config.yaml'))
        return int(getattr(cfg.server, 'port', 0) or 0)
    except Exception as exc:
        print(f"failed to read config: {exc}")
        return 0


def build_getport_call(xid: int, prog: int) -> bytes:
    p = Packer()
    p.pack_uint(xid)
    p.pack_uint(MSG_CALL)
    p.pack_uint(2)  # RPC version
    p.pack_uint(PMAP_PROG)
    p.pack_uint(PMAP_VERS)
    p.pack_uint(PMAPPROC_GETPORT)
    # cred/verf: AUTH_NULL
    p.pack_uint(AUTH_NULL)
    p.pack_uint(0)
    p.pack_uint(AUTH_NULL)
    p.pack_uint(0)
    # mapping args: prog, vers, prot, port
    p.pack_uint(prog)
    p.pack_uint(1)
    p.pack_uint(IPPROTO_TCP)
    p.pack_uint(0)
    return p.get_buffer()


def udp_getport(host: str, port: int, prog: int) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2.0)
    xid = random.randint(1, 2**31 - 1)
    s.sendto(build_getport_call(xid, prog), (host, port))
    data, _ = s.recvfrom(4096)
    up = Unpacker(data)
    up.unpack_uint()
    _ = up.unpack_uint()
    _ = up.unpack_uint()  # reply, accepted
    _ = up.unpack_uint()
    ln = up.unpack_uint()
    up.unpack_fopaque(ln)
    _ = up.unpack_uint()  # acceptstat
    port = up.unpack_uint()
    return port


def tcp_getport(host: str, port: int, prog: int) -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    s.connect((host, port))
    xid = random.randint(1, 2**31 - 1)
    payload = build_getport_call(xid, prog)
    rm = 0x80000000 | len(payload)
    s.sendall(struct.pack('!I', rm) + payload)
    # read record marker
    hdr = s.recv(4)
    if len(hdr) < 4:
        return 0
    (rmr,) = struct.unpack('!I', hdr)
    n = rmr & 0x7FFFFFFF
    chunks = []
    to_read = n
    while to_read > 0:
        chunk = s.recv(to_read)
        if not chunk:
            break
        chunks.append(chunk)
        to_read -= len(chunk)
    data = b''.join(chunks)
    up = Unpacker(data)
    up.unpack_uint()
    _ = up.unpack_uint()
    _ = up.unpack_uint()  # reply, accepted
    _ = up.unpack_uint()
    ln = up.unpack_uint()
    up.unpack_fopaque(ln)
    _ = up.unpack_uint()  # acceptstat
    port = up.unpack_uint()
    return port


def main() -> int:
    expected = get_expected_port()
    if expected == 0:
        print("Expected port is 0 in config, failing")
        return 1

    # UDP checks
    got = udp_getport('127.0.0.1', 111, VXI11_DEVICE_CORE)
    print(f'UDP CORE GETPORT returned {got}, expected {expected}')
    if got != expected:
        print('UDP CORE mismatch')
        return 2
    got = udp_getport('127.0.0.1', 111, VXI11_DEVICE_ASYNC)
    print(f'UDP ASYNC GETPORT returned {got}, expected {expected}')
    if got != expected:
        print('UDP ASYNC mismatch')
        return 3
    got = udp_getport('127.0.0.1', 111, VXI11_DEVICE_INTR)
    print(f'UDP INTR GETPORT returned {got}, expected 0')
    if got != 0:
        print('UDP INTR expected 0')
        return 4

    # TCP checks
    got = tcp_getport('127.0.0.1', 111, VXI11_DEVICE_CORE)
    print(f'TCP CORE GETPORT returned {got}, expected {expected}')
    if got != expected:
        print('TCP CORE mismatch')
        return 5
    got = tcp_getport('127.0.0.1', 111, VXI11_DEVICE_ASYNC)
    print(f'TCP ASYNC GETPORT returned {got}, expected {expected}')
    if got != expected:
        print('TCP ASYNC mismatch')
        return 6
    got = tcp_getport('127.0.0.1', 111, VXI11_DEVICE_INTR)
    print(f'TCP INTR GETPORT returned {got}, expected 0')
    if got != 0:
        print('TCP INTR expected 0')
        return 7

    print('Portmapper probes OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
