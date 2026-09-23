/* Edytor artykulu bazy wiedzy.
 *
 * Formularz dziala bez skryptu (pole Markdown, dwa puste wiersze warunkow).
 * Skrypt dokłada wygode: przyciski formatowania, podglad renderowany przez
 * serwer, dopisywanie warunkow i licznik pasujacych maszyn na zywo.
 * Wynik z serwera (liczba, nazwy maszyn) wstawiamy przez textContent - nigdy
 * jako HTML. Wyjatkiem jest podglad tresci: to HTML wyrenderowany przez
 * serwer z wylaczonym surowym HTML, ten sam, ktory pokazuje strona artykulu.
 */
(function () {
  "use strict";

  var formularz = document.querySelector("[data-kb-edytor]");
  if (!formularz) { return; }

  var tresc = formularz.querySelector("#kb-tresc");
  var narzedzia = formularz.querySelector("[data-kb-narzedzia]");
  var podglad = formularz.querySelector("[data-kb-podglad]");
  var przyciskPodgladu = formularz.querySelector("[data-kb-podglad-przycisk]");
  var csrf = formularz.querySelector("input[name=csrf_token]").value;

  // --- formatowanie -----------------------------------------------------------

  function wstaw(przed, po) {
    var start = tresc.selectionStart, koniec = tresc.selectionEnd;
    var zaznaczone = tresc.value.slice(start, koniec);
    tresc.setRangeText(przed + zaznaczone + po, start, koniec, "end");
    var kursor = start + przed.length + zaznaczone.length;
    tresc.setSelectionRange(kursor, kursor);
    tresc.focus();
  }

  function naPoczatkuLinii(prefiks) {
    var start = tresc.selectionStart;
    var poczatek = tresc.value.lastIndexOf("\n", start - 1) + 1;
    tresc.setRangeText(prefiks, poczatek, poczatek, "end");
    tresc.focus();
  }

  if (narzedzia) {
    narzedzia.hidden = false;
    narzedzia.querySelectorAll("[data-md]").forEach(function (b) {
      b.addEventListener("click", function () {
        var czesci = b.dataset.md.split("|");
        wstaw(czesci[0], czesci[1] || "");
      });
    });
    narzedzia.querySelectorAll("[data-md-linia]").forEach(function (b) {
      b.addEventListener("click", function () { naPoczatkuLinii(b.dataset.mdLinia); });
    });
    var pole = narzedzia.querySelector("[data-md-pole]");
    if (pole) {
      pole.addEventListener("change", function () {
        if (pole.value) { wstaw(pole.value, ""); }
        pole.value = "";
      });
    }
  }

  // --- podglad ------------------------------------------------------------------

  if (przyciskPodgladu && podglad) {
    przyciskPodgladu.addEventListener("click", function () {
      if (!podglad.hidden) {
        podglad.hidden = true;
        tresc.hidden = false;
        przyciskPodgladu.textContent = "Podgląd";
        tresc.focus();
        return;
      }
      var dane = new FormData();
      dane.append("csrf_token", csrf);
      dane.append("tresc", tresc.value);
      przyciskPodgladu.disabled = true;
      fetch("/wiedza/podglad", { method: "POST", body: dane, credentials: "same-origin" })
        .then(function (odp) {
          if (!odp.ok) { throw new Error("HTTP " + odp.status); }
          return odp.text();
        })
        .then(function (html) {
          podglad.innerHTML = html;
          podglad.hidden = false;
          tresc.hidden = true;
          przyciskPodgladu.textContent = "Wróć do edycji";
        })
        .catch(function () {
          podglad.textContent = "Nie udało się pobrać podglądu. Treść jest bezpieczna w polu edycji.";
          podglad.hidden = false;
        })
        .finally(function () { przyciskPodgladu.disabled = false; });
    });
  }

  // --- warunki "Dotyczy" ----------------------------------------------------------

  var kontener = formularz.querySelector("[data-kb-warunki]");
  var dodaj = formularz.querySelector("[data-kb-dodaj-warunek]");
  var systemy = formularz.querySelector("[data-kb-systemy]");
  var panelTrafien = formularz.querySelector("[data-kb-trafienia]");
  var LISTY = { rodzaj: "kb-lista-rodzaj", lokalizacja: "kb-lista-lokalizacja" };

  function podepnij(wiersz) {
    var wybor = wiersz.querySelector("[data-kb-pole]");
    var wartosc = wiersz.querySelector("[data-kb-wartosc]");
    var usun = wiersz.querySelector("[data-kb-usun-warunek]");
    function lista() {
      if (LISTY[wybor.value]) { wartosc.setAttribute("list", LISTY[wybor.value]); }
      else { wartosc.removeAttribute("list"); }
    }
    lista();
    wybor.addEventListener("change", function () { lista(); przelicz(); });
    wartosc.addEventListener("input", przelicz);
    usun.hidden = false;
    usun.addEventListener("click", function () {
      if (kontener.querySelectorAll("[data-kb-warunek]").length > 1) { wiersz.remove(); }
      else { wartosc.value = ""; }
      przelicz();
    });
  }

  if (kontener) {
    kontener.querySelectorAll("[data-kb-warunek]").forEach(podepnij);
    if (dodaj) {
      dodaj.hidden = false;
      dodaj.addEventListener("click", function () {
        var wzor = kontener.querySelector("[data-kb-warunek]");
        var nowy = wzor.cloneNode(true);
        nowy.querySelector("[data-kb-wartosc]").value = "";
        kontener.appendChild(nowy);
        podepnij(nowy);
        nowy.querySelector("[data-kb-wartosc]").focus();
      });
    }
  }
  if (systemy) { systemy.addEventListener("input", przelicz); }

  // --- licznik pasujacych maszyn ------------------------------------------------------

  var zegar = null, numerZapytania = 0;

  function odmiana(n) {
    if (n === 1) { return "maszyna pasuje teraz"; }
    var d = n % 10, s = n % 100;
    if (d >= 2 && d <= 4 && (s < 10 || s >= 20)) { return "maszyny pasują teraz"; }
    return "maszyn pasuje teraz";
  }

  function przelicz() {
    clearTimeout(zegar);
    zegar = setTimeout(zapytaj, 400);
  }

  function zapytaj() {
    if (!panelTrafien || !kontener) { return; }
    var parametry = new URLSearchParams();
    kontener.querySelectorAll("[data-kb-warunek]").forEach(function (w) {
      var wartosc = w.querySelector("[data-kb-wartosc]").value.trim();
      if (!wartosc) { return; }
      parametry.append("pole", w.querySelector("[data-kb-pole]").value);
      parametry.append("wartosc", wartosc);
    });
    if (systemy && systemy.value.trim()) { parametry.append("systemy", systemy.value); }
    var numer = ++numerZapytania;
    fetch("/wiedza/dopasowanie?" + parametry.toString(), { credentials: "same-origin" })
      .then(function (odp) { return odp.json().then(function (j) { return { ok: odp.ok, dane: j }; }); })
      .then(function (wynik) {
        if (numer !== numerZapytania) { return; }
        var liczba = panelTrafien.querySelector("[data-kb-liczba]");
        var opis = panelTrafien.querySelector("[data-kb-opis-liczby]");
        var lista = panelTrafien.querySelector("[data-kb-lista-trafien]");
        lista.textContent = "";
        panelTrafien.hidden = false;
        if (!wynik.ok) {
          liczba.textContent = "!";
          opis.textContent = wynik.dane.blad || "nie udało się sprawdzić warunków";
          return;
        }
        liczba.textContent = String(wynik.dane.liczba);
        opis.textContent = odmiana(wynik.dane.liczba);
        wynik.dane.maszyny.forEach(function (m) {
          var li = document.createElement("li");
          var a = document.createElement("a");
          a.href = "/assets/" + encodeURIComponent(m.id);
          a.textContent = m.nazwa;
          var powod = document.createElement("span");
          powod.className = "muted small";
          powod.textContent = m.powody.join(", ");
          li.appendChild(a);
          li.appendChild(powod);
          lista.appendChild(li);
        });
        if (wynik.dane.liczba > wynik.dane.maszyny.length) {
          var reszta = document.createElement("li");
          reszta.className = "muted small";
          reszta.textContent = "…i " + (wynik.dane.liczba - wynik.dane.maszyny.length) + " kolejnych";
          lista.appendChild(reszta);
        }
      })
      .catch(function () { /* licznik jest dodatkiem - brak odpowiedzi niczego nie psuje */ });
  }

  zapytaj();
})();
