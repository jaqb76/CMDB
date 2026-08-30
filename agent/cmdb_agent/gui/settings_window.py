"""Okno konfiguracji agenta: adres serwera i token rejestracyjny.

Uruchamiane na wyrazne zadanie uzytkownika z menu
ikony w zasobniku. Zapisuje konfiguracje i od razu rejestruje maszyne,
zeby uzytkownik od razu wiedzial, czy dane sa poprawne - zamiast czekac
do pierwszego przebiegu zadania.

Wymaga uprawnien administratora (zapis do katalogu programu), dlatego
uruchamiane jest w osobnym, podniesionym procesie.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from uuid import uuid4
import logging
import queue
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..config import (
    INTERACTIVE_MAX_RETRIES,
    INTERACTIVE_PROCESS_TIMEOUT,
    INTERACTIVE_TIMEOUT_SECONDS,
    AgentConfig,
    default_config_path,
)
from ..state import load_state, save_state, _harden_file
from .common import is_admin, run_agent

log = logging.getLogger(__name__)

PADDING = {"padx": 12, "pady": 6}

# Budzet rejestracji z okna - stale w config.py, zeby dalo sie je sprawdzic
# testem bez uruchamiania interfejsu graficznego.
INTERACTIVE_ENV = {
    "CMDB_AGENT_TIMEOUT": str(INTERACTIVE_TIMEOUT_SECONDS),
    "CMDB_AGENT_MAX_RETRIES": str(INTERACTIVE_MAX_RETRIES),
}
ENROLL_PROCESS_TIMEOUT = INTERACTIVE_PROCESS_TIMEOUT
TIMEOUT_RETURNCODE = -1


class SettingsWindow:
    def __init__(self, config: AgentConfig, config_path: Path | None = None):
        self.config = config
        self.config_path = config_path or default_config_path()
        self.saved = False
        self.results: queue.Queue = queue.Queue()
        self.check_results: queue.Queue = queue.Queue()

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
        self.discovery_var = tk.BooleanVar(value=config.discovery_enabled)
        self.discovery_auto_var = tk.BooleanVar(value=config.discovery_auto_subnets)
        self.discovery_cidrs_var = tk.StringVar(value=", ".join(config.discovery_cidrs))
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

        discovery = ttk.LabelFrame(frame, text="Wykrywanie urzadzen w sieci", padding=8)
        discovery.grid(row=10, column=0, columnspan=3, sticky="we", pady=(12, 4))
        ttk.Checkbutton(discovery, text="Wlacz skanowanie sieci (mam zgode administratora sieci)",
                        variable=self.discovery_var).pack(anchor="w")
        ttk.Checkbutton(discovery, text="Automatycznie wykryj lokalne prywatne podsieci IPv4",
                        variable=self.discovery_auto_var).pack(anchor="w")
        ttk.Label(discovery, text="Dodatkowe zakresy CIDR, oddzielone przecinkiem:").pack(anchor="w")
        ttk.Entry(discovery, textvariable=self.discovery_cidrs_var, width=64).pack(fill="x")
        ttk.Label(discovery, text="Np. 192.168.10.0/24. Tylko dostepne prywatne sieci IPv4. "
                  "Domyslnie co 24 h, do 1024 adresow i 5 min. Wyniki: CMDB → Wykrywanie sieci. "
                  "Pierwszy skan: nastepna synchronizacja. Typ i OS wymagaja weryfikacji.",
                  wraplength=500, foreground="#666").pack(anchor="w", pady=(6, 0))

        self.message = ttk.Label(frame, textvariable=self.message_var, wraplength=520)
        self.message.grid(row=11, column=0, columnspan=3, sticky="w", pady=(12, 4))

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=520)

        buttons = ttk.Frame(frame)
        buttons.grid(row=13, column=0, columnspan=3, sticky="we", pady=(12, 0))
        # Sprawdzenie polaczenia nie zapisuje niczego - mozna go uzyc, zanim
        # zdecydujemy sie na rejestracje.
        self.check_button = ttk.Button(
            buttons, text="Sprawdz polaczenie", command=self._on_check
        )
        self.check_button.pack(side="left")
        self.save_button = ttk.Button(
            buttons, text="Zapisz", command=self._on_save
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
        enrolled = self._already_enrolled(server)
        if not token and not enrolled:
            self._set_message("Podaj token rejestracyjny otrzymany od administratora.", "#a52222")
            return None
        if token and not token.startswith("cmdb_ent_"):
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

        candidate = replace(self.config,
            server_url=server,
            enrollment_token=token,
            ca_bundle=ca or None,
            report_interval_seconds=hours * 3600,
            collect_processes=self.processes_var.get(),
            discovery_enabled=self.discovery_var.get(),
            discovery_auto_subnets=self.discovery_auto_var.get(),
            discovery_cidrs=[v.strip() for v in self.discovery_cidrs_var.get().split(",") if v.strip()],
            data_dir=self.config.data_dir,
        )
        try:
            candidate.validate()
        except ValueError as exc:
            self._set_message(str(exc), "#a52222")
            return None
        return candidate

    def _already_enrolled(self, server: str) -> bool:
        state = load_state(self.config.state_path)
        return state.is_enrolled and state.server_url.rstrip("/") == server.rstrip("/")

    def _write_config(self, candidate: AgentConfig) -> bool:
        tmp = self.config_path.with_name(f".{self.config_path.name}.{uuid4().hex}.tmp")
        try:
            # Zachowaj takze ustawienia nieznane starszemu oknu konfiguracji.
            try:
                payload = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                payload = {}
            payload.update({k: str(v) if isinstance(v, Path) else v for k, v in asdict(candidate).items()
                            if not k.startswith("_") and k != "config_access_denied"})
            if not candidate.enrollment_token:
                payload.pop("enrollment_token", None)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                _harden_file(tmp)
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.replace(tmp, self.config_path)
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror("Zapis konfiguracji", f"Nie moge zapisac konfiguracji: {exc}")
            return False
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _on_check(self) -> None:
        """Diagnostyka polaczenia - bez zapisywania konfiguracji."""
        server = self.server_var.get().strip().rstrip("/")
        if not server.startswith("https://"):
            self._set_message("Podaj adres serwera zaczynajacy sie od https://", "#a52222")
            return

        self.check_button.configure(state="disabled", text="Sprawdzam...")
        self._set_message("Sprawdzam polaczenie...", "#333")
        ca = self.ca_var.get().strip() or None
        threading.Thread(
            target=self._check_worker, args=(server, ca), daemon=True
        ).start()
        self._poll_check_result()

    def _check_worker(self, server: str, ca: str | None) -> None:
        from ..diagnose import render_report

        try:
            report, healthy = render_report(server, ca)
            self.check_results.put((report, healthy))
        except Exception as exc:
            self.check_results.put((f"Diagnostyka nie powiodla sie: {exc}", False))

    def _poll_check_result(self) -> None:
        try:
            report, healthy = self.check_results.get_nowait()
        except queue.Empty:
            self.root.after(150, self._poll_check_result)
            return

        self.check_button.configure(state="normal", text="Sprawdz polaczenie")
        self._set_message(
            "Polaczenie z serwerem dziala." if healthy else "Polaczenie nie dziala - szczegoly ponizej.",
            "#1c6b34" if healthy else "#a52222",
        )
        self._show_report(report)

    def _show_report(self, report: str) -> None:
        window = tk.Toplevel(self.root)
        window.title("Diagnostyka polaczenia")
        text = tk.Text(window, width=94, height=20, wrap="word", font=("Consolas", 9))
        text.insert("1.0", report)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True, padx=10, pady=10)
        scroll = ttk.Scrollbar(window, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        ttk.Button(window, text="Zamknij", command=window.destroy).pack(pady=(0, 10))

    def _on_save(self) -> None:
        candidate = self._collect()
        if candidate is None:
            return
        if not self._write_config(candidate):
            return

        if self._already_enrolled(candidate.server_url) and not candidate.enrollment_token:
            if any(getattr(candidate, key) != getattr(self.config, key) for key in
                   ("discovery_enabled", "discovery_auto_subnets", "discovery_cidrs")):
                state = load_state(self.config.state_path)
                state.last_discovery_at = ""
                try:
                    save_state(self.config.state_path, state)
                except RuntimeError as exc:
                    self._set_message(f"Zapisano konfiguracje, ale nie odswiezono terminu skanu: {exc}", "#a52222")
                    return
            self.saved = True
            messagebox.showinfo("Gotowe", "Zapisano ustawienia. Zostana uzyte przy nastepnej synchronizacji. "
                                "Mozesz wybrac Synchronizuj teraz z menu ikony.")
            self.root.destroy()
            return

        self.save_button.configure(state="disabled")
        self.progress.grid(row=12, column=0, columnspan=3, sticky="we")
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
            result = run_agent(
                ["enroll"],
                self.config_path,
                timeout=ENROLL_PROCESS_TIMEOUT,
                extra_env=INTERACTIVE_ENV,
            )
            output = (result.stderr or b"").decode("utf-8", errors="replace")
            self.results.put((result.returncode, output))
        except subprocess.TimeoutExpired:
            self.results.put((TIMEOUT_RETURNCODE, ""))
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
    if returncode == TIMEOUT_RETURNCODE:
        return (
            "Rejestracja nie odpowiedziala w wyznaczonym czasie.\n\n"
            "Najczestsze przyczyny:\n"
            "- zapora blokuje polaczenie (pakiety sa odrzucane po cichu, "
            "wiec agent czeka do konca limitu),\n"
            "- adres 'localhost' rozwiazuje sie na IPv6, a serwer nasluchuje "
            "tylko na IPv4 - sprobuj wpisac https://127.0.0.1:8443,\n"
            "- serwer nie zdazyl wystartowac.\n\n"
            "Szczegoly znajdziesz w dzienniku agenta (menu ikony: Pokaz dziennik)."
        )
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
