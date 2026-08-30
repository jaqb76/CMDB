"""Jedna ikona na sesje Windows i katalog instalacji; worker nie bierze blokady."""
from __future__ import annotations

import ctypes
import hashlib
import sys
from pathlib import Path


def acquire():
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    identity = str(Path(sys.executable).resolve().parent).casefold().encode("utf-8")
    name = "Local\\CMDB.Agent.Tray." + hashlib.sha256(identity).hexdigest()[:24]
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "Nie mozna utworzyc blokady ikony")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return False
    return kernel, handle


def release(token):
    if token:
        kernel, handle = token
        kernel.CloseHandle(handle)
