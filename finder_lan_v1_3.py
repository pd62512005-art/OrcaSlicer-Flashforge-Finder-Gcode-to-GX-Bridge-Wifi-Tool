#!/usr/bin/env python3
"""FlashForge Finder LAN client for the legacy TCP protocol on port 8899.

No third-party packages are required.

Examples:
  python finder_lan.py info 192.168.40.180
  python finder_lan.py status 192.168.40.180
  python finder_lan.py upload 192.168.40.180 model.gx
  python finder_lan.py upload 192.168.40.180 model.gx --start
  python finder_lan.py pause 192.168.40.180
  python finder_lan.py resume 192.168.40.180
  python finder_lan.py cancel 192.168.40.180
  python finder_lan.py command 192.168.40.180 "M105"
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

VERSION = "1.3.0"
DEFAULT_PORT = 8899
FINALIZE_DELAY = 0.5
BLOCK_SIZE = 4096
MAGIC = b"\x5a\x5a\xa5\xa5"


class FinderProtocolError(RuntimeError):
    pass


class FinderClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 10.0, finalize_delay: float = FINALIZE_DELAY):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.finalize_delay = finalize_delay
        self.sock: socket.socket | None = None
        self.rx = bytearray()

    def __enter__(self) -> "FinderClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock.settimeout(self.timeout)
        response = self.command("M601 S1")
        if b"Control Success" not in response:
            raise FinderProtocolError(
                "Printer answered M601 but did not grant control:\n"
                + response.decode("utf-8", "replace")
            )

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.command("M602", timeout=2.0)
        except (OSError, FinderProtocolError):
            # M602 may close the socket before the complete reply is read.
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def _sendall(self, data: bytes) -> None:
        if self.sock is None:
            raise FinderProtocolError("Not connected")
        self.sock.sendall(data)

    def _recv_until(self, marker: bytes, timeout: float | None = None) -> bytes:
        if self.sock is None:
            raise FinderProtocolError("Not connected")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while marker not in self.rx:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                preview = bytes(self.rx[-500:]).decode("utf-8", "replace")
                raise FinderProtocolError(
                    f"Timed out waiting for {marker!r}. Last response bytes:\n{preview}"
                )
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(8192)
            if not chunk:
                preview = bytes(self.rx[-500:]).decode("utf-8", "replace")
                raise FinderProtocolError(
                    f"Printer closed the connection while waiting for {marker!r}. "
                    f"Last response bytes:\n{preview}"
                )
            self.rx.extend(chunk)

        end = self.rx.index(marker) + len(marker)
        result = bytes(self.rx[:end])
        del self.rx[:end]
        return result

    def command(self, command: str, timeout: float | None = None) -> bytes:
        command = command.strip()
        if command.startswith("~"):
            command = command[1:]
        payload = f"~{command}\r\n".encode("ascii")
        self._sendall(payload)
        response = self._recv_until(b"ok\r\n", timeout)
        if b"Control failed" in response or b"Error" in response:
            raise FinderProtocolError(response.decode("utf-8", "replace"))
        return response.lstrip(b"\x00")

    @staticmethod
    def make_frame(counter: int, chunk: bytes) -> bytes:
        if len(chunk) > BLOCK_SIZE:
            raise ValueError("Chunk is larger than 4096 bytes")
        crc = zlib.crc32(chunk) & 0xFFFFFFFF
        header = MAGIC + struct.pack(">III", counter, len(chunk), crc)
        return header + chunk + (b"\x00" * (BLOCK_SIZE - len(chunk)))

    def upload(self, local_file: Path, remote_name: str | None = None) -> str:
        local_file = local_file.resolve()
        if not local_file.is_file():
            raise FileNotFoundError(local_file)

        size = local_file.stat().st_size
        if size <= 0:
            raise FinderProtocolError("Refusing to upload an empty file")

        name = remote_name or local_file.name
        name = Path(name).name
        # The legacy firmware is safest with simple ASCII filenames.
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name:
            raise FinderProtocolError("Remote filename became empty after sanitizing")
        remote_path = f"0:/user/{name}"

        response = self.command(f"M28 {size} {remote_path}", timeout=15.0)
        if b"Writing to file:" not in response:
            raise FinderProtocolError(
                "Printer did not enter file-write mode:\n"
                + response.decode("utf-8", "replace")
            )

        sent = 0
        counter = 0
        with local_file.open("rb") as fh:
            while True:
                chunk = fh.read(BLOCK_SIZE)
                if not chunk:
                    break
                self._sendall(self.make_frame(counter, chunk))
                expected = f"N000{counter} ok.\r\n".encode("ascii")
                response = self._recv_until(expected, timeout=30.0)
                if expected not in response:
                    raise FinderProtocolError(
                        f"Bad acknowledgement for block {counter}: {response!r}"
                    )
                sent += len(chunk)
                counter += 1
                percent = sent * 100.0 / size
                print(f"\rUploaded {sent:,}/{size:,} bytes ({percent:5.1f}%)", end="", flush=True)

        print()
        # FlashPrint waits about 0.5 s after the final block acknowledgement.
        # The Finder test capture shows the same delay before M29.
        if self.finalize_delay > 0:
            time.sleep(self.finalize_delay)
        response = self.command("M29", timeout=15.0)
        if b"Done saving file." not in response:
            raise FinderProtocolError(
                "Printer did not confirm file save:\n"
                + response.decode("utf-8", "replace")
            )
        return remote_path

    def start_print(self, remote_path: str) -> bytes:
        response = self.command(f"M23 {remote_path}", timeout=15.0)
        if b"File selected" not in response:
            raise FinderProtocolError(
                "Printer did not confirm print start:\n"
                + response.decode("utf-8", "replace")
            )
        return response


def print_response(data: bytes) -> None:
    # Strip the protocol's occasional NUL prefix before display.
    print(data.replace(b"\x00", b"").decode("utf-8", "replace").rstrip())


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control or upload .gx files to a FlashForge Finder over LAN")
    parser.add_argument("--version", action="version", version=f"finder_lan {VERSION}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default: 8899)")
    parser.add_argument("--timeout", type=float, default=10.0, help="socket timeout in seconds")
    parser.add_argument("--finalize-delay", type=float, default=FINALIZE_DELAY, help="seconds to wait before M29 after upload (default: 0.5)")

    sub = parser.add_subparsers(dest="action", required=True)

    for name in ("info", "status", "pause", "resume", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("host")

    p = sub.add_parser("command", help="send one raw FlashForge command")
    p.add_argument("host")
    p.add_argument("gcode", help='example: "M105"')

    p = sub.add_parser("upload", help="upload a .gx file")
    p.add_argument("host")
    p.add_argument("file", type=Path)
    p.add_argument("--remote-name", help="filename stored under 0:/user/")
    p.add_argument("--start", action="store_true", help="start printing after upload")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)

    try:
        print(f"finder_lan {VERSION}")
        print(f"Connecting to {args.host}:{args.port} ...", flush=True)
        with FinderClient(args.host, args.port, args.timeout, args.finalize_delay) as printer:
            print("Control granted.", flush=True)
            if args.action == "info":
                print_response(printer.command("M115"))
            elif args.action == "status":
                print_response(printer.command("M119"))
                print_response(printer.command("M105"))
                print_response(printer.command("M27"))
            elif args.action == "pause":
                print_response(printer.command("M25"))
            elif args.action == "resume":
                print_response(printer.command("M24"))
            elif args.action == "cancel":
                print_response(printer.command("M26"))
            elif args.action == "command":
                print_response(printer.command(args.gcode))
            elif args.action == "upload":
                local_file = args.file.resolve()
                if local_file.suffix.lower() != ".gx":
                    raise ValueError(f"Upload requires a sliced .gx file, got: {local_file.name}")
                if not local_file.is_file():
                    raise FileNotFoundError(local_file)
                print(f"Local file: {local_file}")
                print(f"File size: {local_file.stat().st_size:,} bytes", flush=True)
                remote_path = printer.upload(local_file, args.remote_name)
                print(f"Saved as {remote_path}")
                if args.start:
                    print_response(printer.start_print(remote_path))
            else:
                raise AssertionError(args.action)
        return 0
    except (OSError, FinderProtocolError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
