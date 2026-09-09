"""Okno statusu agenta.

Najwazniejsza informacja to data ostatniej POPRAWNEJ synchronizacji - czyli
kiedy dane w CMDB byly ostatnio aktualne. Celowo pokazujemy ja osobno od
daty ostatniej proby: agent moze probowac co godzine i za kazdym razem
dostawac odmowe, a wtedy "ostatnia proba: przed chwila" bylaby mylaca.
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk

from .. import __version__, status as status_module
from .appearance import apply_style

log = logging.getLogger(__name__)

REFRESH_MS = 5000

STATUS_COLORS = {
    "ok": "#1c6b34",
    "error": "#a52222",
    "offline": "#8a6100",
    "never": "#6b7280",
    "not_configured": "#6b7280",
}


class StatusWindow:
    """Okno statusu. Tworzone raz, potem tylko pokazywane i ukrywane."""

    def __init__(self, root: tk.Tk, config, on_sync, on_settings, on_open_log):
        self.root = root
        self.config = config
        self.on_sync = on_sync
        self.on_settings = on_settings
        self.on_open_log = on_open_log

        self.window: tk.Toplevel | None = None
        self.values: dict[str, tk.StringVar] = {}
        self._refresh_job = None

    # --- budowa -----------------------------------------------------------

    def _build(self) -> None:
        self.window = tk.Toplevel(self.root)
        icon = apply_style(self.window)
        self.window.title("CMDB Agent · Status")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        frame = ttk.Frame(self.window, padding=24)
        frame.grid(sticky="nsew")

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        if icon:
            ttk.Label(header, image=icon).pack(side="left", padx=(0, 12))
        title = ttk.Frame(header)
        title.pack(side="left")
        ttk.Label(title, text="CMDB Agent", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title, text="Inwentaryzacja i stan połączenia", style="Muted.TLabel").pack(anchor="w")
        self.version_label = ttk.Label(header, text=f"v{__version__}", style="Muted.TLabel")
        self.version_label.pack(side="right", padx=(25, 0))

        health = ttk.Frame(frame, style="Card.TFrame", padding=18)
        health.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.state_var = tk.StringVar()
        self.state_label = ttk.Label(health, textvariable=self.state_var, style="Card.TLabel", font=("Segoe UI", 14, "bold"))
        self.state_label.pack(anchor="w")
        self.values["last_sync"] = tk.StringVar(value="—")
        ttk.Label(health, text="Ostatnia poprawna synchronizacja", style="CardMuted.TLabel").pack(anchor="w", pady=(12, 3))
        ttk.Label(health, textvariable=self.values["last_sync"], style="Card.TLabel").pack(anchor="w")

        detail = ttk.Frame(frame, style="Card.TFrame", padding=18)
        detail.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        rows = [("machine", "Komputer"), ("tenant", "Firma"), ("server", "Serwer CMDB"),
                ("last_attempt", "Ostatnia próba"), ("next_sync", "Planowana synchronizacja"),
                ("monitoring", "Monitorowanie usług")]
        for index, (key, label) in enumerate(rows):
            ttk.Label(detail, text=label, style="CardMuted.TLabel").grid(row=index, column=0, sticky="nw", pady=5, padx=(0, 22))
            self.values[key] = tk.StringVar(value="—")
            ttk.Label(detail, textvariable=self.values[key], style="Card.TLabel", wraplength=365).grid(row=index, column=1, sticky="w", pady=5)

        self.problem_var = tk.StringVar()
        self.problem_label = ttk.Label(frame, textvariable=self.problem_var, wraplength=580, foreground="#946000")
        self.problem_label.grid(row=4, column=0, sticky="w", pady=(12, 0))
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="we", pady=(18, 0))
        self.sync_button = ttk.Button(buttons, text="Synchronizuj dane", command=self._sync_clicked, style="Primary.TButton")
        self.sync_button.pack(side="left")
        ttk.Button(buttons, text="Ustawienia", command=self.on_settings).pack(side="left", padx=8)
        ttk.Button(buttons, text="Dziennik", command=self.on_open_log).pack(side="left")
        ttk.Button(buttons, text="Ukryj", command=self.hide).pack(side="right", padx=(20, 0))
        self._center()

    def _center(self) -> None:
        self.window.update_idletasks()
        width = self.window.winfo_width()
        height = self.window.winfo_height()
        x = (self.window.winfo_screenwidth() - width) // 2
        y = (self.window.winfo_screenheight() - height) // 3
        self.window.geometry(f"+{x}+{y}")

    # --- odswiezanie ------------------------------------------------------

    def refresh(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            return
        if self._refresh_job is not None:
            self.window.after_cancel(self._refresh_job)
            self._refresh_job = None
        snapshot = status_module.read(self.config)

        stale = status_module.is_stale(snapshot)
        self.state_var.set("Brak świeżej synchronizacji" if stale else snapshot.status_label.capitalize())
        self.state_label.configure(foreground="#946000" if stale else STATUS_COLORS.get(snapshot.last_status, "#333"))

        if snapshot.last_sync_at:
            self.values["last_sync"].set(
                f"{status_module.format_local(snapshot.last_sync_at)}"
                f"  ({status_module.format_relative(snapshot.last_sync_at)})"
            )
        else:
            self.values["last_sync"].set("jeszcze nie bylo udanej synchronizacji")

        if snapshot.last_attempt_at and snapshot.last_attempt_at != snapshot.last_sync_at:
            self.values["last_attempt"].set(
                f"{status_module.format_local(snapshot.last_attempt_at)}"
                f"  ({status_module.format_relative(snapshot.last_attempt_at)})"
            )
        else:
            self.values["last_attempt"].set("-")

        self.values["next_sync"].set(
            status_module.format_local(snapshot.next_sync_estimate)
            if snapshot.next_sync_estimate
            else "-"
        )
        self.values["machine"].set(snapshot.hostname or "-")
        self.values["tenant"].set(snapshot.tenant_slug or "-")
        self.values["server"].set(snapshot.server_url or "(nie ustawiono)")
        self.values["monitoring"].set(status_module.monitoring_label(snapshot.monitoring))

        problems = list(snapshot.warnings)
        if snapshot.last_error:
            problems.insert(0, snapshot.last_error)
        self.problem_var.set("\n".join(problems))

        self._refresh_job = self.window.after(REFRESH_MS, self.refresh)

    def _sync_clicked(self) -> None:
        self.sync_button.configure(state="disabled", text="Synchronizuje...")
        self.on_sync()
        # Wynik pojawi sie w pliku statusu - przycisk odblokowujemy po chwili.
        self.window.after(4000, self._restore_sync_button)

    def _restore_sync_button(self) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.sync_button.configure(state="normal", text="Synchronizuj dane")
            self.refresh()

    # --- widocznosc -------------------------------------------------------

    def show(self) -> None:
        if self.window is None or not self.window.winfo_exists():
            self._build()
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.refresh()

    def hide(self) -> None:
        if self._refresh_job is not None and self.window is not None:
            try:
                self.window.after_cancel(self._refresh_job)
            except tk.TclError:
                pass
            self._refresh_job = None
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()
