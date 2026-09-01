"""Ktore wydanie agenta powstaje z danej zmiany.

Wydanie dla systemu, w ktorym nic sie nie zmienilo, jest gorsze niz zadne:
plik jest bajt w bajt taki sam jak poprzedni, a jego opis zmian mowi o czyms,
czego w nim nie ma. Dokladnie to sie zdarzylo przy poprawce dla Linuksa.
"""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def plan():
    sciezka = Path(__file__).resolve().parents[2] / "scripts" / "release_plan.py"
    spec = importlib.util.spec_from_file_location("cmdb_test_plan", sciezka)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def test_zmiana_w_skrypcie_linuksa_nie_wydaje_windowsa(plan):
    assert plan.touched_systems(["agent/packaging/install-agent.sh"]) == {"linux"}
    assert plan.touched_systems(["agent/packaging/uninstall-agent.sh"]) == {"linux"}


def test_zmiana_w_instalatorze_windows_nie_wydaje_linuksa(plan):
    assert plan.touched_systems(["agent/packaging/install-agent.ps1"]) == {"windows"}
    assert plan.touched_systems(["agent/packaging/cmdb-agent.iss"]) == {"windows"}


def test_kod_agenta_jest_wspolny(plan):
    """Pakiet cmdb_agent dziala na obu systemach - jego zmiana dotyczy obu."""
    assert plan.touched_systems(["agent/cmdb_agent/upgrade.py"]) == {"linux", "windows"}
    assert plan.touched_systems(
        ["agent/packaging/install-agent.sh", "agent/cmdb_agent/main.py"]
    ) == {"linux", "windows"}


def test_zmiana_poza_agentem_nie_wydaje_niczego(plan):
    assert plan.touched_systems(["server/cmdb_server/api/ui.py", "README.md"]) == set()


def test_nieznany_plik_w_packaging_jest_wspolny(plan):
    """Nowy plik w katalogu pakowania nalezy uznac za wspolny, dopoki ktos nie
    przypisze go do systemu. Pominiecie wydania jest gorsze niz wydanie zbedne:
    poprawka nie dojechalaby na maszyny i nikt by tego nie zauwazyl."""
    assert plan.touched_systems(["agent/packaging/cos-nowego"]) == {"linux", "windows"}


def test_opis_zmian_dotyczy_wskazanego_systemu(plan, tmp_path):
    wpisy = [
        {"category": "fixed", "text": "Wspolna poprawka dla obu systemow agenta."},
        {"category": "fixed", "text": "Poprawka wylacznie dla Linuksa i jego uslugi.",
         "systemy": ["linux"]},
    ]
    assert [w["text"] for w in plan.notes_for(wpisy, "windows")] == [wpisy[0]["text"]]
    assert len(plan.notes_for(wpisy, "linux")) == 2
    # Pole "systemy" nie trafia do opisu pojedynczego wydania - tam jest zbedne.
    assert all("systemy" not in w for w in plan.notes_for(wpisy, "linux"))


def test_fragment_z_systemem_jest_wczytywany(plan, tmp_path):
    katalog = tmp_path / "agent" / "changelog"
    katalog.mkdir(parents=True)
    (katalog / "a.json").write_text(json.dumps(
        {"category": "fixed", "text": "Poprawka dotyczaca wylacznie Linuksa.",
         "systemy": ["linux"]}), encoding="utf-8")

    wpisy = plan.read_changelog(tmp_path, ["agent/changelog/a.json"])
    assert wpisy == [{"category": "fixed",
                      "text": "Poprawka dotyczaca wylacznie Linuksa.",
                      "systemy": ["linux"]}]


def test_nieznany_system_we_fragmencie_jest_odrzucany(plan, tmp_path):
    katalog = tmp_path / "agent" / "changelog"
    katalog.mkdir(parents=True)
    (katalog / "a.json").write_text(json.dumps(
        {"category": "fixed", "text": "Poprawka dla nieistniejacego systemu.",
         "systemy": ["bsd"]}), encoding="utf-8")

    with pytest.raises(ValueError):
        plan.read_changelog(tmp_path, ["agent/changelog/a.json"])
