"""Windows package identity without importing Qt or execution dependencies."""

from __future__ import annotations

import ctypes
import sys


def current_package_full_name() -> str | None:
    """Return the OS-assigned identity, or None for an unpackaged process."""
    if sys.platform != "win32":
        return None
    query = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentPackageFullName
    query.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
    query.restype = ctypes.c_long
    length = ctypes.c_uint32()
    status = query(ctypes.byref(length), None)
    if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE
        return None
    if status != 122:  # ERROR_INSUFFICIENT_BUFFER
        raise ctypes.WinError(status)
    buffer = ctypes.create_unicode_buffer(length.value)
    status = query(ctypes.byref(length), buffer)
    if status:
        raise ctypes.WinError(status)
    return buffer.value
