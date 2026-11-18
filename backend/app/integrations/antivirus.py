"""Malware scanning adapters.

``ClamAVScanner`` speaks the clamd INSTREAM protocol over TCP. ``DisabledDevScanner`` is a
development-only stand-in: it reports ``skipped_dev`` (never "clean"), is listed as a
development adapter in readiness output and is refused by production settings validation.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from typing import Literal, Protocol

ScanStatus = Literal["clean", "infected", "skipped_dev", "error"]
_CHUNK = 64 * 1024


@dataclass(frozen=True, slots=True)
class ScanResult:
    status: ScanStatus
    signature: str | None = None
    detail: str | None = None


class Scanner(Protocol):
    name: str

    def scan(self, data: bytes) -> ScanResult: ...


class DisabledDevScanner:
    name = "disabled_dev"

    def scan(self, data: bytes) -> ScanResult:
        return ScanResult(
            "skipped_dev", detail="Antivirus scanning is disabled in this development environment"
        )


class ClamAVScanner:
    name = "clamav"

    def __init__(self, host: str, port: int, timeout_seconds: float = 30.0) -> None:
        self.host, self.port, self.timeout = host, port, timeout_seconds

    def _exchange(self, payload: bytes | None, command: bytes) -> str:
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as conn:
            conn.sendall(command)
            if payload is not None:
                for start in range(0, len(payload), _CHUNK):
                    chunk = payload[start : start + _CHUNK]
                    conn.sendall(struct.pack("!L", len(chunk)) + chunk)
                conn.sendall(struct.pack("!L", 0))
            response = b""
            while not response.endswith(b"\0"):
                part = conn.recv(4096)
                if not part:
                    break
                response += part
        return response.rstrip(b"\0").decode(errors="replace").strip()

    def ping(self) -> bool:
        return self._exchange(None, b"zPING\0") == "PONG"

    def scan(self, data: bytes) -> ScanResult:
        try:
            reply = self._exchange(data, b"zINSTREAM\0")
        except OSError as exc:
            return ScanResult("error", detail=f"clamd unreachable ({type(exc).__name__})")
        # Replies: "stream: OK", "stream: Eicar-Signature FOUND", "INSTREAM size limit exceeded. ERROR"
        if reply.endswith(" OK"):
            return ScanResult("clean")
        if reply.endswith(" FOUND"):
            return ScanResult("infected", signature=reply.removeprefix("stream: ").removesuffix(" FOUND"))
        return ScanResult("error", detail=reply[:200])


def build_scanner(provider: str, host: str, port: int) -> Scanner:
    if provider == "clamav":
        return ClamAVScanner(host, port)
    return DisabledDevScanner()
