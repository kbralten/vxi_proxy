"""Interactive VXI-11 terminal for manual testing and debugging."""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass
from typing import List, Optional

from vxi11 import vxi11 as vxi11_proto


class TerminalError(RuntimeError):
    pass


class CommandError(RuntimeError):
    pass


@dataclass
class CommandResult:
    lines: List[str]
    exit: bool = False


@dataclass
class ConnectionInfo:
    host: str
    port: int
    device: str
    client_id: int
    link_id: int
    max_recv: int


class _CommandParser(argparse.ArgumentParser):
    def __init__(self, prog: str) -> None:
        super().__init__(prog=prog, add_help=False)

    def error(self, message: str) -> None:  # type: ignore[override]
        raise CommandError(message)

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:  # type: ignore[override]
        if message:
            raise CommandError(message)
        raise CommandError()


class Vxi11Terminal:
    def __init__(
        self,
        *,
        auto_read: bool = True,
        append_newline: bool = True,
        read_size: int = 4096,
        io_timeout: float = 5.0,
    ) -> None:
        self._auto_read = auto_read
        self._append_newline = append_newline
        self._read_size = max(1, read_size)
        self._io_timeout_ms = max(0, int(io_timeout * 1000))
        headroom = max(0.1, io_timeout * 0.1)
        self._socket_timeout = max(0.1, io_timeout + headroom)
        self._client: Optional[vxi11_proto.CoreClient] = None
        self._connection: Optional[ConnectionInfo] = None
        self._has_lock = False
        self._next_client_id = 0x4000

    @property
    def prompt(self) -> str:
        if self._connection is None:
            return "vxi> "
        info = self._connection
        suffix = "*" if self._has_lock else ""
        return f"vxi({info.device}@{info.host}:{info.port}{suffix})> "

    def close(self) -> None:
        if self._client is not None:
            try:
                if self._connection is not None:
                    try:
                        self._client.destroy_link(self._connection.link_id)
                    except Exception:
                        pass
            finally:
                try:
                    self._client.close()
                finally:
                    self._client = None
        self._connection = None
        self._has_lock = False

    def execute(self, line: str) -> CommandResult:
        line = line.strip()
        if not line:
            return CommandResult([])
        tokens = shlex.split(line)
        if not tokens:
            return CommandResult([])
        command = tokens[0].lower()
        args = tokens[1:]
        if command in {"quit", "exit"}:
            return CommandResult(["Bye."], exit=True)
        handler = {
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "lock": self._cmd_lock,
            "unlock": self._cmd_unlock,
            "read": self._cmd_read,
            "status": self._cmd_status,
            "help": self._cmd_help,
            "?": self._cmd_help,
        }.get(command)
        if handler is not None:
            try:
                return CommandResult(handler(args))
            except CommandError as exc:
                return CommandResult([f"Command error: {exc}"])
            except TerminalError as exc:
                return CommandResult([str(exc)])
        return CommandResult(self._send_and_receive(line))

    def _cmd_help(self, _: List[str]) -> List[str]:
        return [
            "Commands:",
            "  connect <host> <device> [--port N] [--client-id N] [--lock] [--lock-timeout MS]",
            "  disconnect",
            "  lock [--timeout MS]",
            "  unlock",
            "  read [--size N]",
            "  status",
            "  help",
            "  ?",
            "  quit",
            "Plain text lines are sent via device_write followed by device_read.",
        ]

    def _cmd_connect(self, args: List[str]) -> List[str]:
        parser = _CommandParser("connect")
        parser.add_argument("host")
        parser.add_argument("device")
        parser.add_argument("--port", type=int, default=None)
        parser.add_argument("--client-id", type=int, default=None)
        parser.add_argument("--lock", action="store_true")
        parser.add_argument("--lock-timeout", type=int, default=1000)
        parsed = parser.parse_args(args)
        client_id = parsed.client_id if parsed.client_id is not None else self._generate_client_id()
        self._connect(
            host=parsed.host,
            port=parsed.port,
            device=parsed.device,
            client_id=client_id,
            request_lock=parsed.lock,
            lock_timeout=parsed.lock_timeout,
        )
        info = self._connection
        if info is None:
            raise TerminalError("Connection failed")
        status = f"Connected to {info.device} at {info.host}:{info.port} (lid={info.link_id}, max_recv={info.max_recv})"
        if self._has_lock:
            status += " with lock"
        return [status]

    def _cmd_disconnect(self, _: List[str]) -> List[str]:
        if self._connection is None:
            return ["Not connected."]
        self.close()
        return ["Disconnected."]

    def _cmd_lock(self, args: List[str]) -> List[str]:
        self._require_connection()
        parser = _CommandParser("lock")
        parser.add_argument("--timeout", type=int, default=1000)
        parsed = parser.parse_args(args)
        assert self._client is not None
        assert self._connection is not None
        err = self._client.device_lock(self._connection.link_id, 0, parsed.timeout)
        if err != vxi11_proto.ERR_NO_ERROR:
            return [self._format_error("device_lock", err)]
        self._has_lock = True
        return ["Lock acquired."]

    def _cmd_unlock(self, _: List[str]) -> List[str]:
        self._require_connection()
        assert self._client is not None
        assert self._connection is not None
        err = self._client.device_unlock(self._connection.link_id)
        if err != vxi11_proto.ERR_NO_ERROR:
            return [self._format_error("device_unlock", err)]
        self._has_lock = False
        return ["Lock released."]

    def _cmd_read(self, args: List[str]) -> List[str]:
        self._require_connection()
        parser = _CommandParser("read")
        parser.add_argument("--size", type=int, default=self._read_size)
        parsed = parser.parse_args(args)
        assert self._client is not None
        assert self._connection is not None
        err, reason, data = self._client.device_read(  # type: ignore[assignment]
            self._connection.link_id,
            max(1, parsed.size),
            self._io_timeout_ms,
            0,
            0,
            0,
        )
        if err != vxi11_proto.ERR_NO_ERROR:
            return [self._format_error("device_read", err)]
        return [self._format_read(reason, data)]

    def _cmd_status(self, _: List[str]) -> List[str]:
        if self._connection is None:
            return ["Not connected."]
        info = self._connection
        lock_state = "held" if self._has_lock else "available"
        return [
            f"Host: {info.host}:{info.port}",
            f"Device: {info.device}",
            f"Link: {info.link_id} (client_id={info.client_id}, max_recv={info.max_recv})",
            f"Lock: {lock_state}",
        ]

    def _send_and_receive(self, payload: str) -> List[str]:
        self._require_connection()
        assert self._client is not None
        assert self._connection is not None
        data = payload.encode("utf-8", errors="replace")
        if self._append_newline and not data.endswith(b"\n"):
            data += b"\n"
        err, count = self._client.device_write(  # type: ignore[assignment]
            self._connection.link_id,
            self._io_timeout_ms,
            0,
            0,
            data,
        )
        lines = [f"[write] {count} bytes"]
        if err != vxi11_proto.ERR_NO_ERROR:
            lines.append(self._format_error("device_write", err))
            return lines
        if not self._auto_read:
            return lines
        err, reason, response = self._client.device_read(
            self._connection.link_id,
            self._read_size,
            self._io_timeout_ms,
            0,
            0,
            0,
        )
        if err != vxi11_proto.ERR_NO_ERROR:
            lines.append(self._format_error("device_read", err))
        else:
            lines.append(self._format_read(reason, response))
        return lines

    def _connect(
        self,
        *,
        host: str,
        port: Optional[int],
        device: str,
        client_id: int,
        request_lock: bool,
        lock_timeout: int,
    ) -> None:
        self.close()
        port_value = port if port is not None else 0
        try:
            client = vxi11_proto.CoreClient(host, port=port_value)
        except Exception as exc:
            raise TerminalError(f"Failed to contact {host}:{port_value}: {exc}") from exc
        client.sock.settimeout(self._socket_timeout)
        device_bytes = device.encode("utf-8")
        try:
            err, link_id, _, max_recv = client.create_link(  # type: ignore[assignment]
                client_id,
                request_lock,
                max(0, lock_timeout),
                device_bytes,
            )
        except Exception as exc:
            client.close()
            raise TerminalError(f"create_link failed: {exc}") from exc
        if err != vxi11_proto.ERR_NO_ERROR:
            client.close()
            raise TerminalError(self._format_error("create_link", err))
        self._client = client
        self._connection = ConnectionInfo(
            host=host,
            port=client.port,
            device=device,
            client_id=client_id,
            link_id=link_id,
            max_recv=max_recv,
        )
        self._has_lock = request_lock

    def _require_connection(self) -> None:
        if self._connection is None or self._client is None:
            raise TerminalError("Not connected. Use 'connect <host> <device>' first.")

    def _format_error(self, operation: str, code: int) -> str:
        label = vxi11_proto.Vxi11Exception.em.get(code, "Unknown error")
        return f"[{operation}] error {code}: {label}"

    def _format_read(self, reason: int, payload: bytes) -> str:
        flags: List[str] = []
        if reason & vxi11_proto.RX_END:
            flags.append("END")
        if reason & vxi11_proto.RX_CHR:
            flags.append("CHR")
        if reason & vxi11_proto.RX_REQCNT:
            flags.append("REQCNT")
        if not flags:
            flags.append("0")
        try:
            text = payload.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError:
            text = payload.hex(" ")
        return f"[read] {len(payload)} bytes ({'|'.join(flags)}): {text}"

    def _generate_client_id(self) -> int:
        self._next_client_id += 1
        return self._next_client_id


