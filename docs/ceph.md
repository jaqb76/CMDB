# Ceph

CMDB odczytuje klaster Ceph przez **Ceph Dashboard REST API**: stan zdrowia,
pojemność, OSD, monitory, węzły i pule. Klaster trafia do ewidencji jako
jeden zasób rodzaju „Pamięć masowa”, a węzły i pule są jego szczegółami.

Gdy CMDB czyta też OpenStacka, region, którego Cinder trzyma wolumeny
w tym Cephie, łączy się z klastrem relacją **Klaster → pamięć masowa**:

```
region OpenStack (klaster)  ─ cluster_storage →  klaster Ceph (pamięć masowa)
```

Konfiguracja, test połączenia, „Odczytaj teraz” i bezpieczeństwo hasła działają
tak samo jak przy wirtualizacji (patrz [`nutanix.md`](nutanix.md),
[`openstack.md`](openstack.md)). Poniżej opisane są tylko różnice.

## Kto się z kim łączy

Z Dashboardem łączy się **agent** na wybranej maszynie (usługa
`cmdb-agent-monitor`), a nie serwer CMDB.

Wymagania:

- Ceph **Pacific lub nowszy** z włączonym modułem Dashboard
  (`ceph mgr module enable dashboard`),
- konto Dashboardu z rolą **read-only**:

  ```bash
  echo -n 'mocne-haslo' > /tmp/haslo && \
  ceph dashboard ac-user-create cmdb-ro -i /tmp/haslo read-only && rm /tmp/haslo
  ```

- maszyna z agentem, która widzi Dashboard. Domyślnie jest to `https://<mgr>:8443`;
  `ceph mgr services` pokazuje aktualny adres.

Dashboard działa na **aktywnym** mgr. Mgr w trybie standby przekierowuje na
aktywny, a agent nie podąża za przekierowaniami. Podaj więc adres aktywnego mgr
albo adres za load balancerem. Przy przekierowaniu agent zgłasza to wprost.

## Włączenie

Karta maszyny → **Agent** → **Dodatkowe funkcjonalności** → **Ceph**:

1. adres Dashboardu (`ceph-mgr.firma.pl` wystarczy, port 8443 dopisze się sam),
2. **Nazwa**, np. „Ceph DC1”. Pod tą nazwą klaster pojawi się w ewidencji
   (bez nazwy pojawi się pod adresem),
3. użytkownik `cmdb-ro` i hasło, potem **Zapisz** i **Testuj połączenie**,
4. zaznacz **Odczytuj Ceph Dashboard z tej maszyny** i zapisz.

Dashboard bez TLS (`ssl false`, zwykle port 8080) wymaga zaznaczenia
**Zezwól na połączenie bez TLS**. Hasło i token idą wtedy otwartym tekstem,
połączenie ma w panelu znacznik „bez TLS”, a zgoda trafia do audytu.
Przy `https` certyfikat jest zawsze weryfikowany. Certyfikat self-signed
Dashboardu można wkleić w polu CA.

## Co agent pyta

| Zapytanie | Po co |
|---|---|
| `POST /api/auth`, na końcu `POST /api/auth/logout` | token JWT. To jedyny zapis po stronie Cepha |
| `GET /api/summary` | wersja Cepha |
| `GET /api/health/minimal` | zdrowie i kontrole, pojemność, OSD up/in, monitory i kworum, fsid |
| `GET /api/health/get_cluster_fsid` albo `/api/monitor` | fsid, gdy `minimal` go nie podaje (zależnie od wersji) |
| `GET /api/host` | węzły z rolami (mon, mgr, osd, rgw, mds…), liczbą OSD i wersją |
| `GET /api/pool?stats=true` | pule: typ, replikacja, PG, aplikacje, zajętość |

Test połączenia czyta tylko trzy pierwsze. Wszystkie zapytania mają nagłówek
`Accept: application/vnd.ceph.api.v1.0+json`.

## Co trafia do ewidencji

| Z Dashboardu | W CMDB |
|---|---|
| klaster (fsid) | zasób „Pamięć masowa”, źródło **„ceph”**: nazwa połączenia, wersja, fsid jako numer seryjny |
| zdrowie, kontrole | znacznik HEALTH_OK / WARN / ERR i lista ostrzeżeń na karcie |
| `df` | pojemność raw z paskiem zajętości |
| OSD, monitory | liczby, w tym OSD down i monitory poza kworum |
| węzły | tabela na karcie: nazwa, adres, role, OSD, wersja |
| pule | tabela: typ, replikacja (pula bez kopii jest oznaczona), PG, aplikacje, zajęte/dostępne |

Klaster widać też w menu **Wirtualizacja**, w sekcji **Pamięć masowa** (zwinięty,
z filtrem po źródle i wyszukiwarką), oraz w globalnej wyszukiwarce: po nazwie,
fsid, nazwie lub adresie węzła i nazwie puli.

Gdy Dashboard przestanie pokazywać klaster (np. adres wskazuje teraz inny
klaster), wpis zostaje **wycofany** z powodem „zniknął z Ceph Dashboard”.
Gdy klaster wróci, wycofanie zostaje cofnięte. Nieudany odczyt niczego nie wycofuje.

## Relacja z OpenStackiem

Agent czytający OpenStacka pyta też Cindera o pule (`scheduler-stats/get_pools`,
konto admin). Sterownik RBD podaje w nich `location_info` w postaci
`ceph:<conf>:<fsid>:<użytkownik>:<pula>`. Po fsid CMDB łączy region
z klastrem Ceph o tym samym fsid:

- działa w dowolnej kolejności: relacja powstaje, gdy w ewidencji są oba końce,
- znika, gdy Cinder przestanie korzystać z tego Cepha albo gdy któryś koniec
  zniknie z odczytu,
- relacje dodane ręcznie (np. VM → Ceph, gdy coś montuje RBD bez Cindera)
  zostają nietknięte.

Na karcie klastra Ceph i w sekcji Pamięć masowa widać, które regiony z niego
korzystają.

## Diagnostyka na maszynie

```bash
cmdb-agent status          # wiersze "odczyt Ceph" i "blad odczytu Ceph"
journalctl -u cmdb-agent-monitor -n 50 | grep -i ceph
```

| Komunikat | Znaczenie |
|---|---|
| `odrzucil dane logowania (HTTP 400/401)` | zły użytkownik albo hasło |
| `HTTP 403 … rolę read-only` | konto bez roli read-only |
| `przekierowuje na …` | adres wskazuje mgr w trybie standby; podaj aktywny mgr |
| `nie zna /api/… (HTTP 404)` | adres nie wskazuje Dashboardu |
| `nie obsługuje API v1.0` | Ceph starszy niż Pacific |
| `adres bez https` | adres `http` bez zgody na połączenie bez TLS |
| `nie podał fsid klastra` | Dashboard nie zwrócił fsid; sprawdź `ceph fsid` i wersję |
