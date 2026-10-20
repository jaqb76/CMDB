# Proxmox VE

CMDB odczytuje z Proxmox VE klaster, węzły, maszyny wirtualne QEMU i kontenery
LXC. Wpisuje je do ewidencji razem z zależnościami, tak samo jak z Nutanix
Prism Central i VMware vCenter (patrz [`nutanix.md`](nutanix.md)):

```
klaster  ← host_cluster ─  węzeł (host)  ← vm_host ─  VM QEMU / kontener LXC
```

Konfiguracja, łączenie VM z agentami po UUID, znikanie obiektów, mapa relacji
i strona **Wirtualizacja** działają tak samo jak przy Nutanix i vCenter.
Poniżej opisane są tylko różnice.

## Kto się z kim łączy

Z Proxmoxem łączy się **agent** na wybranej maszynie (usługa
`cmdb-agent-monitor`), a nie serwer CMDB. Agent co 5 minut pobiera konfigurację,
co ustawiony odstęp czyta API Proxmoxa (`/api2/json`) i odsyła spłaszczony wynik.

Wymagania:

- Proxmox VE **7 lub nowszy**,
- konto (najlepiej z **tokenem API**) z rolą **PVEAuditor** na ścieżce `/`
  z propagacją. Rola tylko czyta, więc odczyt niczego w klastrze nie zmieni,
- maszyna z agentem, która widzi `https://<węzeł>:8006`. Wystarczy adres
  jednego węzła: każdy węzeł podaje dane całego klastra,
- certyfikat węzła zaufany na tej maszynie. Domyślnie wystawia go CA klastra:
  plik `/etc/pve/pve-root-ca.pem` z dowolnego węzła wklej w konfiguracji.
  Wklejony sam certyfikat węzła (self-signed) zostanie przypięty.
  **Weryfikacji certyfikatu nie da się wyłączyć**, a `http://` nie jest dopuszczane.

## Logowanie

| Sposób | Użytkownik | Hasło | Co agent robi |
|---|---|---|---|
| **token API** (zalecany) | `cmdb@pve!cmdb` (użytkownik, `!`, nazwa tokenu) | sekret tokenu | same `GET` z nagłówkiem `PVEAPIToken`, bez żadnego zapisu |
| hasło | `cmdb@pve` (z realmem: `@pve`, `@pam`, `@ad`…) | hasło | `POST /access/ticket` po bilet, potem `GET` z ciasteczkiem |

Agent rozpoznaje sposób po znaku `!` w polu użytkownika. Bilet z hasła wygasa
sam po dwóch godzinach (API Proxmoxa nie ma wylogowania). Konto z włączonym
2FA nie zaloguje się hasłem — użyj tokenu.

Konto i token na dowolnym węźle:

```bash
pveum user add cmdb@pve --comment "CMDB - tylko odczyt"
pveum acl modify / --users cmdb@pve --roles PVEAuditor
pveum user token add cmdb@pve cmdb --privsep 0      # sekret wyświetla się raz
```

`--privsep 0` — token ma uprawnienia użytkownika. Z `--privsep 1` token
potrzebuje własnego wpisu: `pveum acl modify / --tokens 'cmdb@pve!cmdb' --roles PVEAuditor`.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **Proxmox VE**:

1. adres węzła (`pve01.firma.pl` wystarczy, port 8006 dopisze się sam),
2. użytkownik albo ID tokenu i hasło albo sekret tokenu,
3. certyfikat CA klastra (`/etc/pve/pve-root-ca.pem`), jeśli nie jest
   zaufany w systemie tej maszyny,
4. **Zapisz**, potem **Testuj połączenie**. Test loguje się, czyta wersję,
   klaster i listę węzłów (pojedynczy serwer nie ma klastra — test pokaże
   „klastry: 0 · hosty: 1”),
5. zaznacz **Odczytuj Proxmox VE z tej maszyny** i zapisz.

