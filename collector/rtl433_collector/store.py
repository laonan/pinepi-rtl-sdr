"""TCP receiver + raw-stream persistence.

:class:`RawStore` owns the rolling raw JSON file: it appends received lines and
can atomically "roll" the file (rename the current file out of the way and start
a fresh one) after the daily snapshot has been rendered.

:class:`Receiver` runs a threaded TCP server that accepts the sensor's
``socat`` connection(s) and writes each newline-delimited JSON line into the
store. It mirrors the plain ``socat TCP-LISTEN:...,fork`` sink documented in
USAGE.md, but in-process so we also control rotation and parsing.
"""

from __future__ import annotations

import logging
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("rtl433_collector.store")


class RawStore:
    """Append-only raw JSON file with atomic daily rollover.

    Thread-safe: the receiver thread appends while the scheduler thread rolls.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = None
        self._open()

    def _open(self) -> None:
        # Line-buffered append so a crash keeps whole lines.
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def append_line(self, line: str) -> None:
        """Append one already-decoded text line (without trailing newline)."""
        line = line.rstrip("\r\n")
        if not line:
            return
        with self._lock:
            self._fh.write(line + "\n")

    def roll(self, *, archive_suffix: Optional[str] = None) -> Optional[Path]:
        """Close the current file, move it aside, and open a fresh one.

        Returns the path the old data was moved to, or ``None`` if the file was
        empty / missing. The caller is responsible for deleting the archive once
        it has been consumed (rendered).
        """
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

            archived: Optional[Path] = None
            if self.path.exists() and self.path.stat().st_size > 0:
                suffix = archive_suffix or datetime.now().strftime("%Y%m%d_%H%M%S")
                archived = self.path.with_name(f"{self.path.name}.{suffix}")
                self.path.replace(archived)

            self._open()
            return archived

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


def iter_json_lines(path: Path | str) -> Iterator[dict]:
    """Yield parsed JSON objects from a newline-delimited JSON file.

    Malformed lines are skipped (rtl_433 output can be partial across
    rotation). Missing file yields nothing.
    """
    import json

    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


class Receiver:
    """Threaded TCP listener that writes received lines into a :class:`RawStore`.

    :param store: destination for received lines.
    :param host:  bind address (``0.0.0.0`` to accept from the sensor Pi).
    :param port:  TCP port (must match the sensor's ``FORWARD_PORT``).
    """

    def __init__(
        self,
        store: RawStore,
        *,
        host: str = "0.0.0.0",
        port: int = 9000,
        backlog: int = 8,
    ) -> None:
        self.store = store
        self.host = host
        self.port = port
        self.backlog = backlog
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        sock.settimeout(1.0)  # so accept() wakes up to check _stop
        self._sock = sock
        log.info("listening on %s:%d", self.host, self.port)

        accept_thread = threading.Thread(
            target=self._accept_loop, name="receiver-accept", daemon=True
        )
        accept_thread.start()
        self._threads.append(accept_thread)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            log.info("connection from %s:%d", *addr)
            t = threading.Thread(
                target=self._handle_conn,
                args=(conn, addr),
                name=f"receiver-conn-{addr[0]}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        conn.settimeout(1.0)
        buf = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break  # peer closed
                buf += chunk
                # Split complete lines; keep the trailing partial in buf.
                *lines, buf = buf.split(b"\n")
                for raw in lines:
                    self.store.append_line(raw.decode("utf-8", "replace"))
        finally:
            # Flush any trailing complete text left without a newline.
            if buf:
                self.store.append_line(buf.decode("utf-8", "replace"))
            conn.close()
            log.info("connection closed %s:%d", *addr)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
