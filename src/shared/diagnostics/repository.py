"""Small, local diagnostic sessions; never read project contents."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import ExitStack
import errno
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout


SESSION_LIMIT = 5
READ_LIMIT = 64 * 1024
TOTAL_STORAGE_LIMIT = 10 * 1024 * 1024
SESSION_RESERVATION = 1024 * 1024
LOG_LIMIT = 256 * 1024
TRACE_LIMIT = 256 * 1024
CONTEXT_LIMIT = 4096
_METADATA_RESERVATION = 4096
_SESSION_FILES = (
    "context.txt", "events.log", "events.log.1", "trace.log", "reviewed", "trace.done",
)


class ReportRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    def sessions(self) -> list[Path]:
        try:
            return sorted((p for p in self.root.glob("session-*") if p.is_dir() and not p.is_symlink()), reverse=True)
        except OSError:
            return []

    def start_session(self) -> tuple[Path, FileLock]:
        self.root.mkdir(parents=True, exist_ok=True)
        # Admission is serialized across processes. Reserve maximum future
        # growth, not today's file sizes: writers never need to scan or lock the
        # global store while logging or recording a fatal error.
        with self.budget_lock():
            sessions = self.sessions()
            sizes = {path: self._size(path) for path in sessions}
            costs = {path: max(SESSION_RESERVATION, size + _METADATA_RESERVATION)
                     for path, size in sizes.items()}
            other_files = max(0, self._size(self.root) - sum(sizes.values()))
            reserved = sum(costs.values()) + other_files + _METADATA_RESERVATION
            for old in reversed(sessions):
                if len(costs) < SESSION_LIMIT and reserved + SESSION_RESERVATION <= TOTAL_STORAGE_LIMIT:
                    break
                if self._remove_inactive(old):
                    reserved -= costs.pop(old)
            if reserved + SESSION_RESERVATION > TOTAL_STORAGE_LIMIT:
                # Busy or undeletable reports are protected. Decline new disk
                # logging instead of exceeding the budget or blocking startup.
                raise OSError(errno.ENOSPC, "Diagnostic storage budget is in use")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            directory = self.root / f"session-{stamp}-{uuid4().hex[:8]}"
            directory.mkdir(mode=0o700)
            lock = FileLock(directory / "active.lock")
            lock.acquire(timeout=0)
            return directory, lock

    def budget_lock(self, *, timeout: float = 0) -> FileLock:
        return FileLock(self.root / "budget.lock", timeout=timeout)

    @staticmethod
    def _size(directory: Path) -> int:
        # Include unexpected files in old sessions; never delete those files.
        return sum(path.stat().st_size for path in directory.rglob("*")
                   if path.is_file() and not path.is_symlink())

    @staticmethod
    def _remove_inactive(directory: Path) -> bool:
        try:
            with ExitStack() as locks:
                locks.enter_context(FileLock(directory / "active.lock", timeout=0))
                locks.enter_context(FileLock(directory / "trace.lock", timeout=0))
                for name in _SESSION_FILES:
                    (directory / name).unlink(missing_ok=True)
            for name in ("active.lock", "trace.lock"):
                (directory / name).unlink(missing_ok=True)
            directory.rmdir()
            return True
        except (OSError, Timeout):
            return False

    @staticmethod
    def write_context(directory: Path, text: str) -> None:
        (directory / "context.txt").write_bytes(text.encode("utf-8", errors="replace")[:CONTEXT_LIMIT])

    @staticmethod
    def read_tail(path: Path) -> str:
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - READ_LIMIT))
                text = stream.read(READ_LIMIT).decode("utf-8", errors="replace")
            return ("[Earlier content omitted]\n" if size > READ_LIMIT else "") + text
        except FileNotFoundError:
            return ""
        except OSError as error:
            return f"[Diagnostics could not be read: {type(error).__name__}]"

    def has_unreviewed_trace(self) -> bool:
        for directory in self.sessions()[:SESSION_LIMIT]:
            try:
                if (directory / "trace.log").stat().st_size > int(
                    (directory / "reviewed").read_text() if (directory / "reviewed").exists() else "0"
                ):
                    return True
            except (OSError, ValueError):
                pass
        return False

    def mark_reviewed(self) -> None:
        for directory in self.sessions()[:SESSION_LIMIT]:
            try:
                (directory / "reviewed").write_text(str((directory / "trace.log").stat().st_size))
            except OSError:
                pass

    @staticmethod
    def save_report(destination: Path, text: str) -> None:
        # Replace only after a successful write; a full disk must not destroy an
        # existing report. Stage beside the chosen destination, not in /tmp.
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(text)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
