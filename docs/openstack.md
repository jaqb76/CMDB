# OpenStack

CMDB odczytuje z OpenStacka regiony, hypervisory Novy i maszyny wirtualne
wszystkich projektów. Wpisuje je do ewidencji razem z zależnościami, tak samo
jak z Nutanix Prism Central i VMware vCenter (patrz [`nutanix.md`](nutanix.md)):

```
region (klaster)  ← host_cluster ─  hypervisor (host)  ← vm_host ─  maszyna wirtualna
```

Konfiguracja, łączenie VM z agentami po UUID, znikanie obiektów, mapa relacji
i strona **Wirtualizacja** działają tak samo jak przy Nutanix i vCenter.
Poniżej opisane są tylko różnice.

## Kto się z kim łączy

Z OpenStackiem łączy się **agent** na wybranej maszynie (usługa
`cmdb-agent-monitor`), a nie serwer CMDB. Agent co 5 minut pobiera konfigurację.
Co ustawiony odstęp loguje się do **Keystone v3**, czyta Novę i Cindera,
a spłaszczony wynik odsyła do CMDB.

Wymagania:

- OpenStack **Pike lub nowszy** (Nova API w mikrowersji 2.53),
- konto z rolą **admin** w projekcie. Lista hypervisorów i serwery wszystkich
  projektów (`all_tenants`) są w domyślnej polityce Novy dostępne tylko dla
  admina. Zamiast admina można nadać rolę `reader` i dopisać w polityce Novy
  reguły `os_compute_api:os-hypervisors:list-detail` oraz
  `os_compute_api:servers:detail:get_all_tenants`,
- maszyna z agentem, która widzi Keystone (zwykle port 5000), Novę (8774)
  i, opcjonalnie, Cindera (8776).

## Logowanie

Agent obsługuje dwa sposoby. Oba wybiera się w tym samym formularzu:

| Sposób | Użytkownik | Hasło | Domena | Projekt |
|---|---|---|---|---|
| **application credential** (zalecany) | ID poświadczenia | sekret | puste | **puste** |
| użytkownik i hasło | nazwa konta | hasło | domena konta (puste = `Default`) | projekt, w którym konto ma rolę |

O sposobie decyduje pole **Projekt**: puste oznacza application credential.

Application credential zakłada się raz, na koncie z rolą admin:

```bash
openstack application credential create cmdb-odczyt --role admin
```

Polecenie zwraca `id` i `secret`. Poświadczenie można w każdej chwili
unieważnić bez zmiany hasła konta.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **OpenStack**:

1. adres Keystone, np. `keystone.firma.pl` (port 5000 dopisze się sam) albo
   `https://chmura.firma.pl/identity`. Końcówka `/v3` jest opcjonalna,
2. dane logowania według tabeli powyżej,
3. **Zapisz**, potem **Testuj połączenie**. Test loguje się, czyta katalog
   usług i wersję Novy,
4. zaznacz **Odczytuj OpenStack z tej maszyny** i zapisz.

### Adresy usług

Adresy Novy i Cindera agent bierze z katalogu usług Keystone: najpierw punkt
`public`, a gdy go nie ma, `internal`. Bywa, że katalog podaje nazwy, których
maszyna z agentem nie rozwiązuje (np. `http://controller:8774/v2.1`). Wtedy
trzeba wpisać adresy w polach **Adres Compute** i **Adres Volumes**, np.:

| Pole | Przykład |
|---|---|
| Adres | `http://172.17.30.199:5000/v3` |
| Adres Compute | `http://172.17.30.199:8774/v2.1` |
| Adres Volumes | `http://172.17.30.199:8776/v3` |

Adres Volumes może być bez ID projektu: agent dopisze ID projektu z tokenu.
Ręczny adres dotyczy jednego regionu, czyli pierwszego z katalogu. Chmurę
z kilkoma regionami trzeba czytać z katalogu albo dodać osobne połączenie na
każdy region. Adresy usług sieciowych (Neutron) i obrazów (Glance) nie są
potrzebne, bo adresy IP i MAC maszyn podaje Nova.

### OpenStack bez TLS

Domyślnie każde połączenie idzie po `https` z weryfikacją certyfikatu.
Gdy Keystone i Nova słuchają tylko po `http`, trzeba zaznaczyć **Zezwól na
połączenie bez TLS**. Wtedy:

