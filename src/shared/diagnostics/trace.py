"""A bounded sink for raw fault-handler writes, independent of the crashing process."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from filelock import FileLock, Timeout

from shared.diagnostics.repository import ReportRepository, TRACE_LIMIT
from shared.internal_workers import internal_worker_command


TRUNCATED = b"\n[Further crash/freeze output omitted: trace size limit reached.]\n"


class CrashTrace:
    def __init__(self, directory: Path) -> None:
        self.path = directory / "trace.log"
        self.process = subprocess.Popen(
            internal_worker_command("diagnostic-trace", directory),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            bufsize=0, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            # A full pipe must never block the crashing thread. Losing some
            # diagnostic bytes is preferable to freezing or growing storage.
            os.set_blocking(self.fileno(), False)
        except (OSError, AttributeError, NotImplementedError):
            self.close()
            raise

    def fileno(self) -> int:
        return self.process.stdin.fileno()

    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self) -> None:
        self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=1)


def _input_descriptor() -> int:
    if sys.platform != "win32":
        return os.dup(0)
    # Windowed builds have no sys.stdin and may have no CRT descriptor 0.
    # Duplicate the explicitly inherited OS pipe handle into an owned CRT fd.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    handle = kernel.GetStdHandle(-10)  # STD_INPUT_HANDLE
    process = kernel.GetCurrentProcess()
    copied = wintypes.HANDLE()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(copied), 0, False, 2):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return msvcrt.open_osfhandle(copied.value, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(copied)
        raise


def collect(directory: Path, descriptor: int) -> None:
    repository = ReportRepository(directory.parent)
    # Acquire protection before opening the file; an immediately crashing parent
    # may already have released its own lock. Never recreate a pruned session.
    lock = FileLock(directory / "trace.lock", timeout=0)
    with repository.budget_lock(timeout=1):
        if not directory.is_dir() or directory.is_symlink():
            return
        lock.acquire()
    try:
        remaining = TRACE_LIMIT - len(TRUNCATED)
        truncated = False
        with (directory / "trace.log").open("xb", buffering=0) as output:
            while chunk := os.read(descriptor, 8192):
                if truncated:
                    continue  # Drain excess bytes in memory until parent EOF.
                try:
                    accepted = chunk[:remaining]
                    output.write(accepted)
                    remaining -= len(accepted)
                    if len(chunk) > len(accepted):
                        output.write(TRUNCATED)
                        truncated = True
                except OSError:
                    truncated = True  # Disk full: stop writing, keep draining.
    finally:
        try:
            (directory / "trace.done").touch()
        except OSError:
            pass
        lock.release()


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        return 2
    descriptor = None
    try:
        descriptor = _input_descriptor()
        collect(Path(arguments[0]), descriptor)
        return 0
    except (OSError, Timeout):
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)
