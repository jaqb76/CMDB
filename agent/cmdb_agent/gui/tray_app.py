"""Ikona agenta w zasobniku systemowym.

Model watkow (wazny, bo latwo go zepsuc):

  watek glowny  - petla tkinter (okna MUSZA powstawac i zyc w tym watku),
  watek ikony   - pystray z wlasna petla komunikatow Windows.

Menu ikony nie dotyka widgetow bezposrednio: wrzuca polecenie do kolejki,
ktora watek glowny odpytuje przez root.after(). To jedyny bezpieczny sposob
polaczenia dwoch petli zdarzen - wolanie tkinter prosto z watku ikony
konczy sie zawieszeniem albo cichym uszkodzeniem interfejsu.

Ikona nie wymaga uprawnien administratora: czyta wylacznie plik statusu,
a operacje wymagajace uprawnien (ustawienia, wymuszona synchronizacja)
uruchamia jako osobny, podniesiony proces.
"""
from __future__ import annotations

import logging
import queue
import threading
import subprocess
import sys
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from .. import status as status_module
from ..config import AgentConfig, default_config_path
from .common import is_windows, open_in_shell, run_agent_elevated, trigger_scheduled_task
from .status_window import StatusWindow
from ..proces import srodowisko_dla_potomka, flagi_bez_okna
from . import instance

log = logging.getLogger(__name__)

POLL_MS = 200
TRAY_REFRESH_MS = 30_000

# Kolor kropki w zasobniku - stan widoczny bez otwierania czegokolwiek.
ICON_COLORS = {
    "ok": (34, 139, 64),
    "error": (176, 38, 38),
    "offline": (200, 145, 20),
    "never": (120, 128, 140),
    "not_configured": (120, 128, 140),
}


def _make_icon_image(state: str):
    """Rysuje ikone proceduralnie - nie musimy dolaczac plikow graficznych."""
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = ICON_COLORS.get(state, ICON_COLORS["never"])

    draw.ellipse((4, 4, size - 4, size - 4), fill=color + (255,))
    # Stylizowana litera "C" - odrozniamy agenta od innych kolorowych kropek.
    draw.arc((18, 18, size - 18, size - 18), start=40, end=320, fill=(255, 255, 255, 255), width=7)
    return image


