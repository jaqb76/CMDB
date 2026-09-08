import json
import os
import pathlib
import subprocess
import time
import urllib.error
import urllib.request

root = pathlib.Path(__file__).resolve().parents[2]
os.chdir(root)

context_parts = [
    "# Commit\n" + subprocess.check_output(["git", "log", "-1", "--format=%H%n%s%n%b"], text=True),
]
try:
    diff = subprocess.check_output(["git", "diff", "HEAD^", "HEAD", "--", "server", "agent", "android"], text=True)
except subprocess.CalledProcessError:
    diff = ""
context_parts.append("# Diff\n" + diff[:60000])
readme = pathlib.Path("README.md").read_text(encoding="utf-8", errors="replace")[:30000]
context_parts.append("# README\n" + readme)
context = "\n\n".join(context_parts)

prompt = """Jestes senior software architect i security reviewerem. Analizujesz rzeczywisty projekt CMDB: FastAPI + PostgreSQL/JSONB, agent Windows/Linux, aplikacja Android, multi-tenant.

Ocen aktualny commit/diff i kontekst architektury. Nie podawaj ogolnikow. Szukaj realnych regresji, race conditions, problemow z retry/idempotency, offline/restart agentow, izolacja tenantow, autoryzacja, kompatybilnoscia wsteczna, utrata danych, falszywymi alarmami oraz bledami wdrozen.

Dla kazdego znalezionego problemu uzyj formatu:
PROBLEM -> SCENARIUSZ -> SKUTEK -> TEST -> POPRAWKA

Na koncu podaj 5 najpowazniejszych ryzyk, ocene architektury 1-10, jedna zmiane o najwiekszym ROI i rzeczy, ktore wygladaja dobrze. Odpowiadaj po polsku. Jesli czegos nie da sie potwierdzic, oznacz to jako HIPOTEZA DO WERYFIKACJI.

=== KONTEKST REPOZYTORIUM ===
""" + context

payload = {
    "model": "mercury-2.5",
    "messages": [{"role": "user", "content": prompt}],
    "max_tokens": 5000,
}

req = urllib.request.Request(
    "https://api.inceptionlabs.ai/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": "Bearer " + os.environ["INCEPTION_API_KEY"],
        "Content-Type": "application/json",
    },
    method="POST",
)

started = time.perf_counter()
try:
    with urllib.request.urlopen(req, timeout=180) as response:
        raw = response.read().decode("utf-8")
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    raise SystemExit(f"Inception API HTTP {exc.code}: {body[:2000]}")

elapsed = time.perf_counter() - started
data = json.loads(raw)
text = data["choices"][0]["message"]["content"]
usage = data.get("usage", {})

out = pathlib.Path(".mercury")
out.mkdir(exist_ok=True)
(out / "review.md").write_text(text, encoding="utf-8")
(out / "metadata.json").write_text(json.dumps({"model": "mercury-2.5", "elapsed_seconds": round(elapsed, 3), "usage": usage}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Mercury completed in {elapsed:.3f}s")
print(json.dumps(usage, ensure_ascii=False))
