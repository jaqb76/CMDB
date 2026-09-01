// Wizualny edytor schematu slownika.
//
// Zrodlem prawdy pozostaje POLE TEKSTOWE z definicja JSON - to ono jest
// wysylane przy zapisie. Edytor jest soczewka nad nim: czyta je przy starcie
// i zapisuje po kazdej zmianie. Dzieki temu obie drogi (przyciski i recznie
// wpisany JSON) prowadza do tego samego wyniku i nie moga sie rozjechac,
// a serwer nie musi wiedziec, ktorej uzyto.
//
// Bez procedur obslugi wpisanych w znaczniki (onclick): polityka
// bezpieczenstwa dopuszcza wylacznie skrypty z tego serwera.
(function () {
  "use strict";

  var korzen = document.querySelector("[data-edytor-schematu]");
  if (!korzen) { return; }

  var pole = document.getElementById("definicja");
  var lista = korzen.querySelector("[data-pola]");
  var komunikat = korzen.querySelector("[data-komunikat]");
  var slownik = JSON.parse(korzen.dataset.slownik || "{}");
  var uzycia = JSON.parse(korzen.dataset.uzycia || "{}");
  var limity = JSON.parse(korzen.dataset.limity || "{}");

  var schemat;
  try {
    schemat = JSON.parse(pole.value);
  } catch (e) {
    // Recznie wpisany JSON jest niepoprawny - nie nadpisujemy go widokiem,
    // bo to skasowaloby prace, ktora ktos wlasnie wykonuje.
    korzen.hidden = true;
    return;
  }
  if (!Array.isArray(schemat.pola)) { schemat.pola = []; }

  // --- pomocnicze ---------------------------------------------------------

  function el(nazwa, klasa, tresc) {
    var wezel = document.createElement(nazwa);
    if (klasa) { wezel.className = klasa; }
    if (tresc !== undefined) { wezel.textContent = tresc; }
    return wezel;
  }

  function wybor(wartosci, biezaca, pusteJako) {
    var select = el("select");
    wartosci.forEach(function (wartosc) {
      var opcja = el("option", null, wartosc === "" ? (pusteJako || "— brak —") : wartosc);
      opcja.value = wartosc;
      if (wartosc === (biezaca || "")) { opcja.selected = true; }
      select.appendChild(opcja);
    });
    return select;
  }

  function klucz_z(tekst) {
    var mapa = {"ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
                "ó": "o", "ś": "s", "ź": "z", "ż": "z"};
    return String(tekst || "").toLowerCase().trim()
      .replace(/[ąćęłńóśźż]/g, function (znak) { return mapa[znak]; })
      .replace(/\s+/g, "_")
      .replace(/[^a-z0-9_]/g, "")
      .slice(0, 40);
  }

  function zapisz() {
    pole.value = JSON.stringify(schemat, null, 2);
    sprawdz();
  }

  // Sprawdzamy to, co serwer i tak odrzuci - zeby czlowiek dowiedzial sie
  // teraz, a nie po przeladowaniu strony z komunikatem o bledzie.
  function sprawdz() {
    var powody = [];
    var klucze = schemat.pola.map(function (p) { return p.klucz; });
    var puste = klucze.filter(function (k) { return !k; }).length;
    if (puste) { powody.push(puste + " pól bez klucza"); }

    var powtorzone = klucze.filter(function (k, i) {
      return k && klucze.indexOf(k) !== i;
    });
    if (powtorzone.length) { powody.push("powtórzony klucz: " + powtorzone[0]); }

    var role = schemat.pola.map(function (p) { return p.rola; }).filter(Boolean);
    var rolePowtorzone = role.filter(function (r, i) { return role.indexOf(r) !== i; });
    if (rolePowtorzone.length) {
      powody.push("rola „" + rolePowtorzone[0] + "” przypisana dwa razy — jedna rola, jedno pole");
    }
    if (schemat.pola.length && !schemat.pola.some(function (p) { return !!p.w_etykiecie; })) {
      powody.push("zaznacz co najmniej jedno pole budujące etykietę wpisu");
    }

    schemat.pola.forEach(function (p) {
      if (p.typ === "wybor" && (!p.opcje || !p.opcje.length)) {
        powody.push("pole „" + (p.etykieta || p.klucz) + "” wymaga listy opcji");
      }
      if (p.typ === "odwolanie" && !p.cel) {
        powody.push("pole „" + (p.etykieta || p.klucz) + "” wymaga wskazania celu");
      }
    });

    if (limity.pola && schemat.pola.length > limity.pola) {
      powody.push("za dużo pól: " + schemat.pola.length + " z " + limity.pola);
    }

    komunikat.textContent = powody.length
      ? "Do poprawienia: " + powody.join("; ") + "."
      : "";
    komunikat.hidden = !powody.length;
  }

  // --- rysowanie ----------------------------------------------------------

  function wiersz(etykieta, kontrolka, pomoc, szerokosc) {
    var kolumna = el("div", "pole-edytora " + (szerokosc || "szer-3"));
    var opis = el("label", null, etykieta);
    if (kontrolka.id) { opis.htmlFor = kontrolka.id; }
    kolumna.appendChild(opis);
    kolumna.appendChild(kontrolka);
    if (pomoc) { kolumna.appendChild(el("div", "muted small", pomoc)); }
    return kolumna;
  }

  function tekstowe(wartosc, przy_zmianie, atrybuty) {
    var input = el("input");
    input.value = wartosc || "";
    Object.keys(atrybuty || {}).forEach(function (k) { input.setAttribute(k, atrybuty[k]); });
    input.addEventListener("input", function () { przy_zmianie(input.value); });
    return input;
  }

  function warunkowe(p, numer) {
    var blok = el("div", "warunkowe");

    if (p.typ === "tekst") {
      blok.appendChild(el("div", "warunkowe-tytul", "Format tekstu"));
      var lista_formatow = [""].concat(Object.keys(slownik.formaty || {}));
      var select = wybor(lista_formatow, p.format, "— bez sprawdzania —");
      select.addEventListener("change", function () {
        if (select.value) { p.format = select.value; } else { delete p.format; }
        rysuj();
      });
      blok.appendChild(wiersz("Format", select,
        "Decyduje o sprawdzaniu wpisu i podpowiedzi w formularzu.", "szer-6"));
      if (p.format && slownik.formaty[p.format]) {
        blok.appendChild(el("div", "muted small", "Przykład: " + slownik.formaty[p.format]));
      }
      return blok;
    }

    if (p.typ === "odwolanie") {
      blok.appendChild(el("div", "warunkowe-tytul", "Na co wskazuje"));
      var cele = wybor(slownik.cele || [], p.cel);
      cele.addEventListener("change", function () { p.cel = cele.value; zapisz(); });
      blok.appendChild(wiersz("Cel", cele, "Osoba z listy firmy albo wpis innego słownika.", "szer-6"));
      return blok;
    }

    if (p.typ === "liczba") {
      blok.appendChild(el("div", "warunkowe-tytul", "Zakres"));
      var siatka = el("div", "siatka-edytora");
      siatka.appendChild(wiersz("Najmniej", tekstowe(p.min, function (w) {
        if (w === "") { delete p.min; } else { p.min = Number(w); }
        zapisz();
      }, {type: "number", step: "any"}), null, "szer-6"));
      siatka.appendChild(wiersz("Najwięcej", tekstowe(p.max, function (w) {
        if (w === "") { delete p.max; } else { p.max = Number(w); }
        zapisz();
      }, {type: "number", step: "any"}), null, "szer-6"));
      blok.appendChild(siatka);
      return blok;
    }

    if (p.typ === "wybor") {
      blok.appendChild(el("div", "warunkowe-tytul", "Opcje do wyboru"));
      (p.opcje || []).forEach(function (opcja, i) {
        var rzad = el("div", "opcja-rzad");
        var input = tekstowe(opcja, function (w) { p.opcje[i] = w; zapisz(); },
                             {placeholder: "np. portal", maxlength: "100"});
        rzad.appendChild(input);
        var usun = el("button", "btn btn-small btn-danger", "Usuń");
        usun.type = "button";
        usun.addEventListener("click", function () {
          p.opcje.splice(i, 1);
          rysuj();
        });
        rzad.appendChild(usun);
        blok.appendChild(rzad);
      });

      var dodaj = el("button", "btn btn-small", "+ Dodaj opcję");
      dodaj.type = "button";
      dodaj.disabled = (p.opcje || []).length >= (limity.opcje || 50);
      dodaj.addEventListener("click", function () {
        if (!p.opcje) { p.opcje = []; }
        p.opcje.push("");
        rysuj();
      });
      blok.appendChild(dodaj);
      blok.appendChild(el("div", "muted small",
        "Zamknięta lista kończy z literówkami — „portal” i „Portal” przestają być dwiema wartościami."));
      return blok;
    }

    return null;
  }

  function karta(p, numer) {
    var box = el("div", "panel pole-karta");

    var glowa = el("div", "pole-glowa");
    glowa.appendChild(el("span", "pole-numer", String(numer + 1)));
    var tytul = el("div", "pole-tytul");
    tytul.appendChild(el("b", null, p.etykieta || "Nowe pole"));
    tytul.appendChild(el("span", "badge", p.typ));
    if (p.rola) { tytul.appendChild(el("span", "badge badge-ok", p.rola)); }
    if (p.wymagane) { tytul.appendChild(el("span", "wymagane", "*")); }
    glowa.appendChild(tytul);
    glowa.appendChild(el("code", "pole-klucz", p.klucz || "bez_klucza"));

    var akcje = el("div", "pole-akcje");
    [["↑", -1], ["↓", 1]].forEach(function (para) {
      var przycisk = el("button", "btn btn-small", para[0]);
      przycisk.type = "button";
      przycisk.disabled = (para[1] < 0 && numer === 0)
        || (para[1] > 0 && numer === schemat.pola.length - 1);
      przycisk.addEventListener("click", function () {
        var wyjete = schemat.pola.splice(numer, 1)[0];
        schemat.pola.splice(numer + para[1], 0, wyjete);
        rysuj();
      });
      akcje.appendChild(przycisk);
    });

    var ile = uzycia[p.klucz] || 0;
    var usun = el("button", "btn btn-small btn-danger", "Usuń");
    usun.type = "button";
    if (ile) {
      // Pole z wartosciami sie nie usuwa. Blokujemy przycisk zamiast pozwolic
      // kliknac i odbic sie od serwera - tlumaczenie ma byc przy przycisku.
      usun.disabled = true;
      usun.title = "Ma wartości w " + ile + " wpisach — najpierw je wyczyść";
    } else {
      usun.addEventListener("click", function () {
        if (!window.confirm("Usunąć pole „" + (p.etykieta || p.klucz) + "”?")) { return; }
        schemat.pola.splice(numer, 1);
        rysuj();
      });
    }
    akcje.appendChild(usun);
    glowa.appendChild(akcje);
    box.appendChild(glowa);

    var cialo = el("div", "pole-cialo");
    var siatka = el("div", "siatka-edytora");

    siatka.appendChild(wiersz("Etykieta", tekstowe(p.etykieta, function (w) {
      p.etykieta = w;
      if (!p.klucz) { p.klucz = klucz_z(w); }
      zapisz();
      tytul.firstChild.textContent = w || "Nowe pole";
    }, {maxlength: "60"}), "To widzi człowiek w formularzu.", "szer-4"));

    var poleKlucza = tekstowe(p.klucz, function (w) { p.klucz = w; zapisz(); },
                              {maxlength: "40", pattern: "[a-z][a-z0-9_]*"});
    poleKlucza.addEventListener("input", function () {
      poleKlucza.value = klucz_z(poleKlucza.value);
      p.klucz = poleKlucza.value;
      zapisz();
    });
    if (ile) {
      poleKlucza.readOnly = true;
      poleKlucza.title = "Po kluczu leżą wartości w " + ile + " wpisach — zmiana go osierociłaby";
    }
    siatka.appendChild(wiersz("Klucz", poleKlucza,
      ile ? "Zablokowany: pole ma już wartości." : "Tożsamość pola. Nie zmienia się.", "szer-4"));

    siatka.appendChild(wiersz("Grupa", tekstowe(p.grupa, function (w) {
      p.grupa = w; zapisz();
    }, {maxlength: "60", placeholder: "np. Zgłoszenia"}),
      "Nagłówek sekcji w formularzu.", "szer-4"));

    var typy = wybor(slownik.typy || [], p.typ);
    typy.addEventListener("change", function () {
      p.typ = typy.value;
      if (p.typ !== "tekst") { delete p.format; }
      if (p.typ !== "odwolanie") { delete p.cel; }
      if (p.typ !== "wybor") { p.opcje = []; }
      if (p.typ !== "liczba") { delete p.min; delete p.max; }
      rysuj();
    });
    siatka.appendChild(wiersz("Typ", typy, "Decyduje o sortowaniu i porównywaniu.", "szer-4"));

    var role = wybor([""].concat(Object.keys(slownik.role || {})), p.rola, "— żadna —");
    role.addEventListener("change", function () {
      if (role.value) { p.rola = role.value; } else { delete p.rola; }
      rysuj();
    });
    siatka.appendChild(wiersz("Rola systemowa", role,
      p.rola && slownik.role[p.rola] ? slownik.role[p.rola] : "Po roli sięgają funkcje systemu.",
      "szer-4"));

    var wymagane = el("label", "kolumna-wybor");
    var znacznik = el("input");
    znacznik.type = "checkbox";
    znacznik.checked = !!p.wymagane;
    znacznik.addEventListener("change", function () {
      p.wymagane = znacznik.checked;
      rysuj();
    });
    wymagane.appendChild(znacznik);
    wymagane.appendChild(el("span", null, "wymagane w karcie słownika"));
    siatka.appendChild(wiersz("Obowiązkowe", wymagane,
      "Wymagane przy zapisie karty wpisu.", "szer-4"));

    var etykieta = el("label", "kolumna-wybor");
    var znacznikEtykiety = el("input");
    znacznikEtykiety.type = "checkbox";
    znacznikEtykiety.checked = !!p.w_etykiecie;
    znacznikEtykiety.addEventListener("change", function () {
      p.w_etykiecie = znacznikEtykiety.checked;
      rysuj();
    });
    etykieta.appendChild(znacznikEtykiety);
    etykieta.appendChild(el("span", null, "pokazuj w etykiecie wpisu"));
    siatka.appendChild(wiersz("Etykieta wpisu", etykieta,
      "Łączy wybrane pola, np. Miasto · Ulica · Dział.", "szer-4"));

    siatka.appendChild(wiersz("Podpowiedź w polu", tekstowe(p.podpowiedz, function (w) {
      if (w) { p.podpowiedz = w; } else { delete p.podpowiedz; }
      zapisz();
    }, {maxlength: "120"}), null, "szer-12"));

    cialo.appendChild(siatka);
    var extra = warunkowe(p, numer);
    if (extra) { cialo.appendChild(extra); }
    box.appendChild(cialo);
    return box;
  }

  function rysuj() {
    lista.textContent = "";
    if (!schemat.pola.length) {
      lista.appendChild(el("p", "muted", "Słownik nie ma jeszcze żadnych pól."));
    }
    schemat.pola.forEach(function (p, numer) { lista.appendChild(karta(p, numer)); });
    zapisz();
  }

  var dodajPole = korzen.querySelector("[data-dodaj-pole]");
  dodajPole.addEventListener("click", function () {
    if (schemat.pola.length >= (limity.pola || 40)) { return; }
    schemat.pola.push({klucz: "", etykieta: "Nowe pole", typ: "tekst",
                       opcje: [], wymagane: false, w_etykiecie: false, grupa: "Pozostale"});
    rysuj();
    lista.lastChild.querySelector("input").focus();
  });

  // Przelaczanie widoku. JSON zostaje dostepny jako edycja zaawansowana -
  // pole jest wysylane niezaleznie od tego, ktora zakladka jest widoczna.
  korzen.querySelectorAll("[data-widok]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var wybrany = przycisk.dataset.widok;
      korzen.querySelectorAll("[data-widok]").forEach(function (b) {
        b.classList.toggle("is-active", b === przycisk);
      });
      document.querySelectorAll("[data-panel-widok]").forEach(function (panel) {
        panel.hidden = panel.dataset.panelWidok !== wybrany;
      });
      if (wybrany === "formularz") {
        // Ktos mogl poprawiac JSON recznie - wracamy z jego wersja, nie swoja.
        try {
          var wczytany = JSON.parse(pole.value);
          if (Array.isArray(wczytany.pola)) { schemat = wczytany; rysuj(); }
        } catch (e) { /* niepoprawny JSON zostawiamy uzytkownikowi */ }
      }
    });
  });

  rysuj();
})();
