"""Okno konfiguracji agenta: adres serwera i token rejestracyjny.

Uruchamiane przy pierwszym starcie (agent nieskonfigurowany) oraz z menu
ikony w zasobniku. Zapisuje konfiguracje i od razu rejestruje maszyne,
zeby uzytkownik od razu wiedzial, czy dane sa poprawne - zamiast czekac
do pierwszego przebiegu zadania.

Wymaga uprawnien administratora (zapis do katalogu programu), dlatego
uruchamiane jest w osobnym, podniesionym procesie.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..config import AgentConfig, default_config_path
from ..state import load_state
from .common import is_admin, run_agent

log = logging.getLogger(__name__)

PADDING = {"padx": 12, "pady": 6}


class SettingsWindow:
    def __init__(self, config: AgentConfig, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path or default_config_path()
        self.saved = False
        self.results: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title("CMDB Agent - konfiguracja")
        self.root.resizable(False, False)
        self.root.minsize(560, 0)

        self.server_var = tk.StringVar(value=config.server_url)
        self.token_var = tk.StringVar(value=config.enrollment_token)
        self.ca_var = tk.StringVar(value=config.ca_bundle or "")
        # Przy swiezej konfiguracji proponujemy 4 h - tyle samo, co domyslnie w instalatorze.
        default_hours = max(1, config.report_interval_seconds // 3600) if config.server_url else 4
        self.interval_var = tk.StringVar(value=str(default_hours))
        self.processes_var = tk.BooleanVar(value=config.collect_processes)
        self.show_token_var = tk.BooleanVar(value=False)
        self.message_var = tk.StringVar(value="")

        self._build()
        self._center()

    # --- budowa okna ------------------------------------------------------

    def _build(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.grid(sticky="nsew")

        ttk.Label(
            frame,
            text="Podaj adres serwera CMDB i token otrzymany od administratora.",
            wraplength=520,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(frame, text="Adres serwera").grid(row=1, column=0, sticky="w", pady=4, padx=(0, 14))
        server_entry = ttk.Entry(frame, textvariable=self.server_var, width=48)
        server_entry.grid(row=1, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Label(
            frame, text="np. https://cmdb.twojafirma.pl  (wymagane https)", foreground="#666"
        ).grid(row=2, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="Token rejestracyjny").grid(row=3, column=0, sticky="w", pady=4, padx=(0, 14))
        self.token_entry = ttk.Entry(frame, textvariable=self.token_var, width=48, show="*")
        self.token_entry.grid(row=3, column=1, columnspan=2, sticky="we", pady=4)
        ttk.Checkbutton(
            frame, text="Pokaz token", variable=self.show_token_var, command=self._toggle_token
        ).grid(row=4, column=1, sticky="w")

        ttk.Separator(frame, orient="horizontal").grid(
            row=5, column=0, columnspan=3, sticky="we", pady=12
        )

        ttk.Label(frame, text="Certyfikat CA").grid(row=6, column=0, sticky="w", pady=4, padx=(0, 14))
        ttk.Entry(frame, textvariable=self.ca_var, width=38).grid(row=6, column=1, sticky="we", pady=4)
        ttk.Button(frame, text="Wybierz...", command=self._pick_ca).grid(row=6, column=2, sticky="w")
        ttk.Label(
            frame,
            text="tylko gdy serwer ma certyfikat wewnetrznego CA firmy",
            foreground="#666",
        ).grid(row=7, column=1, columnspan=2, sticky="w")

        ttk.Label(frame, text="Co ile godzin").grid(row=8, column=0, sticky="w", pady=4, padx=(0, 14))
        ttk.Spinbox(frame, from_=1, to=24, textvariable=self.interval_var, width=6).grid(
            row=8, column=1, sticky="w", pady=4
        )

        ttk.Checkbutton(
            frame, text="Zbieraj liste uruchomionych procesow", variable=self.processes_var
        ).grid(row=9, column=1, columnspan=2, sticky="w", pady=4)

        self.message = ttk.Label(frame, textvariable=self.message_var, wraplength=520)
        self.message.grid(row=10, column=0, columnspan=3, sticky="w", pady=(12, 4))

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=520)

        buttons = ttk.Frame(frame)
        buttons.grid(row=12, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self.save_button = ttk.Button(
            buttons, text="Zapisz i zarejestruj", command=self._on_save
        )
        self.save_button.pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Anuluj", command=self.root.destroy).pack(side="right")

        frame.columnconfigure(1, weight=1)
        server_entry.focus_set()

        if not is_admin():
            self._set_message(
                "Uwaga: okno nie ma uprawnien administratora - zapis konfiguracji "
                "moze sie nie powiesc.",
                "#8a6100",
            )

    def _center(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{x}+{y}")

    def _toggle_token(self) -> None:
        self.token_entry.configure(show="" if self.show_token_var.get() else "*")

    def _pick_ca(self) -> None:
        selected = filedialog.askopenfilename(
            title="Wybierz plik certyfikatu CA",
            filetypes=[("Certyfikaty", "*.pem *.crt *.cer"), ("Wszystkie pliki", "*.*")],
        )
        if selected:
            self.ca_var.set(selected)

    def _set_message(self, text: str, color: str = "#333") -> None:
        self.message_var.set(text)
        self.message.configure(foreground=color)

    # --- walidacja i zapis ------------------------------------------------

    def _collect(self) -> AgentConfig | None:
        server = self.server_var.get().strip().rstrip("/")
        token = self.token_var.get().strip()

        if not server:
            self._set_message("Podaj adres serwera.", "#a52222")
            return None
        if not server.startswith("https://"):
            self._set_message(
                "Adres musi zaczynac sie od https:// - agent nie wysyla danych "
                "ani tokenu po nieszyfrowanym polaczeniu.",
                "#a52222",
            )
            return None
        if not token:
            self._set_message("Podaj token rejestracyjny otrzymany od administratora.", "#a52222")
            return None
        if not token.startswith("cmdb_ent_"):
            self._set_message(
                "To nie wyglada na token rejestracyjny - powinien zaczynac sie od 'cmdb_ent_'.",
                "#a52222",
            )
            return None

        ca = self.ca_var.get().strip()
        if ca and not Path(ca).is_file():
            self._set_message(f"Nie znaleziono pliku certyfikatu: {ca}", "#a52222")
            return None

        try:
            hours = max(1, min(24, int(self.interval_var.get())))
        except ValueError:
            self._set_message("Odstep miedzy raportami musi byc liczba godzin.", "#a52222")
            return None

        candidate = AgentConfig(
            server_url=server,
            enrollment_token=token,
            ca_bundle=ca or None,
            report_interval_seconds=hours * 3600,
            collect_processes=self.processes_var.get(),
            data_dir=self.config.data_dir,
        )
        try:
            candidate.validate()
        except ValueError as exc:
            self._set_message(str(exc), "#a52222")
            return None
        return candidate

    def _write_config(self, candidate: AgentConfig) -> bool:
        payload = {
            "server_url": candidate.server_url,
            "enrollment_token": candidate.enrollment_token,
            "report_interval_seconds": candidate.report_interval_seconds,
            "collect_processes": candidate.collect_processes,
            "log_level": self.config.log_level,
        }
        if candidate.ca_bundle:
            payload["ca_bundle"] = candidate.ca_bundle
        if self.config.pin_sha256:
            payload["pin_sha256"] = self.config.pin_sha256
        if str(candidate.data_dir) != str(AgentConfig().data_dir):
            payload["data_dir"] = str(candidate.data_dir)

        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return True
        except OSError as exc:
            messagebox.showerror(
                "Brak uprawnien",
                f"Nie moge zapisac konfiguracji w:\n{self.config_path}\n\n{exc}\n\n"
                "Uruchom konfiguracje jako administrator.",
            )
            return False

    def _on_save(self) -> None:
        candidate = self._collect()
        if candidate is None:
            return
        if not self._write_config(candidate):
            return

        self.save_button.configure(state="disabled")
        self.progress.grid(row=11, column=0, columnspan=3, sticky="we")
        self.progress.start(12)
        self._set_message("Rejestruje maszyne w serwerze...", "#333")

        # Rejestracja idzie przez siec - w osobnym watku, zeby okno nie zamarlo.
        threading.Thread(target=self._enroll_worker, daemon=True).start()
        self._poll_enroll_result()

    def _enroll_worker(self) -> None:
        """Watek roboczy NIE dotyka widgetow - wynik wraca kolejka.

        Tkinter nie jest wielowatkowy: wolanie root.after() spoza watku
        z petla glowna potrafi rzucic "main thread is not in main loop"
        albo po cichu uszkodzic interfejs. Kolejka odpytywana z watku
        glownego to jedyny bezpieczny most.
        """
        try:
            result = run_agent(["enroll"], self.config_path, timeout=180)
            output = (result.stderr or b"").decode("utf-8", errors="replace")
            self.results.put((result.returncode, output))
        except Exception as exc:  # okno nie moze zniknac bez komunikatu
            self.results.put((1, str(exc)))

    def _poll_enroll_result(self) -> None:
        try:
            returncode, output = self.results.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll_enroll_result)
            return
        self._enroll_done(returncode, output)

    def _enroll_done(self, returncode: int, output: str) -> None:
        self.progress.stop()
        self.progress.grid_remove()
        self.save_button.configure(state="normal")

        if returncode == 0:
            self.saved = True
            state = load_state(self.config.state_path)
            firma = state.tenant_slug or "(nieznana)"
            messagebox.showinfo(
                "Gotowe",
                f"Maszyna zostala zarejestrowana w firmie: {firma}\n\n"
                "Agent bedzie raportowal automatycznie zgodnie z harmonogramem.",
            )
            self.root.destroy()
            return

        reason = _explain_failure(returncode, output)
        self._set_message(reason, "#a52222")
        messagebox.showerror("Rejestracja nie powiodla sie", reason)

    def run(self) -> bool:
        self.root.mainloop()
        return self.saved


def _explain_failure(returncode: int, output: str) -> str:
    """Zamienia kod wyjscia i log na komunikat zrozumialy dla uzytkownika."""
    lowered = output.lower()
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return (
            "Nie udalo sie zweryfikowac certyfikatu serwera. Jesli serwer uzywa "
            "certyfikatu wewnetrznego CA firmy, wskaz plik CA w polu 'Certyfikat CA'."
        )
    if returncode == 2 or "401" in output:
        return (
            "Serwer odrzucil token. Sprawdz, czy token nie zostal wycofany "
            "i czy skopiowales go w calosci."
        )
    if returncode == 3:
        return (
            "Brak lacznosci z serwerem. Sprawdz adres, polaczenie sieciowe "
            "i czy zapora przepuszcza ruch HTTPS do serwera CMDB."
        )
    if returncode == 4:
        return (
            "Odcisk certyfikatu serwera nie zgadza sie z przypietym. "
            "Skontaktuj sie z administratorem - moze to oznaczac podszycie sie pod serwer."
        )
    tail = output.strip().splitlines()[-1] if output.strip() else "brak szczegolow"
    return f"Rejestracja nie powiodla sie (kod {returncode}): {tail}"


def open_settings(config: AgentConfig, config_path: Path | None = None) -> bool:
    return SettingsWindow(config, config_path).run()