Jeden agent może czytać dowolnie wiele klastrów Proxmoxa — każdy jako osobne
połączenie.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `POST /access/ticket` | bilet — tylko przy logowaniu hasłem |
| `GET /version` | wersja Proxmox VE do opisu testu i klastra |
| `GET /cluster/status` | nazwa klastra, węzły, ich adresy i stan |
| `GET /nodes/{węzeł}/status` | procesor (gniazda, rdzenie, wątki), RAM, wersja PVE i jądra, czas uruchomienia — tylko węzły online |
| `GET /cluster/resources?type=vm` | wszystkie VM i kontenery jednym zapytaniem (szablony są pomijane) |
| `GET /nodes/{węzeł}/qemu/{vmid}/config`, `…/lxc/{vmid}/config` | vCPU, RAM, dyski z magazynem, karty z mostem i VLAN, **UUID z SMBIOS**, opis |
| `GET …/qemu/{vmid}/agent/network-get-interfaces`, `…/agent/get-osinfo` | adresy IP i system wg QEMU Guest Agent — tylko włączone maszyny z włączonym agentem gościa |
| `GET …/lxc/{vmid}/interfaces` | adresy IP działającego kontenera |

Zapytania do QEMU Guest Agent wymagają uprawnienia `VM.GuestAgent.Audit`
(Proxmox VE 9) albo `VM.Monitor` (Proxmox VE 8), którego **PVEAuditor nie ma**.
Bez niego maszyny są odczytywane normalnie, tylko bez adresów IP i systemu
z wnętrza gościa (zostaje typ systemu z konfiguracji, np. „Linux”). `VM.Monitor`
daje dostęp do monitora QEMU, więc nie nadawaj go tylko dla CMDB; w PVE 9
wystarczy rola z samym `VM.GuestAgent.Audit`. Adresy statyczne kontenerów LXC
są w konfiguracji, więc widać je zawsze.

## Co trafia do ewidencji

| Z Proxmoxa | W CMDB |
|---|---|
| klaster | zasób „Klaster”: wersja Proxmox VE, liczba węzłów |
| węzeł | zasób „Host wirtualizacji”: procesor, RAM, wersja PVE i jądra, adres z klastra, czas uruchomienia + relacja do klastra |
| VM QEMU | zasób „Maszyna wirtualna”, model **QEMU/KVM**: stan, vCPU, RAM, dyski, karty z mostem/VLAN i adresami, system, pula + relacja do węzła |
| kontener LXC | zasób „Maszyna wirtualna”, model **LXC**: to samo, system z `ostype` (np. Debian) |

Wpisy mają źródło **„proxmox”** (na liście sprzętu: „z Proxmox”). VMID (`100`),
nazwy węzłów i klastrów powtarzają się w każdej instalacji, więc CMDB zapisuje je
z przedrostkiem wyliczonym z adresu połączenia — dwa klastry nie nadpisują sobie
obiektów.

Łączenie z kartami maszyn z agentem:

- **VM QEMU** — po UUID z `smbios1` (= `hardware.system.uuid` z raportu agenta
  w gościu), w obu kolejnościach bajtów,
- **kontener LXC** — nie ma własnego UUID (agent w kontenerze widzi UUID węzła),
  więc łączy się po **nazwie hosta**, tylko gdy jest jednoznaczna,
- **węzeł** to Debian, często z agentem CMDB — dopisuje się do jego karty po
  nazwie hosta, jak hypervisor OpenStacka.

Pojedynczy serwer (bez klastra) to węzeł bez klastra: na stronie Wirtualizacja
trafia do grupy „Bez przypisanego klastra”. Znikanie działa jak przy innych
dostawcach; powód wycofania to „zniknęła z Proxmox VE”.

## Diagnostyka na maszynie

```bash
cmdb-agent status          # wiersze "odczyt Proxmox" i "blad odczytu Proxmox"
journalctl -u cmdb-agent-monitor -n 50 | grep -i proxmox
```

| Komunikat | Znaczenie |
|---|---|
| `HTTP 401` | zły użytkownik, hasło, ID tokenu albo sekret |
| `HTTP 403` | konto bez uprawnień: rola PVEAuditor na `/` z propagacją |
| `konto nie widzi zadnego wezla` | brak uprawnień na `/`; token z `--privsep 1` potrzebuje własnego wpisu ACL |
| `uzytkownika z realmem` | w polu użytkownika brakuje `@pve` / `@pam` |
| `konto ma wlaczone 2FA` | logowanie hasłem z 2FA — użyj tokenu API |
| `certyfikat Proxmoxa nie jest zaufany` | wklej `/etc/pve/pve-root-ca.pem` w konfiguracji |
| `brak połączenia` | firewall, DNS albo zły port (8006) |

Zmiany konfiguracji i zlecenia są audytowane jako `proxmox.config_changed`,
`proxmox.test_requested`, `proxmox.read_requested`.
