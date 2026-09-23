# VMware vCenter

CMDB odczytuje z VMware vCenter klastry, hosty ESXi i maszyny wirtualne
i wpisuje je do ewidencji razem z zależnościami, tak samo jak z Nutanix
Prism Central (patrz [`nutanix.md`](nutanix.md)):

```
klaster  ← host_cluster ─  host ESXi  ← vm_host ─  maszyna wirtualna
```

Obie integracje korzystają z tej samej ścieżki: tak samo wygląda
konfiguracja, łączenie VM z agentami po UUID, znikanie obiektów, mapa relacji
i strona **Wirtualizacja**. Poniżej opisane są tylko różnice.

## Kto się z kim łączy

Z vCenter łączy się **agent** na wybranej maszynie (usługa
`cmdb-agent-monitor`), a nie serwer CMDB. Agent co 5 minut pobiera konfigurację,
co ustawiony odstęp czyta vCenter przez **REST API** (`/api/...`) i odsyła
spłaszczony wynik.

Wymagania:

- vCenter **7.0 U2 lub nowszy** (REST API `/api/vcenter/...`, `/api/session`),
- konto z rolą **Read-only** nadaną na poziomie obiektu vCenter, z zaznaczonym
  „Propagate to children”, np. `cmdb-ro@vsphere.local` albo konto domenowe,
- maszyna z agentem, która widzi `https://<vcenter>:443`,
- certyfikat vCenter zaufany na tej maszynie. Zwykle wystawia go VMCA
  (wewnętrzne CA vCentra): jego certyfikat można pobrać z
  `https://<vcenter>/certs/download.zip` i dodać do systemu albo wkleić
  w konfiguracji. **Weryfikacji certyfikatu nie da się wyłączyć.**

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **VMware vCenter**:

1. adres (`vcenter.firma.pl` wystarczy, port 443 dopisze się sam),
2. użytkownik (z domeną, np. `cmdb-ro@vsphere.local`) i hasło,
3. **Zapisz**, potem **Testuj połączenie**. Test loguje się, pobiera listę
   klastrów i wersję vCenter,
4. zaznacz **Odczytuj vCenter z tej maszyny** i zapisz. **Odczytaj teraz**
   zleca pełny odczyt bez czekania na odstęp.

Na jednej maszynie mogą działać jednocześnie odczyt Nutanix i odczyt vCenter.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `POST /api/session`, na końcu `DELETE /api/session` | sesja. To jedyny zapis po stronie vCenter i API go wymaga |
| `GET /api/appliance/system/version` | wersja vCenter do opisu testu (opcjonalne) |
| `GET /api/vcenter/cluster` | klastry |
| `GET /api/vcenter/host?clusters=…`, `GET /api/vcenter/host` | hosty, ich przynależność do klastrów, hosty samodzielne |
| `GET /api/vcenter/vm?hosts=…` | VM dla każdego hosta osobno, co omija limit długości listy vCenter |
| `GET /api/vcenter/vm/{vm}` | vCPU, RAM, dyski, karty, **UUID z BIOS-u** |
| `GET /api/vcenter/vm/{vm}/guest/identity`, `…/guest/networking/interfaces` | system i adresy IP według VMware Tools, tylko dla włączonych maszyn |

## Co trafia do ewidencji

| Z vCenter | W CMDB |
|---|---|
| klaster | zasób „Klaster”: liczba hostów, producent VMware |
| host ESXi | zasób „Host wirtualizacji”: nazwa, adres (gdy host dodano po IP) + relacja do klastra |
| maszyna wirtualna | zasób „Maszyna wirtualna”: stan, vCPU, RAM, dyski (z nazwą datastore), karty z siecią i adresami, VMware Tools, system + relacja do hosta |

Wpisy mają źródło **„vmware”** (na liście sprzętu: „z VMware”). Identyfikatory
vCenter (`vm-101`, `host-10`, `domain-c8`) powtarzają się w każdej instalacji,
więc CMDB zapisuje je z przedrostkiem wyliczonym z adresu vCenter. Dzięki temu
dwa vCentry nie nadpisują sobie obiektów.

VM łączy się z kartą maszyny z agentem po **UUID z BIOS-u** (`identity.bios_uuid`
z vCenter = `hardware.system.uuid` z raportu agenta), w obu kolejnościach bajtów.

Znikanie działa jak przy Nutanixie: tylko po udanym, pełnym odczycie i tylko
w obrębie tego samego dostawcy. Odczyt Nutanix nie wycofa maszyn z vCenter
i odwrotnie. Powód wycofania to „zniknęła z vCenter”.

## Diagnostyka na maszynie

```bash
cmdb-agent status          # wiersze "odczyt VMware" i "blad odczytu VMware"
journalctl -u cmdb-agent-monitor -n 50 | grep -i vmware
```

| Komunikat | Znaczenie |
|---|---|
| `HTTP 401` | zły użytkownik lub hasło (użytkownik z domeną, np. `@vsphere.local`) |
| `HTTP 403` | konto bez uprawnień: nadaj rolę Read-only na poziomie vCenter z propagacją |
| `/api/session (HTTP 404)` | vCenter starszy niż 7.0 U2 |
| `certyfikat vCenter nie jest zaufany` | dodaj CA vCentra (VMCA) do systemu albo wklej je w konfiguracji |
| `brak połączenia` | firewall, DNS albo zły port |

Zmiany konfiguracji i zlecenia są audytowane jako `vmware.config_changed`,
`vmware.test_requested`, `vmware.read_requested`.
