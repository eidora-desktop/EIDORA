"""Evidence-based failure guidance and a reviewable, manual support report."""
from __future__ import annotations

from datetime import datetime, timezone
import errno
import logging
import platform
import re

from eidora_version import __version__
from shared.diagnostics.repository import ReportRepository, SESSION_LIMIT


LOGGER = logging.getLogger("eidora.diagnostics")


def failure_guidance(error: BaseException | str) -> str:
    """Prefer original OS codes; retain explicit codes across worker boundaries."""
    codes: set[int | None] = set()
    windows_codes: set[int | None] = set()
    seen: set[int] = set()
    current = error if isinstance(error, BaseException) else None
    sqlite_full = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        codes.add(getattr(current, "errno", None))
        windows_codes.add(getattr(current, "winerror", None))
        sqlite_full |= getattr(current, "sqlite_errorcode", None) == 13
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    codes.update(int(code) for code in re.findall(r"\[Errno (\d+)\]", str(error)))
    windows_codes.update(int(code) for code in re.findall(r"\[WinError (\d+)\]", str(error)))
    if codes & {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)} or windows_codes & {39, 112} or sqlite_full:
        return ("Storage space or a storage quota was exhausted. Free space on the drive used by "
                "this operation (including its temporary storage), then retry. This error does not "
                "indicate that your source data is invalid.")
    if errno.ENOENT in codes or windows_codes & {2, 3} or isinstance(error, FileNotFoundError):
        return ("A required file or folder could not be found. Check whether it was moved or deleted, "
                "reconnect external drives, and make cloud files available locally. For a media "
                "source, update its location in Assets. The error alone cannot identify which of these happened.")
    if codes & {errno.EACCES, errno.EPERM, errno.EROFS} or windows_codes & {5, 19}:
        return ("Access was denied or the location is read-only. Check the folder's permissions "
                "and whether the drive allows writing, or choose a writable location.")
    if errno.ETIMEDOUT in codes or isinstance(error, TimeoutError):
        return ("The operation timed out. Check the connection or storage availability and retry. "
                "A timeout alone does not establish a cloud-storage problem or an EIDORA bug.")
    if isinstance(error, MemoryError) or errno.ENOMEM in codes:
        return "Memory allocation failed. Close other applications or reduce the workload before retrying."
    return ""


def record_failure(operation: str, error: BaseException | str) -> str:
    guidance = failure_guidance(error)
    LOGGER.error("%s: %s%s", operation, str(error)[:8000], f"\n{guidance}" if guidance else "",
                 exc_info=(type(error), error, error.__traceback__) if isinstance(error, BaseException) else None)
    return guidance


def redact_paths(text: str) -> str:
    # Best effort, followed by an editable user preview. Do not collect locals,
    # environment variables, media, metadata tables, or project files.
    text = re.sub(r"https?://[^\s'\"<>]+", "<url>", text)
    text = re.sub(r'File "[^"]+", line', lambda match: 'File "<path>/' +
                  match.group().split('"')[1].replace("\\", "/").rsplit("/", 1)[-1] + '", line', text)
    text = re.sub(r"(['\"])(?:[A-Za-z]:[\\/]|/|\\\\)[^'\"\n]+\1", "'<path>'", text)
    text = re.sub(r"(?<![\w<>])(?:[A-Za-z]:[\\/]|/|\\\\)[^\s'\"<>]+", "<path>", text)
    return text


def build_report(repository: ReportRepository) -> str:
    lines = [
        "EIDORA diagnostic report",
        "",
        "YOUR PROBLEM DESCRIPTION",
        "[Replace this line with what you were doing, what happened, and what you expected.]",
        "",
        "TECHNICAL DIAGNOSTICS",
        f"Created (UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Current EIDORA: {__version__}",
        f"System: {platform.system()} {platform.release()} ({platform.machine()})",
        f"Python: {platform.python_version()}",
        "",
    ]
    sessions = repository.sessions()[:SESSION_LIMIT]
    statuses = {directory: repository.session_status(directory) for directory in sessions}
    # Keep the session that prompted a restart ahead of fresh startup output.
    # Within each group, retain newest-first order.
    sessions.sort(key=lambda directory: (
        2 if statuses[directory] == "running" else
        0 if statuses[directory] == "interrupted" or repository.trace_size(directory) else 1
    ))
    status_labels = {
        "running": "Running now",
        "closed": "Closed normally",
        "interrupted": "Ended without a normal shutdown",
        "unknown": "Shutdown status unavailable",
    }
    if not sessions:
        lines.append("No saved diagnostics are available.")
    for directory in sessions:
        lines.extend((f"--- {directory.name} ---",))
        lines.append(f"Status: {status_labels[statuses[directory]]}")
        if statuses[directory] == "interrupted" and not repository.trace_size(directory):
            lines.append("No crash trace was captured. A force quit can prevent capture; the cause is unknown.")
        for filename in ("context.txt", "events.log.1", "events.log", "trace.log"):
            content = repository.read_tail(directory / filename)
            if content:
                lines.extend((f"[{filename}]", redact_paths(content)))
    return "\n".join(lines)
