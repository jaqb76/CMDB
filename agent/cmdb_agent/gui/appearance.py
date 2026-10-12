"""Shared native-window branding: the Nexassert mark used by windows, tray and EXE."""
from __future__ import annotations

import sys
from tkinter import ttk

# Windows grupuje okna na pasku zadan po identyfikatorze aplikacji i stamtad
# bierze ikone przycisku. Proces, ktory go nie deklaruje, dostaje identyfikator
# wyliczony przez powloke - i przycisk na pasku pokazuje ikone domyslna zamiast
# naszej, mimo poprawnie ustawionego iconphoto i ikony wkompilowanej w EXE.
# Ten sam ciag musi byc w skrocie instalatora, inaczej przypiety skrot i
# dzialajace okno beda dwoma osobnymi przyciskami.
APP_ID = "CMDB.Agent.Tray"


def use_app_identity() -> None:
    """Deklaruje identyfikator aplikacji. Musi poprzedzic pierwsze okno."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        # Starsza powloka albo brak shell32 - ikona bedzie domyslna,
        # ale to nie powod, zeby agent sie nie uruchomil.
        pass

BG = "#f3f6fa"
INK = "#142a43"
MUTED = "#5b6d7c"

# Barwy marki Nexassert. Te same wartosci sa w app.css portalu i w
# ModernUi.kt aplikacji - zmiana tylko w jednym miejscu rozjedzie wyglad.
NAVY = "#142a43"
BLUE = "#167cba"
TEAL = "#23b8b0"
BLUE_ACTIVE = "#106297"

# Znak "NA" w siatce 64x64: cztery wstegi (N i A dziela jedno ramie)
# i poprzeczka litery A. Te same wspolrzedne maja logo.svg portalu
# i ic_launcher_foreground.xml aplikacji.
MARK_RIBBONS = (
    ((4, 50), (13, 50), (18, 14), (9, 14)),
    ((9, 14), (18, 14), (33, 50), (24, 50)),
    ((24, 50), (33, 50), (46, 14), (37, 14)),
    ((37, 14), (46, 14), (60, 50), (51, 50)),
    ((36, 35), (49, 35), (51, 40), (34, 40)),
)
# Wstegi biegnace w dol leza "pod spodem" - przyciemnienie daje efekt zalamania.
MARK_SHADED = (1, 3)


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))


def mark_image(size=64, tile=True):
    """Znak marki; tile=True kladzie go na granatowym kafelku jak ikone aplikacji."""
    from PIL import Image, ImageDraw

    scale = 8                                   # rysujemy wiekszy i zmniejszamy - gladkie krawedzie
    big = 64 * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    if tile:
        ImageDraw.Draw(image).rounded_rectangle((0, 0, big - 1, big - 1), radius=14 * scale, fill=_hex(NAVY))
        inset, k = 7 * scale, 50 / 64           # znak mniejszy, zeby nie dotykal krawedzi kafelka
    else:
        inset, k = 0, 1.0

    gradient = Image.new("RGBA", (big, big))
    a, b = _hex(BLUE), _hex(TEAL)
    gdraw = ImageDraw.Draw(gradient)
    for x in range(big):
        t = x / (big - 1)
        gdraw.line((x, 0, x, big), fill=tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3)) + (255,))

    def pts(poly):
        return [(inset + px * scale * k, inset + py * scale * k) for px, py in poly]

    mask = Image.new("L", (big, big), 0)
    mdraw = ImageDraw.Draw(mask)
    for poly in MARK_RIBBONS:
        mdraw.polygon(pts(poly), fill=255)
    image.paste(gradient, (0, 0), mask)

    shade = Image.new("L", (big, big), 0)
    sdraw = ImageDraw.Draw(shade)
    for i in MARK_SHADED:
        sdraw.polygon(pts(MARK_RIBBONS[i]), fill=45)
    image.paste(Image.new("RGBA", (big, big), (6, 20, 40, 255)), (0, 0), shade)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def icon_image(size=64):
    return mark_image(size=size, tile=True)


def brand_window(window):
    """Retain PhotoImage references: Tk otherwise releases the window icon."""
    try:
        from PIL import ImageTk
        window._cmdb_icons = [ImageTk.PhotoImage(icon_image(size=s), master=window) for s in (16, 32, 64)]
        window.iconphoto(True, *window._cmdb_icons)
        return window._cmdb_icons[-1]
    except ImportError:
        return None


def apply_style(window):
    use_app_identity()
    window.configure(background=BG)
    style = ttk.Style(window)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), foreground=INK, background=BG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG)
    style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground=INK)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Card.TFrame", background="white")
    style.configure("Card.TLabel", background="white", foreground=INK)
    style.configure("CardMuted.TLabel", background="white", foreground=MUTED)
    style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"), background="white", foreground=MUTED)
    style.configure("TButton", padding=(12, 9), background="white", borderwidth=1)
    style.map("TButton", background=[("active", "#e7edf3")])
    style.configure("Primary.TButton", background=BLUE, foreground="white", borderwidth=0)
    style.map("Primary.TButton", background=[("active", BLUE_ACTIVE), ("disabled", "#d8e5ef")],
              foreground=[("disabled", "#5f7385")])
    style.configure("TEntry", padding=7, fieldbackground="white")
    style.configure("TSpinbox", padding=6, fieldbackground="white")
    style.configure("TLabelframe", padding=12, background=BG, bordercolor="#dce4ec")
    style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground=MUTED)
    return brand_window(window)
