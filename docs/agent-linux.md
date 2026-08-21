# Agent na Linuksie (Ubuntu, Debian, Raspberry Pi)

Agent jest napisany wyłącznie na bibliotece standardowej Pythona, więc na
Linuksie **nie trzeba go budować** — instaluje się go wprost ze źródeł. Ma to
znaczenie na Raspberry Pi: PyInstaller nie potrafi budować na inną
architekturę, więc plik zbudowany na maszynie x86 i tak by się tam nie
uruchomił.

Wymagania: Python 3.9+ (Ubuntu 22.04 ma 3.10, 24.04 ma 3.12), systemd, `sudo`.

## Instalacja

```bash
git clone https://github.com/jaqb76/CMDB.git
cd CMDB/agent/packaging
sudo ./install-agent.sh --server https://cmdb.firma.pl --token cmdb_ent_...
```

Skrypt kopiuje pakiet do `/opt/cmdb-agent`, zapisuje konfigurację w
`/etc/cmdb-agent/agent.conf` z prawami `0600` (jest w niej token firmowy),
rejestruje maszynę, zakłada usługę systemd i wysyła pierwszy raport.

Przydatne przełączniki:

| Przełącznik | Znaczenie |
|---|---|
| `--interval 4` | co ile godzin raportować (domyślnie 4) |
| `--ca-bundle /ścieżka/ca.pem` | własne PKI lub certyfikat self-signed zamiast systemowego magazynu |

Certyfikat podany w `--ca-bundle` jest **kopiowany** do `/etc/cmdb-agent/ca.pem`, więc plik źródłowy (np. kopia w katalogu domowym albo w `/tmp`) można po instalacji skasować — agent go już nie potrzebuje.
| `--no-processes` | nie zbieraj listy procesów |

## Cykl pracy

Agent **nie jest rezydentny** — zbiera dane, wysyła i kończy pracę, więc między
przebiegami nie zajmuje pamięci. Cykl zapewnia timer systemd, tak samo jak na
Windows robi to Harmonogram zadań:

```bash
systemctl list-timers cmdb-agent.timer     # kiedy następny przebieg
journalctl -u cmdb-agent.service           # dziennik przebiegów
cmdb-agent status                          # ostatnia poprawna synchronizacja
cmdb-agent doctor                          # diagnostyka połączenia etap po etapie
```

Timer ma `RandomizedDelaySec=10min`, żeby przy większej flocie maszyny nie
uderzały w serwer w tej samej sekundzie, oraz `Persistent=true` — Pi wyłączone
na noc nadgoni pominięty przebieg po starcie.

Usługa działa z `ProtectSystem=strict` i `ProtectHome=true`: agent czyta dane
systemowe, ale zapisywać może wyłącznie do `/var/lib/cmdb-agent`.

## Aktualizacja

Agent uruchomiony ze źródeł **świadomie pomija samoaktualizację** — nie ma
pojedynczego pliku wykonywalnego do podmiany. Aktualizuje się go tak, jak
zainstalowano:

```bash
cd CMDB && git pull
sudo ./agent/packaging/install-agent.sh --server https://cmdb.firma.pl --token cmdb_ent_...
```

Samoaktualizacja z panelu serwera dotyczy wersji zbudowanych PyInstallerem.
Jeśli chcesz jej używać na Linuksie, zbuduj agenta **na maszynie o tej samej
architekturze** co maszyny docelowe i wgraj plik w panelu.

## Architektura procesora ma znaczenie

ELF dla x86-64 i ELF dla ARM64 to oba „linux", ale plik zbudowany dla jednej
architektury nie uruchomi się na drugiej. Dlatego wydanie opisane jest parą
**system + architektura**, a serwer proponuje maszynie wyłącznie plik zgodny z
obiema. Architekturę odczytujemy z nagłówka pliku, nie z deklaracji
wgrywającego — nagłówek jest faktem, deklaracja bywa pomyłką.

Maszyna, która nie zaraportowała swojej architektury (agent starszy niż ta
zmiana), **nie dostaje żadnej propozycji aktualizacji**. Lepiej, żeby została
na swojej wersji, niż miała pobrać plik nie do uruchomienia.

Zbudowanie agenta dla Raspberry Pi wymaga uruchomienia skryptu budującego na
samym Pi — cross-kompilacja nie wchodzi w grę.

## Co agent zbiera na ARM

Raspberry Pi nie ma DMI/SMBIOS, z którego na maszynach x86 czytamy producenta,
model i numer seryjny. Agent sięga wtedy kolejno do:

1. `/sys/class/dmi/id/*` — maszyny x86 i część serwerów ARM,
2. `/proc/device-tree/model` — np. `Raspberry Pi 5 Model B Rev 1.0`,
3. `/proc/cpuinfo` — pola `Model`, `Hardware`, `Serial`, `Revision`.

Numer seryjny Pi pochodzi z `/proc/cpuinfo`, więc maszyna ma stabilny
identyfikator również bez DMI. Pozostałe sekcje — pakiety (dpkg/rpm), usługi
(systemd), konta lokalne, interfejsy sieciowe, dyski i pamięć — działają
tak samo jak na x86.

## Bez systemd

Gdy systemd nie jest dostępny (kontener, minimalny obraz), cykl można oprzeć
o cron:

```cron
17 */4 * * * root /usr/local/bin/cmdb-agent run >/dev/null 2>&1
```

Alternatywnie `cmdb-agent loop` działa w pętli z ustawionym interwałem —
wygodne w kontenerze, gdzie proces i tak ma żyć na pierwszym planie.