- hasło (albo sekret) i token idą przez sieć **otwartym tekstem**,
- połączenie ma w panelu znacznik „bez TLS”, a zgoda trafia do audytu
  (`openstack.config_changed`),
- bez zaznaczenia adres `http://` jest odrzucany przy zapisie, a agent
  nie wyśle hasła na adres `http` z katalogu usług.

Przy połączeniu bez TLS używaj application credential, a nie hasła admina.
Przechwycone poświadczenie można wtedy unieważnić bez zmiany hasła konta.
Zgoda dotyczy tylko OpenStacka: Prism Central i vCenter nadal wymagają `https`.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `POST /v3/auth/tokens`, na końcu `DELETE /v3/auth/tokens` | token. To jedyny zapis po stronie OpenStacka |
| `GET <nova>` | wersja Novy (wymagana 2.53+) |
| `GET <nova>/os-hypervisors/detail` | hypervisory: CPU, RAM, typ i wersja, adres, stan |
| `GET <nova>/os-availability-zone/detail` | strefy dostępności hostów (opcjonalne) |
| `GET <nova>/servers/detail?all_tenants=1` | maszyny wszystkich projektów, stronami po 1000 |
| `GET <cinder>/volumes/detail?all_tenants=1` | rozmiary i typy dołączonych wolumenów (opcjonalne) |
| `GET /v3/projects` | nazwy projektów zamiast ID (opcjonalne) |

Brak uprawnień do zapytań opcjonalnych nie przerywa odczytu. Brak uprawnień
do hypervisorów albo do serwerów wszystkich projektów **przerywa** odczyt.
Niepełna lista wycofałaby z ewidencji maszyny innych projektów.

## Co trafia do ewidencji

| Z OpenStacka | W CMDB |
|---|---|
| region | zasób rodzaju „Klaster”: wersja Novy, typy hypervisorów, liczba hostów |
| hypervisor | zasób rodzaju „Host wirtualizacji”: CPU, gniazda, rdzenie, wątki, RAM, QEMU/KVM, adres, strefa, „tryb serwisowy” = usługa compute wyłączona + relacja do regionu |
| serwer | zasób rodzaju „Maszyna wirtualna”: stan, vCPU i RAM z flavora, dysk lokalny i wolumeny, karty z IP (także floating), projekt, strefa, flavor + relacja do hypervisora |

Wpisy z odczytu mają źródło **„openstack”**. Identyfikatory mają przedrostek
adresu Keystone, więc `RegionOne` z dwóch chmur to dwa różne klastry.

## Maszyny i hosty z agentem

- **VM z agentem**: na KVM/libvirt UUID instancji Novy jest też UUID-em
  SMBIOS maszyny. Odczyt dopisuje się do karty agenta tak jak przy AHV.
- **Hypervisor z agentem**: węzły compute to zwykły Linux, często z agentem
  CMDB. Hypervisor łączy się z kartą agenta po nazwie hosta: pełnej
  (`cmp01.cloud.firma.pl`, porównywanej z FQDN z raportu) albo krótkiej
  (`cmp01`). Gdy tę samą nazwę ma kilka maszyn z agentem, odczyt zakłada
  osobny wpis zamiast zgadywać. Gdy hypervisor zniknie z Novy, karta agenta
  zostaje aktywna. Znika tylko relacja do regionu.

## Diagnostyka na maszynie

```bash
cmdb-agent status          # wiersze "odczyt OpenStack" i "blad odczytu OpenStack"
journalctl -u cmdb-agent-monitor -n 50 | grep -i openstack
```

| Komunikat | Znaczenie |
|---|---|
| `HTTP 401` | zły użytkownik, ID poświadczenia, hasło, domena albo projekt |
| `HTTP 403 … roli admin` | konto nie widzi hypervisorów albo maszyn wszystkich projektów |
| `token bez katalogu usług` | konto nie ma roli w projekcie |
| `katalog usług nie ma Novy` | w projekcie nie ma usługi compute; sprawdź projekt albo wpisz adres Compute |
| `adres bez https` | adres `http` bez zgody na połączenie bez TLS |
| `wymagany OpenStack Pike lub nowszy` | Nova nie obsługuje mikrowersji 2.53 |
| `brak połączenia z http://controller…` | katalog podaje nazwę niewidoczną z agenta; wpisz adres Compute/Volumes |
