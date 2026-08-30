"""Jeden windowed EXE: bez argumentow tray, z argumentami dotychczasowe CLI.

Python w trybie windowed ustawia stdout/stderr na None. Odtwarzamy jedynie
odziedziczone potoki/pliki; nigdy nie tworzymy ani nie pokazujemy konsoli.
"""
from __future__ import annotations

import os
import sys


def _inherited_output(number):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.GetStdHandle(number & 0xFFFFFFFF)
    if not handle or handle == wintypes.HANDLE(-1).value or kernel.GetFileType(handle) == 0:
        return None
    duplicate = wintypes.HANDLE()
    process = kernel.GetCurrentProcess()
    if not kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
        return None
    try:
        fd = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(duplicate)
        return None
    return os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1)


def restore_output():
    if sys.platform != "win32":
        return
    for name, number in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is None:
            try:
                stream = _inherited_output(number)
            except OSError:
                stream = None
            setattr(sys, name, stream or open(os.devnull, "w", encoding="utf-8"))


def main(argv=None):
    restore_output()
    from .main import main as agent_main
    arguments = list(sys.argv[1:] if argv is None else argv)
    return agent_main(arguments or (["gui"] if sys.platform == "win32" else []))
