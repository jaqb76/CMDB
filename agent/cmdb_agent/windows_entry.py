"""Jeden windowed EXE: bez argumentow tray, z argumentami dotychczasowe CLI.

Python w trybie windowed ustawia stdout/stderr na None. Odtwarzamy jedynie
odziedziczone potoki/pliki; nigdy nie tworzymy ani nie pokazujemy konsoli.
"""
from __future__ import annotations

import codecs
import os
import sys
import unicodedata

# Znaki typograficzne, ktorych nie ma w stronach kodowych DOS. Zwykle "?" nie
# niesie zadnej tresci, a mysnik zamiast kropki srodkowej czyta sie tak samo.
ZAMIENNIKI = {"\u00b7": "-", "\u2013": "-", "\u2014": "-", "\u2026": "...",
              "\u2192": "->", "\u201e": '"', "\u201d": '"', "\u2019": "'",
              "\u00d7": "x", "\u00a0": " "}
NAZWA_BLEDU = "cmdb-konsola"


# Litery, ktorych rozklad Unicode nie sprowadza do ASCII.
LITERY = {"\u0142": "l", "\u0141": "L", "\u00df": "ss", "\u00f8": "o", "\u00d8": "O"}


def _odpowiednik(znak: str) -> str:
    if znak in ZAMIENNIKI:
        return ZAMIENNIKI[znak]
    if znak in LITERY:
        return LITERY[znak]
    # "ą" -> "a", "é" -> "e": litera bez ogonka czyta sie lepiej niz "?".
    bazowy = unicodedata.normalize("NFKD", znak).encode("ascii", "ignore").decode("ascii")
    return bazowy or "?"


def _zastap_typografie(blad):
    tekst = blad.object[blad.start:blad.end]
    return "".join(_odpowiednik(znak) for znak in tekst), blad.end


codecs.register_error(NAZWA_BLEDU, _zastap_typografie)


def kodowanie_dla_strony(strona: int) -> str:
    """Nazwa kodeka dla strony kodowej konsoli; nieznana oznacza UTF-8.

    Zero dostajemy, gdy proces nie ma konsoli, a 65001 to UTF-8 pod inna
    nazwa - oba przypadki lapie ten sam wyjatek.
    """
    try:
        return codecs.lookup("cp%d" % strona).name
    except (LookupError, ValueError):
        return "utf-8"


def _kodowanie_wyjscia(kernel, handle):
    """Konsola Windows dekoduje BAJTY wedlug wlasnej strony kodowej.

    Strumien odtworzony na surowym uchwycie omija WriteConsoleW, wiec nie ma
    tu automatycznej konwersji Unicode - to, co wypiszemy, konsola przeczyta
    przez GetConsoleOutputCP. Polski Windows stoi zwykle na stronie 852, a my
    wysylalismy UTF-8; stad "Dziala" i "usluga" rozsypywaly sie w oknie
    PowerShella, mimo ze sam tekst byl poprawny.

    Przekierowanie do pliku albo potoku konsola nie jest i zostaje przy UTF-8:
    tam bajty czyta nastepny program, a nie sterownik ekranu.
    """
    try:
        # Import w srodku: ctypes.wintypes istnieje tylko na Windows, a ta
        # funkcja ma zwrocic kodowanie, nie wysadzic startu agenta.
        import ctypes
        from ctypes import wintypes

        kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetConsoleMode.restype = wintypes.BOOL
        if not kernel.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD())):
            return "utf-8"
        kernel.GetConsoleOutputCP.restype = wintypes.UINT
        return kodowanie_dla_strony(kernel.GetConsoleOutputCP())
    except (AttributeError, OSError, ValueError):
        return "utf-8"


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
    return os.fdopen(fd, "w", encoding=_kodowanie_wyjscia(kernel, handle),
                     errors=NAZWA_BLEDU, buffering=1)


def restore_output():
    if sys.platform != "win32":
        return
    for name, number in (("stdout", -11), ("stderr", -12)):
        obecny = getattr(sys, name)
        if obecny is None:
            try:
                stream = _inherited_output(number)
            except OSError:
                stream = None
            setattr(sys, name, stream or open(os.devnull, "w", encoding="utf-8"))
        else:
            _bezpieczne_znaki(obecny)


def _bezpieczne_znaki(strumien) -> None:
    """Strumien, ktory Python otworzyl sam, nie moze wywracac agenta na "ł".

    Gdy proces dostaje odziedziczony potok (instalator uruchamia "status"
    i czyta jego wyjscie), Python tworzy stdout w kodowaniu systemu (cp1252)
    w trybie "strict". Pierwszy polski znak konczyl sie wtedy wyjatkiem
    i oknem "Unhandled exception" na koncu instalacji.
    """
    try:
        strumien.reconfigure(errors=NAZWA_BLEDU)
    except (AttributeError, ValueError, OSError):
        pass


def main(argv=None):
    restore_output()
    from .main import main as agent_main
    arguments = list(sys.argv[1:] if argv is None else argv)
    return agent_main(arguments or (["gui"] if sys.platform == "win32" else []))
