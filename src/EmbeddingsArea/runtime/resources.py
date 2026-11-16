"""Read-only resource checks for embedding workers; never change hardware settings."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import platform
import shutil
import time

import psutil

from EmbeddingsArea.runtime.batching import BATCH_POLICIES

MIB = 1024 ** 2


@lru_cache(maxsize=1)
def _apple_thermal_reader():
    ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
    objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.objc_getClass.argtypes = [ctypes.c_char_p]
    objc.sel_registerName.restype = ctypes.c_void_p
    objc.sel_registerName.argtypes = [ctypes.c_char_p]
    send_object = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    send_integer = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(('objc_msgSend', objc))
    process = send_object(objc.objc_getClass(b'NSProcessInfo'), objc.sel_registerName(b'processInfo'))
    selector = objc.sel_registerName(b'thermalState')
    return lambda: int(send_integer(process, selector))


def thermal_state() -> int | None:
    try:
        if platform.system() == 'Darwin':
            return _apple_thermal_reader()()
        sensors = getattr(psutil, 'sensors_temperatures', lambda: {})()
        states = []
        for readings in sensors.values():
            for value in readings:
                if value.critical and value.current >= value.critical:
                    states.append(3)
                elif value.high:
                    states.append(2 if value.current >= value.high else 1 if value.current >= value.high - 10 else 0)
        return max(states) if states else None
    except (OSError, AttributeError, ValueError):
        return None


@lru_cache(maxsize=1)
def _nvml():
    """NVML ships with the driver. Query all GPUs conservatively; no setter APIs."""
    library = ctypes.CDLL('nvml.dll' if platform.system() == 'Windows' else 'libnvidia-ml.so.1')
    if library.nvmlInit_v2() != 0:
        raise OSError('NVIDIA monitoring unavailable')
    return library


def gpu_resources() -> tuple[int | None, int | None]:
    class Memory(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in ('total', 'free', 'used')]
    try:
        lib = _nvml()
        count = ctypes.c_uint()
        if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) or not count.value:
            return None, None
        free, states = [], []
        for index in range(count.value):
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(index), ctypes.byref(handle)):
                continue
            memory, temperature, threshold = Memory(), ctypes.c_uint(), ctypes.c_uint()
            if not lib.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)):
                free.append(int(memory.free))
            # NVML_TEMPERATURE_GPU=0; NVML_TEMPERATURE_THRESHOLD_SLOWDOWN=1.
            if (not lib.nvmlDeviceGetTemperature(handle, 0, ctypes.byref(temperature))
                    and not lib.nvmlDeviceGetTemperatureThreshold(handle, 1, ctypes.byref(threshold))):
                states.append(2 if temperature.value >= threshold.value - 5 else
                              1 if temperature.value >= threshold.value - 15 else 0)
        return min(free) if free else None, max(states) if states else None
    except (OSError, AttributeError):
        return None, None


@dataclass(frozen=True)
class ResourceState:
    available: int
    resident: int
    thermal: int | None
    gpu_free: int | None
    stop_reason: str = ''
    reduce: bool = False
    pressure_reason: str = ''


class ResourceMonitor:
    """One-second sampling, conservative growth limits, and resumable safety stops."""

    def __init__(self, directory: Path, device: str, policy: str = 'auto') -> None:
        self.directory, self.device = directory, device
        self.policy_name = policy
        self.policy = BATCH_POLICIES[policy]
        self.process = psutil.Process()
        memory = psutil.virtual_memory()
        self.budget = int(min(memory.total, memory.available) * self.policy.memory_budget)
        self.total = memory.total
        self._at = float('-inf')
        self._state = None
        self._pressure_limit: int | None = None

    def sample(self, *, force: bool = False) -> ResourceState:
        if not force and time.monotonic() - self._at < 1 and self._state is not None:
            return self._state
        memory = psutil.virtual_memory()
        resident = self.process.memory_info().rss
        thermal = thermal_state()
        gpu_free = None
        if self.device == 'cuda':
            gpu_free, gpu_thermal = gpu_resources()
            if gpu_thermal is not None:
                thermal = max(thermal or 0, gpu_thermal)
        reason = ''
        if memory.available < max(512 * MIB, self.total // 20) or resident > self.budget:
            reason = 'Paused: memory pressure. Close other heavy applications before resuming.'
        if gpu_free is not None and gpu_free < 256 * MIB:
            reason = 'Paused: GPU memory pressure. Free GPU memory before resuming.'
        if thermal is not None and thermal >= 2:
            reason = 'Paused to cool down. Resume when the system has cooled.'
        directory = self.directory if self.directory.exists() else self.directory.parent
        if shutil.disk_usage(directory).free < 256 * MIB:
            reason = 'Paused: low disk space. Free space before resuming.'
        pressures = []
        if memory.available < max(1024 * MIB, self.total * self.policy.available_reserve):
            pressures.append('low available RAM')
        if resident > self.budget * self.policy.resident_reserve:
            pressures.append('worker memory budget')
        if gpu_free is not None and gpu_free < (1024 if self.policy_name == 'low' else 512) * MIB:
            pressures.append('low GPU memory')
        reduce = bool(pressures)
        if thermal == 1:
            pressures.append('elevated thermal state')
            reduce = reduce or self.policy_name == 'low'
        self._state = ResourceState(memory.available, resident, thermal, gpu_free, reason,
                                    reduce, ', '.join(pressures))
        if not reduce:
            self._pressure_limit = None
        self._at = time.monotonic()
        return self._state

    def batch_limit(self, current: int) -> int:
        state = self.sample()
        limit = 512
        if state.reduce:
            # One reduction per pressure episode, not one halving per batch.
            # Persistent pressure prevents recovery; critical pressure pauses.
            if self._pressure_limit is None:
                self._pressure_limit = max(1, current // 2)
            limit = min(current, self._pressure_limit)
        elif state.thermal == 1 and self.policy_name == 'auto':
            limit = current  # Mild warmth blocks growth without shrinking useful work.
        # Unknown GPU/thermal headroom must not authorize aggressive growth.
        if self.device == 'cuda' and state.gpu_free is None:
            limit = min(limit, current, 8)
        if state.thermal is None:
            limit = min(limit, 16)
        return limit

    def require_disk_space(self, additional_bytes: int) -> None:
        if shutil.disk_usage(self.directory).free < additional_bytes + 256 * MIB:
            from EmbeddingsArea.runtime.contracts import RunPaused
            raise RunPaused('Paused: insufficient disk space to save embeddings.')