def _build_terminal_from_args(ns: argparse.Namespace) -> Vxi11Terminal:
    return Vxi11Terminal(
        auto_read=not ns.no_auto_read,
        append_newline=not ns.no_newline,
        read_size=ns.read_size,
        io_timeout=ns.timeout,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive VXI-11 terminal")
    parser.add_argument("--host")
    parser.add_argument("--device")
    parser.add_argument("--port", type=int)
    parser.add_argument("--client-id", type=int)
    parser.add_argument("--lock", action="store_true")
    parser.add_argument("--lock-timeout", type=int, default=1000)
    parser.add_argument("--read-size", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--no-auto-read", action="store_true")
    parser.add_argument("--no-newline", action="store_true")
    ns = parser.parse_args(argv)

    terminal = _build_terminal_from_args(ns)
    try:
        if ns.host and ns.device:
            try:
                client_id = ns.client_id if ns.client_id is not None else terminal._generate_client_id()
                terminal._connect(
                    host=ns.host,
                    port=ns.port,
                    device=ns.device,
                    client_id=client_id,
                    request_lock=ns.lock,
                    lock_timeout=ns.lock_timeout,
                )
                for line in terminal._cmd_status([]):
                    print(line)
            except TerminalError as exc:
                print(exc)
        while True:
            try:
                line = input(terminal.prompt)
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                break
            result = terminal.execute(line)
            for out_line in result.lines:
                print(out_line)
            if result.exit:
                break
        return 0
    finally:
        terminal.close()


if __name__ == "__main__":
    sys.exit(main())