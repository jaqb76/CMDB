"""Shared native-window branding; the same C mark as the existing tray."""
from __future__ import annotations

from tkinter import ttk

BG = "#f3f6fa"
INK = "#172b3a"
MUTED = "#5b6d7c"
GREEN = "#228b40"


def icon_image(color=GREEN, size=64):
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=color)
    draw.arc((18, 18, 46, 46), start=40, end=320, fill="white", width=7)
    return image.resize((size, size), Image.Resampling.LANCZOS)


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
    style.configure("Primary.TButton", background=GREEN, foreground="white", borderwidth=0)
    style.map("Primary.TButton", background=[("active", "#176f32"), ("disabled", "#dce9e0")],
              foreground=[("disabled", "#627469")])
    style.configure("TEntry", padding=7, fieldbackground="white")
    style.configure("TSpinbox", padding=6, fieldbackground="white")
    style.configure("TLabelframe", padding=12, background=BG, bordercolor="#dce4ec")
    style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"), foreground=MUTED)
    return brand_window(window)
