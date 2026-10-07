# NetApp ONTAP

CMDB odczytuje klaster NetApp ONTAP przez **REST API** (ONTAP 9.6+): węzły,
agregaty, SVM, wolumeny, LUN-y, dyski, półki, interfejsy (LIF) i relacje
SnapMirror. Klaster trafia do ewidencji jako jeden zasób rodzaju „Pamięć masowa”,
a reszta to jego szczegóły na zakładce **NetApp**, jak przy Cephie
(patrz [`ceph.md`](ceph.md)).

## Kto się z kim łączy

Z ONTAP łączy się **agent** na wybranej maszynie (usługa `cmdb-agent-monitor`),
a nie serwer CMDB. Każde zapytanie niesie nagłówek HTTP Basic. ONTAP nie
otwiera sesji, więc po stronie macierzy nie powstaje żaden zapis, a wszystkie
zapytania to GET.

Wymagania:

- ONTAP **9.6 lub nowszy**,
- konto z wbudowaną rolą **readonly** i dostępem do aplikacji `http`:

  ```
  security login create -user-or-group-name cmdb-ro -application http -authentication-method password -role readonly
  ```

- maszyna z agentem, która widzi adres zarządzania klastrem (cluster management LIF) na porcie 443.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **NetApp ONTAP**:

1. adres klastra, np. `https://172.16.2.137`. Adres z System Managera
   (`https://172.16.2.137/sysmgr/v4/`) też zadziała, bo agent bierze z niego
   sam host i pyta `/api`,
2. konto `cmdb-ro` i hasło,
3. certyfikat: ONTAP ma domyślnie certyfikat self-signed z nazwą klastra, który
   nie pasuje do adresu IP. Wklej go w polu „Certyfikat CA”, a zostanie przypięty
   (jak przy Ceph i OME):

   ```bash
   echo | openssl s_client -connect 172.16.2.137:443 2>/dev/null | openssl x509
   ```

4. **Zapisz**, **Testuj połączenie** (czyta klaster i węzły), potem
   **Odczytuj ONTAP z tej maszyny**.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `GET /api/cluster` | nazwa, UUID, wersja ONTAP, numer seryjny, adres zarządzania |
| `GET /api/cluster/nodes` | węzły: model, numer seryjny, system ID, HA, czas pracy, stan |
| `GET /api/storage/aggregates` | agregaty: pojemność, RAID, liczba dysków |
| `GET /api/svm/svms` | SVM: protokoły, adresy IP |
| `GET /api/storage/volumes` | wolumeny: SVM, agregat, ścieżka, rozmiar, zajętość, snapshoty |
| `GET /api/storage/luns` | LUN-y: rozmiar, system, numer seryjny, mapowanie |
| `GET /api/storage/disks` | dyski: numer seryjny, model, półka i zatoka, rola, agregat, stan |
| `GET /api/storage/shelves` | półki dyskowe |
| `GET /api/network/ip/interfaces` | interfejsy (LIF): IP, SVM, węzeł, port, usługi |
| `GET /api/snapmirror/relationships` | SnapMirror: źródło, cel, stan, zdrowie, opóźnienie |

Każda lista prosi o konkretne pola, a listy są pobierane stronami po 1000.
Gdy starsza wersja ONTAP nie zna któregoś pola (HTTP 400), agent ponawia
zapytanie z `fields=*`. Listy poza klastrem i węzłami są opcjonalne: brak
uprawnień albo funkcji (np. SnapMirror) daje pustą listę, a nie błąd.

## Co widać

- **Przegląd:** wersja ONTAP, numer seryjny klastra, pojemność agregatów z paskiem
  zajętości, węzły, liczby zasobów i lista problemów: węzeł albo agregat w złym
  stanie, agregat zajęty w ≥85%, dyski w złym stanie, niezdrowe relacje SnapMirror.
- **Zakładka NetApp:** zwinięte sekcje z sortowalnymi tabelami: węzły, agregaty,
  SVM, wolumeny (z filtrem, oznaczeniem zajętości ≥90%), LUN-y, dyski (zestawienie
  model × rozmiar, dyski w złym stanie z półką i zatoką, filtr), półki,
  interfejsy, SnapMirror.
- **Wirtualizacja → Pamięć masowa:** klaster z zajętością i agregatami.
- **Globalna wyszukiwarka:** po nazwie klastra, węzła, SVM, wolumenu, adresie IP
  i numerze seryjnym dysku.

## Diagnostyka

```bash
cmdb-agent status          # wiersze "odczyt NetApp" i "blad odczytu NetApp"
journalctl -u cmdb-agent-monitor -n 50 | grep -i netapp
```

| Komunikat | Znaczenie |
|---|---|
| `odrzucił dane logowania (HTTP 401)` | złe konto/hasło albo konto bez aplikacji `http` |
| `HTTP 403 … rolę readonly` | konto bez uprawnień |
| `certyfikat ONTAP nie jest zaufany` | wklej certyfikat klastra (przypięty) albo CA |
| `nie zna /api/… (HTTP 404)` | ONTAP starszy niż 9.6 albo adres nie wskazuje klastra |