class TrayApp:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.commands: queue.Queue = queue.Queue()
        self.icon = None
        self._current_state = ""
        self.restart_requested = False
        self._executable_signature = self._file_signature()

        self.root = tk.Tk()
        self.root.withdraw()          # ikona zyje w zasobniku, nie na pasku zadan
        self.root.title("CMDB Agent")

        self.status_window = StatusWindow(
            self.root,
            config,
            on_sync=self.request_sync,
            on_settings=self.open_settings,
            on_open_log=self.open_log,
        )

    # --- polecenia z menu (wykonywane w watku glownym) --------------------

    def request_sync(self) -> None:
        """Wymusza przebieg. Najpierw zadanie harmonogramu (dziala jako SYSTEM
        i zbierze komplet danych), a gdy sie nie da - podniesienie uprawnien."""
        if is_windows() and trigger_scheduled_task():
            log.info("wymuszono przebieg zadania harmonogramu")
            return
        log.info("zadanie niedostepne - uruchamiam agenta z podniesieniem uprawnien")
        if not run_agent_elevated(["run"]):
            messagebox.showwarning(
                "Synchronizacja",
                "Nie udalo sie uruchomic synchronizacji.\n\n"
                "Agent zsynchronizuje sie samoczynnie przy nastepnym przebiegu "
                "zaplanowanego zadania.",
            )

    def open_settings(self) -> None:
        """Ustawienia wymagaja zapisu do katalogu programu, wiec zawsze
        uruchamiamy je jako osobny, podniesiony proces."""
        if not run_agent_elevated(["configure"], gui=True):
            messagebox.showwarning(
                "Ustawienia",
                "Zmiana ustawien wymaga uprawnien administratora.\n\n"
                "Uruchom ponownie i potwierdz monit systemu Windows.",
            )

    def run_diagnostics(self) -> None:
        """Diagnostyka jest tylko odczytem sieci - nie wymaga uprawnien."""
        if not self.config.server_url:
            messagebox.showinfo(
                "Diagnostyka", "Agent nie ma jeszcze ustawionego adresu serwera."
            )
            return
        from ..diagnose import render_report

        report, healthy = render_report(self.config.server_url, self.config.ca_bundle)
        if healthy:
            messagebox.showinfo("Diagnostyka polaczenia", report)
        else:
            messagebox.showwarning("Diagnostyka polaczenia", report)

    def open_log(self) -> None:
        log_path = self.config.data_dir / "agent.log"
        if not log_path.is_file():
            messagebox.showinfo("Dziennik", f"Dziennik nie istnieje jeszcze:\n{log_path}")
            return
        open_in_shell(log_path)

    def show_status(self) -> None:
        self.status_window.show()

    def quit(self) -> None:
        if self.icon is not None:
            self.icon.stop()
        self.root.quit()

    # --- most miedzy watkiem ikony a watkiem glownym ----------------------

    def _enqueue(self, name: str):
        def handler(*_args):
            self.commands.put(name)

        return handler

    def _poll_commands(self) -> None:
        try:
            while True:
                command = self.commands.get_nowait()
                handler = {
                    "status": self.show_status,
                    "sync": self.request_sync,
                    "settings": self.open_settings,
                    "doctor": self.run_diagnostics,
                    "log": self.open_log,
                    "quit": self.quit,
                }.get(command)
                if handler:
                    try:
                        handler()
                    except Exception:  # zaden blad menu nie moze ubic ikony
                        log.exception("blad obslugi polecenia '%s'", command)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._poll_commands)

    # --- ikona ------------------------------------------------------------

    def _build_icon(self):
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Status agenta...", self._enqueue("status"), default=True),
            pystray.MenuItem("Synchronizuj teraz", self._enqueue("sync")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Ustawienia...", self._enqueue("settings")),
            pystray.MenuItem("Sprawdz polaczenie", self._enqueue("doctor")),
            pystray.MenuItem("Pokaz dziennik", self._enqueue("log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Zakoncz", self._enqueue("quit")),
        )
        snapshot = status_module.read(self.config)
        self._current_state = snapshot.last_status
        return pystray.Icon(
            "cmdb-agent",
            _make_icon_image(snapshot.last_status),
            self._tooltip(snapshot),
            menu,
        )

    @staticmethod
    def _tooltip(snapshot) -> str:
        if snapshot.last_sync_at:
            when = status_module.format_relative(snapshot.last_sync_at)
            return f"CMDB Agent - ostatnia synchronizacja {when}"
        return "CMDB Agent - brak synchronizacji"

    def _refresh_icon(self) -> None:
        """Kolor i podpowiedz ikony odswiezane cyklicznie z pliku statusu."""
        try:
            signature = self._file_signature()
            if self._executable_signature is not None and signature is not None and signature != self._executable_signature:
                # Restart w tej samej sesji uzytkownika, nigdy jako SYSTEM.
                self.restart_requested = True
                self.quit()
                return
            snapshot = status_module.read(self.config)
            if self.icon is not None:
                self.icon.title = self._tooltip(snapshot)
                if snapshot.last_status != self._current_state:
                    self.icon.icon = _make_icon_image(snapshot.last_status)
                    self._current_state = snapshot.last_status
        except Exception:
            log.exception("nie udalo sie odswiezyc ikony")
        self.root.after(TRAY_REFRESH_MS, self._refresh_icon)

    @staticmethod
    def _file_signature():
        if sys.platform != "win32" or not getattr(sys, "frozen", False):
            return None
        try:
            info = Path(sys.executable).stat()
            return info.st_mtime_ns, info.st_size
        except OSError:
            return None

    # --- uruchomienie -----------------------------------------------------

    def run(self) -> int:
        self.icon = self._build_icon()
        threading.Thread(target=self.icon.run, name="cmdb-tray", daemon=True).start()

        self.root.after(POLL_MS, self._poll_commands)
        self.root.after(TRAY_REFRESH_MS, self._refresh_icon)

        # Takze pierwszy start jest cichy. Okna otwieraja tylko akcje menu.
        self.root.mainloop()
        return 0


def run_tray(config: AgentConfig) -> int:
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "brak bibliotek interfejsu graficznego (pystray, Pillow): " + str(exc)
        ) from exc

    token = instance.acquire()
    if token is False:
        return 0
    app = None
    try:
        log.info("uruchamiam ikone w zasobniku, konfiguracja: %s", default_config_path())
        app = TrayApp(config)
        result = app.run()
    finally:
        instance.release(token)
        if app is not None:
            app.root.destroy()
    if app.restart_requested:
        arguments = [sys.executable]
        if config._source_path:
            arguments += ["--config", str(config._source_path)]
        subprocess.Popen([*arguments, "gui"], creationflags=flagi_bez_okna(), env=srodowisko_dla_potomka())
    return result
