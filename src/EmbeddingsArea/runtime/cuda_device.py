"""Query the OS NVIDIA driver in an isolated worker; no CUDA download is required."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import sys


def cuda_devices() -> list[str]:
    from EmbeddingsArea.runtime.cuda_install import platform_key

    if platform_key() is None:
        return []
    try:
        driver = (ctypes.WinDLL("nvcuda.dll", winmode=0x00000800) if sys.platform == "win32"
                  else ctypes.CDLL("libcuda.so.1"))
        if driver.cuInit(0) != 0:
            return []
        count = ctypes.c_int()
        if driver.cuDeviceGetCount(ctypes.byref(count)) != 0:
            return []
        names = []
        for ordinal in range(count.value):
            device, major, minor = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
            if driver.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
                continue
            if driver.cuDeviceComputeCapability(ctypes.byref(major), ctypes.byref(minor), device) != 0 or major.value < 5:
                continue
            name = ctypes.create_string_buffer(256)
            if driver.cuDeviceGetName(name, len(name), device) == 0:
                names.append(name.value.decode("utf-8", errors="replace"))
        return names
    except (OSError, AttributeError):
        return []


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        result = {"ok": True, "devices": cuda_devices()}
    except Exception as exc:
        result = {"ok": False, "devices": [], "error": str(exc)}
    Path(args[0]).write_text(json.dumps(result), encoding="utf-8")
    return 0 if result["ok"] else 1
