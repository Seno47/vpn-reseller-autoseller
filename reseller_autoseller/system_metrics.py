from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import platform
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any


APP_STARTED_AT = time.time()
_LOCK = threading.Lock()
_LAST_CPU: tuple[float, float] | None = None
_LAST_PROCESS: tuple[float, float] | None = None


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _filetime_to_seconds(value: Any) -> float:
    raw = (int(value.dwHighDateTime) << 32) + int(value.dwLowDateTime)
    return raw / 10_000_000


def _windows_cpu_times() -> tuple[float, float] | None:
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    idle_seconds = _filetime_to_seconds(idle)
    total_seconds = _filetime_to_seconds(kernel) + _filetime_to_seconds(user)
    return total_seconds, idle_seconds


def _linux_cpu_times() -> tuple[float, float] | None:
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
        values = [float(item) for item in parts]
    except (OSError, ValueError, IndexError):
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def _cpu_percent() -> float | None:
    global _LAST_CPU
    current = _windows_cpu_times() if platform.system().lower() == "windows" else _linux_cpu_times()
    if current is None:
        return None
    with _LOCK:
        previous = _LAST_CPU
        _LAST_CPU = current
    if previous is None:
        return None
    total_delta = current[0] - previous[0]
    idle_delta = current[1] - previous[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100))


def _windows_memory() -> dict[str, Any] | None:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total = float(status.ullTotalPhys)
    available = float(status.ullAvailPhys)
    used = max(0.0, total - available)
    swap_total = float(status.ullTotalPageFile)
    swap_available = float(status.ullAvailPageFile)
    return {
        "total_mb": _round(total / 1024 / 1024),
        "available_mb": _round(available / 1024 / 1024),
        "used_mb": _round(used / 1024 / 1024),
        "percent": _round(used / total * 100 if total else 0),
        "swap_total_mb": _round(swap_total / 1024 / 1024),
        "swap_used_mb": _round(max(0.0, swap_total - swap_available) / 1024 / 1024),
    }


def _linux_memory() -> dict[str, Any] | None:
    try:
        rows = Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    values: dict[str, float] = {}
    for row in rows:
        key, _, rest = row.partition(":")
        parts = rest.strip().split()
        if parts:
            values[key] = float(parts[0]) * 1024
    total = values.get("MemTotal", 0.0)
    available = values.get("MemAvailable", values.get("MemFree", 0.0))
    used = max(0.0, total - available)
    swap_total = values.get("SwapTotal", 0.0)
    swap_free = values.get("SwapFree", 0.0)
    return {
        "total_mb": _round(total / 1024 / 1024),
        "available_mb": _round(available / 1024 / 1024),
        "used_mb": _round(used / 1024 / 1024),
        "percent": _round(used / total * 100 if total else 0),
        "swap_total_mb": _round(swap_total / 1024 / 1024),
        "swap_used_mb": _round(max(0.0, swap_total - swap_free) / 1024 / 1024),
    }


def _memory() -> dict[str, Any]:
    data = _windows_memory() if platform.system().lower() == "windows" else _linux_memory()
    return data or {
        "total_mb": None,
        "available_mb": None,
        "used_mb": None,
        "percent": None,
        "swap_total_mb": None,
        "swap_used_mb": None,
    }


def _windows_process_times() -> float | None:
    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]

    creation = FILETIME()
    exit_time = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    return _filetime_to_seconds(kernel) + _filetime_to_seconds(user)


def _process_cpu_seconds() -> float:
    if platform.system().lower() == "windows":
        value = _windows_process_times()
        if value is not None:
            return value
    return time.process_time()


def _process_cpu_percent() -> float | None:
    global _LAST_PROCESS
    current = (time.monotonic(), _process_cpu_seconds())
    with _LOCK:
        previous = _LAST_PROCESS
        _LAST_PROCESS = current
    if previous is None:
        return None
    elapsed = current[0] - previous[0]
    cpu_delta = current[1] - previous[1]
    if elapsed <= 0:
        return None
    cores = os.cpu_count() or 1
    return max(0.0, cpu_delta / elapsed / cores * 100)


def _windows_process_rss_mb() -> float | None:
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    psapi = ctypes.WinDLL("psapi.dll", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return None
    return counters.WorkingSetSize / 1024 / 1024


def _process_rss_mb() -> float | None:
    if platform.system().lower() == "windows":
        return _windows_process_rss_mb()
    try:
        pages = int(Path("/proc/self/statm").read_text(encoding="utf-8").split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _system_uptime_seconds() -> float | None:
    if platform.system().lower() == "windows":
        return ctypes.windll.kernel32.GetTickCount64() / 1000
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _load_average() -> list[float] | None:
    try:
        return [_round(value, 2) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def _disk(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    used = usage.total - usage.free
    return {
        "path": str(path),
        "total_mb": _round(usage.total / 1024 / 1024),
        "used_mb": _round(used / 1024 / 1024),
        "free_mb": _round(usage.free / 1024 / 1024),
        "percent": _round(used / usage.total * 100 if usage.total else 0),
    }


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total = int(max(0, seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, _ = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def collect_system_metrics(base_path: Path | None = None) -> dict[str, Any]:
    disk_path = base_path or Path.cwd()
    cpu_percent = _cpu_percent()
    process_cpu_percent = _process_cpu_percent()
    if cpu_percent is None or process_cpu_percent is None:
        time.sleep(0.05)
        cpu_percent = _cpu_percent() if cpu_percent is None else cpu_percent
        process_cpu_percent = _process_cpu_percent() if process_cpu_percent is None else process_cpu_percent
    app_uptime = time.time() - APP_STARTED_AT
    system_uptime = _system_uptime_seconds()
    return {
        "timestamp": time.time(),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "cpu": {
            "cores": os.cpu_count() or 1,
            "percent": _round(cpu_percent),
            "load_average": _load_average(),
        },
        "memory": _memory(),
        "disk": _disk(disk_path),
        "process": {
            "pid": os.getpid(),
            "rss_mb": _round(_process_rss_mb()),
            "cpu_percent": _round(process_cpu_percent, 2),
            "uptime_seconds": _round(app_uptime, 0),
            "uptime": format_duration(app_uptime),
        },
        "uptime_seconds": _round(system_uptime, 0),
        "uptime": format_duration(system_uptime),
    }
