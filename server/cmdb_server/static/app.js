// Panel CMDB - minimum JS, bez zewnetrznych bibliotek (CSP: script-src 'self').
(function () {
  "use strict";

  // Zakladki na karcie maszyny.
  document.querySelectorAll("[data-tabs]").forEach(function (root) {
    var buttons = root.querySelectorAll(".tab-button");
    var panels = root.querySelectorAll(".tab-panel");

    function activate(name) {
      buttons.forEach(function (b) { b.classList.toggle("is-active", b.dataset.tab === name); });
      panels.forEach(function (p) { p.classList.toggle("is-active", p.dataset.panel === name); });
      if (history.replaceState) {
        history.replaceState(null, "", "#" + name);
      }
    }

    buttons.forEach(function (b) {
      b.addEventListener("click", function () { activate(b.dataset.tab); });
    });

    var initial = window.location.hash.replace("#", "");
    if (initial && root.querySelector('[data-panel="' + CSS.escape(initial) + '"]')) {
      activate(initial);
    }
  });

  // Paski zajetosci - szerokosc ustawiana z JS, bo CSP blokuje style inline.
  document.querySelectorAll(".meter-fill[data-percent]").forEach(function (el) {
    var pct = parseFloat(el.dataset.percent);
    el.style.width = Math.max(0, Math.min(100, isNaN(pct) ? 0 : pct)) + "%";
  });

  // Filtrowanie dlugich tabel (oprogramowanie, uslugi) po stronie przegladarki.
  document.querySelectorAll("[data-filter-for]").forEach(function (input) {
    var table = document.getElementById(input.dataset.filterFor);
    if (!table) { return; }
    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach(function (row) {
        row.hidden = needle !== "" && row.textContent.toLowerCase().indexOf(needle) === -1;
      });
    });
  });

  // Potwierdzenie operacji nieodwracalnych.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.dataset.confirm)) { event.preventDefault(); }
    });
  });

  // Formularze administracyjne kierowane do wybranej firmy: adres akcji
  // zawiera znacznik PODMIEN, ktory zastepujemy identyfikatorem z listy.
  document.querySelectorAll("form[data-tenant-form]").forEach(function (form) {
    var select = form.querySelector("[data-tenant-select]");
    if (!select) { return; }
    form.addEventListener("submit", function (event) {
      if (!select.value) {
        event.preventDefault();
        window.alert("Wybierz firme.");
        return;
      }
      form.action = form.action.replace("PODMIEN", encodeURIComponent(select.value));
    });
  });

  // Zaznaczanie wszystkich maszyn na liscie aktualizacji.
  document.querySelectorAll("[data-zaznacz-wszystkie]").forEach(function (box) {
    box.addEventListener("change", function () {
      var tabela = box.closest("table");
      if (!tabela) { return; }
      tabela.querySelectorAll('tbody input[type="checkbox"]').forEach(function (pole) {
        pole.checked = box.checked;
      });
    });
  });

  // Przelacznik firmy dla superadmina.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });

  // Przelacznik motywu: system -> jasny -> ciemny -> system.
  //
  // Wybor trzymamy w ciasteczku, a nie w localStorage, bo atrybut data-theme
  // wstawia serwer przy renderowaniu strony - dzieki temu przy wejsciu nie
  // miga wersja jasna, zanim JavaScript zdazy cokolwiek zrobic.
  var KOLEJNOSC = ["", "jasny", "ciemny"];

  document.querySelectorAll("[data-motyw]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var obecny = document.documentElement.getAttribute("data-theme") || "";
      var nastepny = KOLEJNOSC[(KOLEJNOSC.indexOf(obecny) + 1) % KOLEJNOSC.length];

      if (nastepny) {
        document.documentElement.setAttribute("data-theme", nastepny);
      } else {
        document.documentElement.removeAttribute("data-theme");
      }

      // Rok waznosci; SameSite=Lax wystarcza, bo to zwykla preferencja widoku.
      var atrybuty = "; path=/; max-age=31536000; SameSite=Lax";
      if (location.protocol === "https:") {
        atrybuty += "; Secure";
      }
      document.cookie = "cmdb_motyw=" + nastepny + atrybuty;
    });
  });


  // --- hasla ---------------------------------------------------------------

  function poleObok(przycisk) {
    var grupa = przycisk.closest(".pole-hasla");
    return grupa ? grupa.querySelector("input") : null;
  }

  // Podglad hasla na zadanie. Domyslnie zakryte: haslo wpisywane w panelu
  // widzi kazdy, kto akurat patrzy w ekran, a odslania sie je tylko na czas
  // przepisania.
  document.querySelectorAll("[data-pokaz-haslo]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var pole = poleObok(przycisk);
      if (!pole) { return; }
      var zakryte = pole.type === "password";
      pole.type = zakryte ? "text" : "password";
      przycisk.textContent = zakryte ? "Ukryj" : "Pokaz";
      przycisk.setAttribute("aria-pressed", zakryte ? "true" : "false");
      pole.focus();
    });
  });

  // Generator hasla. Losowosc bierzemy z crypto, nie z Math.random - to
  // drugie jest przewidywalne i nie nadaje sie do niczego, co ma chronic
  // konto. Alfabet bez znakow mylacych sie przy przepisywaniu (0/O, 1/l/I).
  var ZNAKI = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  var DLUGOSC_LOSOWANEGO = 18;

  function losoweHaslo() {
    var bajty = new Uint32Array(DLUGOSC_LOSOWANEGO);
    window.crypto.getRandomValues(bajty);
    var wynik = "";
    for (var i = 0; i < bajty.length; i += 1) {
      wynik += ZNAKI[bajty[i] % ZNAKI.length];
    }
    return wynik;
  }

  document.querySelectorAll("[data-generuj-haslo]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var pole = poleObok(przycisk);
      if (!pole) { return; }
      pole.value = losoweHaslo();
      // Odslaniamy od razu: wygenerowanego hasla nie da sie odczytac pozniej,
      // wiec zakryte byloby haslem, ktorego nikt nie zna.
      pole.type = "text";
      var podglad = przycisk.closest(".pole-hasla").querySelector("[data-pokaz-haslo]");
      if (podglad) {
        podglad.textContent = "Ukryj";
        podglad.setAttribute("aria-pressed", "true");
      }
      pole.focus();
      pole.select();
    });
  });

  // Okno ustawiania hasla wybranemu koncie. Jedno na strone - adres konta
  // wstawiamy przy otwarciu, zamiast powielac formularz przy kazdym wierszu.
  document.querySelectorAll("[data-okno-hasla]").forEach(function (okno) {
    var formularz = okno.querySelector("[data-okno-formularz]");
    var pole = okno.querySelector("[data-pole-hasla]");
    var etykieta = okno.querySelector("[data-okno-konto]");
    if (!formularz || !pole) { return; }

    okno.querySelectorAll("[data-okno-zamknij]").forEach(function (przycisk) {
      przycisk.addEventListener("click", function () { okno.close(); });
    });

    // Sprzatanie po zamknieciu - takze klawiszem Escape, ktory omija przyciski.
    // Bez tego poprzednie haslo zostawaloby w polu, odsloniete, do nastepnego
    // otwarcia okna.
    okno.addEventListener("close", function () {
      pole.value = "";
      pole.type = "password";
      var podglad = okno.querySelector("[data-pokaz-haslo]");
      if (podglad) {
        podglad.textContent = "Pokaz";
        podglad.setAttribute("aria-pressed", "false");
      }
    });

    document.querySelectorAll("[data-haslo-akcja]").forEach(function (przycisk) {
      przycisk.addEventListener("click", function () {
        formularz.action = przycisk.dataset.hasloAkcja;
        if (etykieta) { etykieta.textContent = przycisk.dataset.hasloKonto || ""; }
        okno.showModal();
        pole.focus();
      });
    });
  });

  // Listy, ktore po wyborze przenosza na wskazany adres.
  document.querySelectorAll("[data-autonawigacja]").forEach(function (lista) {
    lista.addEventListener("change", function () {
      if (lista.value) { window.location.href = lista.value; }
    });
  });

  // Wspolne natywne okna: changelog wydania i podglad relacji zasobu.
  document.querySelectorAll("[data-dialog-open]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var okno = document.getElementById(przycisk.dataset.dialogOpen);
      if (okno && typeof okno.showModal === "function") { okno.showModal(); }
    });
  });
  document.querySelectorAll("dialog [data-dialog-close]").forEach(function (przycisk) {
    przycisk.addEventListener("click", function () {
      var okno = przycisk.closest("dialog");
      if (okno) { okno.close(); }
    });
  });
  document.querySelectorAll("dialog.modal-card").forEach(function (okno) {
    okno.addEventListener("click", function (event) {
      if (event.target === okno) { okno.close(); }
    });
  });


  // Czas w strefie czytajacego.
  //
  // Serwer liczy i zapisuje wszystko w UTC, bo strefa serwera nie moze wplywac
  // na dane. Ale czlowiek oglada raport u siebie i "16:25 UTC" zmusza go do
  // przeliczania w pamieci. Atrybut datetime pozostaje jednoznaczny (UTC),
  // podmieniamy tylko to, co widac.
  //
  // Bez tego skryptu strona nadal pokazuje poprawna godzine UTC - dlatego
  // przeliczanie jest tutaj, a nie w szablonie.
  (function () {
    var elementy = document.querySelectorAll("time[data-czas]");
    if (!elementy.length) { return; }

    var strefa = "";
    try {
      strefa = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (e) {
      strefa = "";
    }

    var format;
    try {
      format = new Intl.DateTimeFormat(undefined, {
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      });
    } catch (e) {
      return;                       // bez Intl zostawiamy zapis UTC
    }

    elementy.forEach(function (el) {
      var chwila = new Date(el.getAttribute("datetime"));
      if (isNaN(chwila.getTime())) { return; }   // nie psujemy tego, co widac
      // Podpowiedz zachowuje zapis UTC - przy zglaszaniu bledu i porownywaniu
      // z logami serwera to ta wartosc jest wspolnym punktem odniesienia.
      el.title = el.textContent.trim() + (strefa ? "  (pokazano w strefie " + strefa + ")" : "");
      el.textContent = format.format(chwila).replace(",", "");
    });
  })();


  // Rozpoznawanie formatu przy wpisywaniu.
  //
  // To jest WYGODA, nie zabezpieczenie: serwer sprawdza dokladnie te same
  // wzorce jeszcze raz przy zapisie. Tutaj chodzi tylko o to, zeby czlowiek
  // zobaczyl blad od razu, a nie po przeladowaniu strony.
  (function () {
    var wzorce = {
      kod_pocztowy: /^\d{2}-\d{3}$/,
      email: /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/,
      telefon: /^\+?[\d ]{6,20}$/,
      url: /^https?:\/\/[^\s/]+\.[^\s]*$/,
      nip: /^\d{10}$/
    };

    function nipPoprawny(cyfry) {
      var wagi = [6, 5, 7, 2, 3, 4, 5, 6, 7], suma = 0;
      for (var i = 0; i < 9; i++) { suma += wagi[i] * parseInt(cyfry[i], 10); }
      suma = suma % 11;
      return suma !== 10 && suma === parseInt(cyfry[9], 10);
    }

    function porzadkuj(format, wartosc) {
      var czysty = wartosc.trim();
      if (format === "kod_pocztowy") {
        var cyfry = czysty.replace(/\D/g, "");
        return cyfry.length === 5 ? cyfry.slice(0, 2) + "-" + cyfry.slice(2) : czysty;
      }
      if (format === "email") { return czysty.toLowerCase(); }
      if (format === "nip") { return czysty.replace(/\D/g, ""); }
      if (format === "url" && czysty && !/^https?:\/\//i.test(czysty)) {
        return "https://" + czysty;
      }
      return czysty;
    }

    document.querySelectorAll("[data-format]").forEach(function (pole) {
      var format = pole.dataset.format;
      pole.addEventListener("blur", function () {
        if (!pole.value.trim()) { pole.setCustomValidity(""); return; }
        // Porzadkujemy dopiero po wyjsciu z pola - poprawianie w trakcie
        // pisania przestawia kursor i walczy z czlowiekiem.
        pole.value = porzadkuj(format, pole.value);
        var wzorzec = wzorce[format];
        var dobrze = !wzorzec || wzorzec.test(pole.value);
        if (dobrze && format === "nip") { dobrze = nipPoprawny(pole.value); }
        pole.setCustomValidity(dobrze ? "" : "Sprawdz format tego pola.");
        pole.setAttribute("aria-invalid", dobrze ? "false" : "true");
      });
      pole.addEventListener("input", function () { pole.setCustomValidity(""); });
    });
  })();

})();
