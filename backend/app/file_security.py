"""Best-effort private permissions for local state and secret files."""
from __future__ import annotations

import ctypes
import os


def restrict_path_to_owner(path: str, *, directory: bool = False) -> None:
    """Restrict a path to its owner, SYSTEM, and local administrators.

    Security setup is intentionally best effort for filesystems that do not
    implement POSIX modes or Windows ACLs. Callers should still place secrets
    below a per-user state directory.
    """
    if not os.path.exists(path):
        return
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    _restrict_windows_path(path, directory=directory)


def _restrict_windows_path(path: str, *, directory: bool) -> None:
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    owner = ctypes.c_void_p()
    source_descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        os.path.abspath(path), 1, 0x00000001, ctypes.byref(owner),
        None, None, None, ctypes.byref(source_descriptor),
    )
    if result != 0 or not owner:
        if source_descriptor:
            kernel32.LocalFree(source_descriptor)
        raise OSError(result or ctypes.get_last_error(),
                      f"cannot read owner ACL for {path}")

    owner_text = wintypes.LPWSTR()
    descriptor = ctypes.c_void_p()
    try:
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        inherit = "OICI" if directory else ""
        sddl = (
            f"D:P(A;{inherit};FA;;;{owner_text.value})"
            f"(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)"
        )
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # Set and explicitly protect the DACL from permissive parent entries.
        if not advapi32.SetFileSecurityW(
                os.path.abspath(path), 0x00000004 | 0x80000000, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)
        if owner_text:
            kernel32.LocalFree(owner_text)
        if source_descriptor:
            kernel32.LocalFree(source_descriptor)
