# Architektura

## Przepływ danych

```
agent (maszyna klienta)
  │  1. zbiera dane niezależnymi krokami (kolektorami)
  │  2. składa raport JSON (schema_version = 1)
  │
  ├─ HTTPS POST /api/v1/agents/enroll   ← tylko przy pierwszym uruchomieniu
  │    Authorization: Bearer cmdb_ent_… (token firmy)
  │    ← odpowiedź: cmdb_agt_… (poświadczenie tej maszyny)
  │
  └─ HTTPS POST /api/v1/inventory        ← przy każdym cyklu
       Authorization: Bearer cmdb_agt_…
       Content-Encoding: gzip
                │
serwer          ▼
  1. token → firma (tenant). Firma NIGDY nie pochodzi z treści żądania.
  2. walidacja nagłówka raportu; sekcje danych przechodzą jako dowolny JSON.
  3. odświeżenie kolumn maszyny (hostname, OS, IP, s/n) — po nich filtruje panel.
  4. odcisk „stabilnej" części raportu → jeśli bez zmian, tylko last_seen.
  5. w innym razie nowy snapshot z pełnym JSON-em + retencja N ostatnich.
```

## Model danych

Zasada: **pełny raport w JSON, kolumny relacyjne tylko dla tego, po czym
filtrujemy**. Dodanie nowego pola do raportu nie wymaga migracji bazy.

| Tabela | Rola |
|---|---|
| `tenants` | firma — korzeń izolacji danych |
| `portal_users` | konta panelu (`admin` / `viewer`, opcjonalnie superadmin bez firmy) |
| `enrollment_tokens` | tokeny rejestracyjne firmy (skrót SHA-256 + jawny prefiks do wyszukania) |
| `agent_credentials` | poświadczenie pojedynczej maszyny, unieważnialne osobno |
| `assets` | maszyna: kolumny do filtrowania + `facts` (skrót do listy) |
| `owners` | opiekun/właściciel po stronie firmy |
| `inventory_snapshots` | pełny raport agenta jako JSONB — źródło prawdy |
| `audit_log` | ślad operacji wrażliwych |

Na PostgreSQL kolumny JSON to `JSONB`, a `inventory_snapshots.payload`
i `assets.facts` dostają indeksy GIN — pozwala to pytać o zawartość raportu:

```sql
-- na których maszynach jest zainstalowany dany program?
SELECT a.hostname, p->>'version'
FROM inventory_snapshots s
JOIN assets a ON a.id = s.asset_id
CROSS JOIN LATERAL jsonb_array_elements(s.payload->'software'->'packages') p
WHERE a.tenant_id = :tenant AND p->>'name' = '7-Zip';

-- wszystkie maszyny z Windows 11 (z użyciem indeksu GIN)
SELECT DISTINCT asset_id FROM inventory_snapshots
WHERE payload @> '{"os":{"name":"Microsoft Windows 11 Pro"}}';
```

PostgreSQL jest jedynym wspieranym silnikiem — również w developmencie
i testach. Przez pewien czas dev i testy chodziły na SQLite, gdzie te same
kolumny były zwykłym `JSON`. Wygoda kosztowała więcej, niż dawała: SQLite nie
ma typu `boolean` ani `JSONB`, nie zna blokad doradczych ani indeksów GIN,
więc migracje i zapytania zachowywały się tam inaczej niż na produkcji —
a różnica wychodziła dopiero przy starcie serwera klienta. Jeden silnik
wszędzie znaczy, że to, co przeszło testy, zachowa się tak samo w produkcji.

## Deduplikacja snapshotów

Agent raportuje cyklicznie, więc większość raportów jest identyczna.
Zapisywanie każdego z nich zasypałoby bazę i historię zmian.

Przed zapisem liczymy SHA-256 raportu **po odcięciu pól ulotnych**
(`inventory.VOLATILE_PATHS`): czas zebrania, uptime, wolne miejsce na dyskach,
lista procesów, sesje. Jeśli odcisk się zgadza — aktualizujemy tylko
`last_seen`. Jeśli nie — powstaje nowy snapshot i `last_change_at`.

W efekcie historia maszyny zawiera wyłącznie realne zmiany: instalację
programu, dołożenie pamięci, nowe konto administratora.

Retencja (`CMDB_SNAPSHOT_RETENTION`, domyślnie 50) zostawia N najnowszych
snapshotów na maszynę.

## Izolacja firm

Cała izolacja przechodzi przez `services/scoping.py`. Nie istnieje tam
funkcja pobierająca dane bez `TenantContext`, więc nie da się przypadkiem
napisać zapytania bez filtra `tenant_id`.

* **Agent** — `tenant_id` wynika z tokenu. Ten sam `machine_id` w dwóch
  firmach to dwie odrębne maszyny (klucz `UNIQUE (tenant_id, machine_id)`).
* **Panel** — zwykły użytkownik ma firmę przypisaną na stałe; superadmin
  przełącza się jawnie i zawsze ogląda jedną firmę naraz.
* Wejście na URL maszyny z innej firmy zwraca **404**, nie 403 — nie
  potwierdzamy istnienia cudzego zasobu.

Testy `server/tests/test_tenant_isolation.py` pokrywają wszystkie te ścieżki.

## Kontrakt raportu

```jsonc
{
  "schema_version": 1,
  "machine_id": "win-uuid-4c4c4544-…",   // stabilny identyfikator maszyny
  "agent":    { "version": "0.1.0", "collector": "windows", "collected_at": "…", "duration_ms": 812 },
  "identity": { "hostname": "…", "fqdn": "…", "domain": "…", "os_family": "windows" },
  "hardware": { "system": {…}, "cpu": {…}, "memory": {…}, "storage": {…}, "firmware": {…}, "gpus": […] },
  "os":       { "name": "…", "version": "…", "build": "…", "last_boot": "…" },
  "network":  { "interfaces": […] },
  "software": { "packages": […], "services": […], "updates": […], "processes": […] },
  "users":    { "local_accounts": […], "administrators": […], "groups": […], "sessions": […] },
  "errors":   [ { "collector": "software.updates", "message": "Access denied" } ]
}
```

Serwer waliduje ściśle nagłówek i identyfikację, a sekcje danych przyjmuje
jako dowolny JSON. Nowszy agent może dołożyć pole bez zmiany serwera,
a starszy agent nadal działa.

Pole `errors` jest istotne w praktyce: pojedyncze zapytanie WMI potrafi
zwrócić błąd uprawnień albo się zaciąć. Niekompletny raport z zapisanym
błędem jest lepszy niż brak raportu — panel pokazuje takie maszyny
ostrzeżeniem.

## Dodanie kolejnego systemu operacyjnego

Schemat raportu jest wspólny, więc **nowy system to jeden plik i jedna
linijka**, bez zmian po stronie serwera:

1. `agent/cmdb_agent/collectors/macos.py` — klasa dziedzicząca po
   `BaseCollector`, implementująca `machine_id()`, `identity()` i `steps()`.
2. Rejestracja w `collectors/__init__.py::get_collector`.

Każdy krok w `steps()` to para `("ścieżka.w.raporcie", funkcja)`. Wyjątek
w kroku trafia na listę `errors` i nie przerywa reszty zbierania —
to zachowanie jest w bazowej klasie, nie trzeba go powtarzać.

Wzorcem jest `collectors/linux.py`.
