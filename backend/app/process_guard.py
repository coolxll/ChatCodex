"""Best-effort operating-system ownership for spawned process trees."""
from __future__ import annotations

import ctypes
import os
from typing import Callable, Optional


def attach_windows_kill_job(
        pid: int, log: Optional[Callable[[str], None]] = None) -> Optional[int]:
    """Return a kill-on-close Job Object handle for ``pid`` on Windows.

    Restricted or already job-contained hosts may reject assignment. That is a
    recoverable condition: orderly shutdown still terminates the child.
    """
    if os.name != "nt":
        return None

    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        if log:
            log(f"job: CreateJobObject failed ({ctypes.get_last_error()})")
        return None

    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info))
    process = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
    assigned = bool(process and kernel32.AssignProcessToJobObject(job, process))
    if process:
        kernel32.CloseHandle(process)
    if not configured or not assigned:
        if log:
            log(f"job: assignment unavailable ({ctypes.get_last_error()})")
        kernel32.CloseHandle(job)
        return None
    return int(job)


def close_windows_kill_job(handle: Optional[int]) -> None:
    if os.name == "nt" and handle:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            ctypes.c_void_p(handle))
