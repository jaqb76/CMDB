# Dell OpenManage Enterprise (OME)

CMDB odczytuje z OME serwery Dell: Service Tag, model, stan zdrowia i zasilania,
adres i firmware iDRAC, wersję BIOS, procesory, pamięć, dyski, adresy MAC
i system operacyjny według OME.

OME opisuje fizyczny sprzęt, który zwykle **już jest** w CMDB: jako maszyna
z agentem, host ESXi z vCenter albo wpis ręczny. Kluczem jest **Service Tag**,
czyli numer seryjny:

- serwer, którego numer seryjny jest już w ewidencji, dostaje dane OME na swojej
  karcie w sekcji **„Sprzęt (Dell OME)”**. Nie powstaje drugi wpis.
  Przy kilku kartach z tym numerem wygrywa maszyna z agentem, potem host
  wirtualizacji, potem reszta,
- serwer, którego w ewidencji nie ma, dostaje własną kartę (źródło **„ome”**,
  rodzaj „Komputer / serwer”),
- gdy taki serwer pojawi się później inną drogą (np. po instalacji agenta),
  kolejny odczyt przepina dane OME na tamtą kartę, a wpis z OME zostaje
  **wycofany** z adnotacją „ten sam Service Tag co karta: …”.

Konfiguracja, test połączenia, „Odczytaj teraz” i bezpieczeństwo hasła działają
tak samo jak przy wirtualizacji (patrz [`nutanix.md`](nutanix.md)).

## Wymagania

- OpenManage Enterprise 3.x lub 4.x,
- konto OME (lokalne albo z katalogu) z rolą **VIEWER**,
- maszyna z agentem, która widzi `https://<ome>`.

Appliance OME ma zwykle certyfikat self-signed. Wklej go w polu „Certyfikat CA”:

```bash
echo | openssl s_client -connect <ome>:443 2>/dev/null | openssl x509
```

Gdy serwer pokazuje dokładnie wklejony certyfikat, agent nie sprawdza zgodności
nazwy, ale łańcuch dalej weryfikuje (certyfikat przypięty, jak przy Ceph, patrz
[`ceph.md`](ceph.md)). OME łączy się wyłącznie po `https`.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **Dell OpenManage Enterprise**:
adres (`ome.firma.pl`), konto VIEWER i hasło, certyfikat. Potem **Zapisz**,
**Testuj połączenie** i **Odczytuj OME z tej maszyny**.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `POST /api/SessionService/Sessions`, na końcu `DELETE …/Sessions('<id>')` | sesja API. To jedyny zapis po stronie OME |
| `GET /api/ApplicationService/Info` | wersja OME (opcjonalne) |
| `GET /api/DeviceService/Devices?$filter=Type eq 1000` | serwery, stronami po 100 |
| `GET /api/DeviceService/Devices(<id>)/InventoryDetails` | CPU, pamięć, dyski, karty sieciowe, firmware, system (jedno zapytanie na serwer) |

Test połączenia czyta tylko pierwszy serwer.

## Co widać

- **Karta serwera**, sekcja „Sprzęt (Dell OME)”: zdrowie (OK / ostrzeżenie /
  krytyczny), zasilanie, utrata połączenia OME z iDRAC, Service Tag, model,
  iDRAC z odnośnikiem i wersją firmware, BIOS, CPU, RAM, dyski, MAC, system
  według OME, obudowa, czas ostatniej inwentaryzacji w OME. Przy hoście ESXi
  sekcja stoi obok sekcji Wirtualizacja.
- **Globalna wyszukiwarka** znajduje serwer po Service Tagu, adresie iDRAC,
  adresie MAC i nazwie hosta według OME.

Strona Wirtualizacja nie pokazuje OME, bo to sprzęt, a nie platforma wirtualizacji.

## Znikanie

Serwer, który zniknie z OME: wpis z samego OME zostaje wycofany („zniknął z OME”),
a karta z agentem albo z vCenter zostaje aktywna i traci tylko sekcję OME.
Nieudany odczyt niczego nie wycofuje.

## Diagnostyka

```bash
cmdb-agent status          # wiersze "odczyt OME" i "blad odczytu OME"
journalctl -u cmdb-agent-monitor -n 50 | grep -i ome
```

| Komunikat | Znaczenie |
|---|---|
| `odrzucił dane logowania` | zły użytkownik lub hasło (komunikat OME w nawiasie) |
| `HTTP 403 … rolę VIEWER` | konto bez uprawnień |
| `certyfikat OME nie jest zaufany` | wklej certyfikat appliance (przypięty) albo CA |
| `nie zna … (HTTP 404)` | adres nie wskazuje OpenManage Enterprise |
