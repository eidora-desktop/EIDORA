from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import faulthandler
import platform
import sys
import threading
import time

from eidora_version import __version__
from shared.diagnostics.repository import LOG_LIMIT, TRACE_LIMIT, ReportRepository
from shared.diagnostics.service import LOGGER, record_failure
from shared.diagnostics.trace import CrashTrace


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class _DiagnosticHandler(RotatingFileHandler):
    def _open(self):
        # Keep byte accounting exact on Windows too; no LF-to-CRLF expansion.
        return self._builtin_open(self.baseFilename, self.mode, encoding=self.encoding,
                                  errors=self.errors, newline="\n")

    def handleError(self, record) -> None:
        # Reporting must not fail recursively when the diagnostic drive is full.
        pass

    def format(self, record) -> str:
        # Bound encoded bytes, including multibyte messages and the newline.
        return super().format(record).encode("utf-8", errors="backslashreplace")[:16383].decode("utf-8", errors="ignore")

    def emit(self, record) -> None:
        if self._closed:
            return
        try:
            # Format once so the bytes measured are exactly the bytes written,
            # even when a logged object's string representation changes.
            message = self.format(record) + "\n"
            if self.stream is None:
                self.stream = self._open()
            self.stream.seek(0, 2)
            if self.stream.tell() + len(message.encode("utf-8")) > self.maxBytes:
                self.doRollover()
            self.stream.write(message)
            self.flush()
        except Exception:
            self.handleError(record)


class DiagnosticSession:
    """Own file handles and process hooks for one application session."""

    def __init__(self, repository: ReportRepository, notify=None, *, qt_version: str = "unknown") -> None:
        self.directory, self._lock = repository.start_session()
        self._handler = None
        self._trace = None
        self._closed = False
        self._notify = notify
        self._previous_exception_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook
        self._owns_fault_handler = False
        self._captures = 0
        self._trace_size = 0
        try:
            repository.write_context(self.directory,
                f"EIDORA {__version__}; Qt {qt_version}; Python {platform.python_version()}\n"
                f"{platform.system()} {platform.release()}; {platform.machine()}; "
                f"packaged={bool(getattr(sys, 'frozen', False))}\n")
            self._handler = _DiagnosticHandler(
                self.directory / "events.log", maxBytes=LOG_LIMIT, backupCount=1,
                encoding="utf-8", errors="backslashreplace",
            )
            formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(name)s: %(message)s")
            formatter.converter = time.gmtime
            self._handler.setFormatter(formatter)
            self._handler.addFilter(lambda record: record.levelno >= logging.WARNING or record.name.startswith("eidora."))
            logging.getLogger().addHandler(self._handler)
            # Respect a debugger or test runner that already owns faulthandler.
            if not faulthandler.is_enabled():
                try:
                    self._trace = CrashTrace(self.directory)
                    faulthandler.enable(file=self._trace, all_threads=True)
                    self._owns_fault_handler = True
                except (OSError, AttributeError, NotImplementedError):
                    # Never fall back to an unbounded native-write file.
                    if self._trace is not None:
                        self._trace.close()
                        self._trace = None
                    LOGGER.warning("Bounded crash capture is unavailable; ordinary diagnostics remain enabled.")
            sys.excepthook = self._exception_hook
            threading.excepthook = self._thread_hook
            LOGGER.info("Session started; EIDORA %s; %s %s; %s; Python %s; packaged=%s",
                        __version__, platform.system(), platform.release(), platform.machine(),
                        platform.python_version(), bool(getattr(sys, "frozen", False)))
        except BaseException:
            self.close()
            raise

    def _exception_hook(self, kind, error, traceback) -> None:
        if not issubclass(kind, (KeyboardInterrupt, SystemExit)):
            record_failure("Unexpected application error", error)
            if self._notify is not None:
                self._notify(str(error)[:2000])
        if sys.stderr is not None:
            self._previous_exception_hook(kind, error, traceback)

    def _thread_hook(self, args) -> None:
        if args.exc_type is not SystemExit:
            record_failure("Unexpected background-thread error", args.exc_value)
            if self._notify is not None:
                self._notify(str(args.exc_value)[:2000])
        if sys.stderr is not None:
            self._previous_thread_hook(args)

    def heartbeat(self, timeout: float = 30) -> None:
        if self._closed or not self._owns_fault_handler:
            return
        size = self._trace.size()
        if size > self._trace_size:
            self._captures += 1
            self._trace_size = size
            LOGGER.warning("Interface heartbeat resumed after a diagnostic capture; cause undetermined.")
        # One stack snapshot per stall, at most three per session. No repeated
        # dumps while frozen or polling source/cloud folders. The collector
        # enforces the byte limit even if the main process never responds again.
        if self._captures < 3 and size < TRACE_LIMIT:
            faulthandler.dump_traceback_later(timeout, repeat=False, file=self._trace)

    def stop_heartbeat(self) -> None:
        if self._owns_fault_handler:
            faulthandler.cancel_dump_traceback_later()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_heartbeat()
        if self._owns_fault_handler:
            faulthandler.disable()
        if sys.excepthook == self._exception_hook:
            sys.excepthook = self._previous_exception_hook
        if threading.excepthook == self._thread_hook:
            threading.excepthook = self._previous_thread_hook
        if self._handler is not None:
            LOGGER.info("Diagnostic session closed.")
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
        if self._trace is not None:
            self._trace.close()
        ReportRepository.finish_session(self.directory)
        self._lock.release()

