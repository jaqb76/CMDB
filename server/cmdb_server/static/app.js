// Panel CMDB - minimum JS, bez zewnetrznych bibliotek (CSP: script-src 'self').
(function () {
  "use strict";

  // Zwijane menu aplikacji. Na duzym ekranie wybor jedzie w ciasteczku, bo
  // serwer musi znac go juz przy renderowaniu strony - inaczej kazde przejscie
  // zaczynaloby sie od menu zwinietego i widac by bylo jego rozwijanie. Na
  // telefonie panel zawsze startuje zamkniety i wysuwa sie nad trescia.
  (function () {
    var shell = document.querySelector("[data-sidebar-shell]");
    if (!shell) { return; }

    var body = document.body;
    var toggles = document.querySelectorAll("[data-sidebar-toggle]");
    var closeButtons = document.querySelectorAll("[data-sidebar-close]");
    var mobile = window.matchMedia("(max-width: 760px)");
    var cookieKey = "cmdb_sidebar";

    function zapisanyStan() {
      var wpisy = document.cookie ? document.cookie.split("; ") : [];
      for (var i = 0; i < wpisy.length; i++) {
        var para = wpisy[i].split("=");
        if (para[0] === cookieKey) { return para.slice(1).join("="); }
      }
      return null;
    }

    function savedExpanded() { return zapisanyStan() === "rozwiniete"; }

    function saveExpanded(expanded) {
      document.cookie = cookieKey + "=" + (expanded ? "rozwiniete" : "zwiniete") +
        "; Path=/; Max-Age=31536000; SameSite=Lax";
    }

    function desktopExpanded() { return !body.classList.contains("sidebar-collapsed"); }
    function mobileOpen() { return body.classList.contains("sidebar-mobile-open"); }

    function updateButtons() {
      var expanded = mobile.matches ? mobileOpen() : desktopExpanded();
      toggles.forEach(function (button) {
        button.setAttribute("aria-expanded", expanded ? "true" : "false");
        button.title = expanded ? "Zwiń menu" : "Rozwiń menu";
        button.setAttribute("aria-label", expanded ? "Zwiń menu" : "Rozwiń menu");
        var label = button.querySelector(".nav-label");
        if (label) { label.textContent = expanded ? "Zwiń menu" : "Rozwiń menu"; }
      });
    }

    function closeMobile() {
      body.classList.remove("sidebar-mobile-open");
      updateButtons();
    }

    // Przejscie ze starego zapisu w localStorage: przegladarka, ktora pamieta
    // rozwiniete menu sprzed zmiany, przepisuje je raz na ciasteczko. Bez tego
    // serwer az do pierwszego klikniecia wysylalby menu zwiniete.
    if (zapisanyStan() === null) {
      var stary = false;
      try { stary = window.localStorage.getItem("cmdb_sidebar_expanded") === "1"; }
      catch (e) { /* Tryb prywatny moze blokowac localStorage. */ }
      if (stary) {
        saveExpanded(true);
        if (!mobile.matches) { body.classList.remove("sidebar-collapsed"); }
      }
    }

    // Serwer wyslal juz strone w zapamietanym stanie, wiec tutaj wystarczy
    // dopasowac opisy przyciskow.
    updateButtons();

    toggles.forEach(function (button) {
      button.addEventListener("click", function () {
        // Plynne przejscie ma sens tylko wtedy, gdy szerokosc zmienia czlowiek.
        body.classList.add("menu-animuje");
        if (mobile.matches) {
          body.classList.toggle("sidebar-mobile-open");
        } else {
          body.classList.toggle("sidebar-collapsed");
          saveExpanded(desktopExpanded());
        }
        updateButtons();
      });
    });
    closeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        body.classList.add("menu-animuje");
        closeMobile();
      });
    });

    // Na telefonie wybor pozycji od razu chowa panel - inaczej wysunieta
    // warstwa wisi nad strona przez caly czas jej wczytywania.
    document.querySelectorAll(".app-nav-link[href]").forEach(function (link) {
      link.addEventListener("click", function () {
        if (mobile.matches) { closeMobile(); }
      });
    });

    // Po przejsciu z widoku mobilnego na desktop nie zostawiamy niewidzialnej
    // warstwy blokujacej strone.
    mobile.addEventListener("change", function () {
      closeMobile();
      if (!mobile.matches) {
        body.classList.toggle("sidebar-collapsed", !savedExpanded());
      }
      updateButtons();
    });

    // Zaznaczenie ustawia juz szablon; tutaj zostaje tylko dopisanie
    // aria-current oraz zapasowe wyliczenie dla widokow spoza listy.
    var path = window.location.pathname.replace(/\/$/, "") || "/";
    document.querySelectorAll(".app-nav-link[href]").forEach(function (link) {
      if (link.classList.contains("is-active")) {
        link.setAttribute("aria-current", "page");
        return;
      }
      // Link "#" (przelacznik ukladu) to przycisk, nie strona - jego adres
      // rozwiazuje sie do biezacej strony i swiecilby zawsze jako aktywny.
      if (link.getAttribute("href").charAt(0) === "#") { return; }
      var href = new URL(link.href, window.location.origin).pathname.replace(/\/$/, "") || "/";
      var exact = link.hasAttribute("data-nav-exact");
      if ((exact && path === href) || (!exact && href !== "/" && (path === href || path.indexOf(href + "/") === 0))) {
        link.classList.add("is-active");
        link.setAttribute("aria-current", "page");
      }
    });

    // Na niskim ekranie aktywna pozycja moze lezec ponizej widocznej czesci
    // przewijanej listy - pokazujemy ja, zeby bylo widac, gdzie jestesmy.
    // Liczymy recznie zamiast scrollIntoView: to przewijaloby takze cala
    // strone, a na telefonie menu stoi poza ekranem.
    var lista = document.querySelector(".app-nav");
    var aktywna = lista && lista.querySelector(".app-nav-link.is-active");
    if (aktywna) {
      var dol = aktywna.offsetTop - lista.offsetTop + aktywna.offsetHeight;
      if (dol > lista.clientHeight) { lista.scrollTop = dol - lista.clientHeight + 12; }
    }
  })();

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

    function zHasza() {
      var nazwa = window.location.hash.replace("#", "");
      if (nazwa && root.querySelector('[data-panel="' + CSS.escape(nazwa) + '"]')) {
        activate(nazwa);
      }
    }
    zHasza();
    // Odnosnik do zakladki z tej samej strony (np. "Wycofaj z uzytku"
    // w naglowku) nie przeladowuje jej - bez tego nasluchu klikniecie
    // przewijaloby do ukrytego panelu i wygladalo na zepsute.
    window.addEventListener("hashchange", zHasza);
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
    // Skoro lista wysyla formularz sama, przycisk obok niczego nie wnosi.
    // Chowamy go dopiero tutaj: bez skryptu jest jedynym sposobem zmiany firmy.
    var zapasowy = select.form && select.form.querySelector("[data-zapasowy]");
    if (zapasowy) { zapasowy.hidden = true; }
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


  // Formularz dodawania sprzetu dostraja sie do wybranego rodzaju.
  //
  // Zestawy pol wszystkich rodzajow przychodza ze strona, wiec zmiana rodzaju
  // przestawia formularz od razu. Przeladowanie strony byloby prostsze, ale
  // skasowaloby wszystko, co czlowiek zdazyl juz wpisac wyzej - a rodzaj
  // zmienia sie zwykle po zorientowaniu sie, ze wybralo sie zly.
  (function () {
    var wybor = document.querySelector("[data-rodzaj-sprzetu]");
    if (!wybor) { return; }
    var blok = document.querySelector("[data-pola-rodzaju]");
    var lista = document.querySelector("[data-pola-rodzaju-lista]");
    var legenda = document.querySelector("[data-legenda-rodzaju]");
    if (!blok || !lista) { return; }

    var wszystkie;
    try {
      wszystkie = JSON.parse(wybor.dataset.pola || "{}");
    } catch (e) {
      return;                       // bez danych zostawiamy formularz w spokoju
    }

    function el(nazwa, klasa, tresc) {
      var w = document.createElement(nazwa);
      if (klasa) { w.className = klasa; }
      if (tresc !== undefined) { w.textContent = tresc; }
      return w;
    }

    function kontrolka(pole) {
      var nazwa = "pole_" + pole.klucz;
      if (pole.typ === "wybor") {
        var select = el("select");
        select.id = nazwa; select.name = nazwa;
        select.appendChild(new Option("—", ""));
        (pole.opcje || []).forEach(function (o) { select.appendChild(new Option(o, o)); });
        return select;
      }
      if (pole.typ === "logiczna") {
        var etykieta = el("label", "kolumna-wybor");
        var znacznik = el("input");
        znacznik.type = "checkbox"; znacznik.id = nazwa; znacznik.name = nazwa;
        znacznik.value = "tak";
        etykieta.appendChild(znacznik);
        etykieta.appendChild(el("span", null, "tak"));
        return etykieta;
      }
      if (pole.typ === "notatka") {
        var obszar = el("textarea");
        obszar.id = nazwa; obszar.name = nazwa; obszar.rows = 2;
        obszar.maxLength = 2000;
        return obszar;
      }
      var input = el("input");
      input.id = nazwa; input.name = nazwa;
      if (pole.typ === "liczba") {
        input.type = "number"; input.step = "any";
        if (pole.min !== undefined && pole.min !== null) { input.min = pole.min; }
        if (pole.max !== undefined && pole.max !== null) { input.max = pole.max; }
      } else if (pole.typ === "data") {
        input.type = "date";
      } else {
        input.type = "text";
        input.maxLength = 500;
        if (pole.format) { input.dataset.format = pole.format; }
        if (pole.podpowiedz) { input.placeholder = pole.podpowiedz; }
      }
      return input;
    }

    function rysuj() {
      var pola = wszystkie[wybor.value] || [];
      lista.textContent = "";
      blok.hidden = !pola.length;
      if (legenda) {
        var wybrana = wybor.options[wybor.selectedIndex];
        legenda.textContent = "Właściwe dla rodzaju: " + (wybrana ? wybrana.text : "");
      }
      pola.forEach(function (pole) {
        var kolumna = el("div", "pole");
        var opis = el("label", null, pole.etykieta);
        opis.htmlFor = "pole_" + pole.klucz;
        if (pole.wymagane) {
          var gwiazdka = el("span", "wymagane", "*");
          gwiazdka.title = "pole wymagane";
          opis.appendChild(gwiazdka);
        }
        kolumna.appendChild(opis);
        kolumna.appendChild(kontrolka(pole));
        lista.appendChild(kolumna);
      });
    }

    wybor.addEventListener("change", rysuj);
    rysuj();
  })();

})();

// Przelaczanie pelnego ukladu portalu. Wybor dotyczy portalu firmowego i
// panelu administratora oraz jest zachowany pomiedzy kolejnymi stronami.
(function () {
  "use strict";
  document.querySelectorAll("[data-layout-switch]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      var layout = button.getAttribute("data-layout-switch") === "classic" ? "classic" : "modern";
      document.cookie = "cmdb_layout=" + layout + "; Path=/; Max-Age=31536000; SameSite=Lax";
      window.location.reload();
    });
  });
})();

// Zwijane grupy menu. Zwiniete grupy jada w ciasteczku, zeby serwer rysowal
// menu od razu w tym samym stanie (patrz _menu.html). Zapisujemy stan
// wszystkich grup widocznych na stronie, a klucze grup z drugiego ukladu
// (portal / panel administratora) przepisujemy bez zmian.
(function () {
  "use strict";
  var klucz = "cmdb_menu_zwiniete";
  var grupy = document.querySelectorAll("[data-nav-group]");
  if (!grupy.length) { return; }

  function zapisane() {
    var wpisy = document.cookie ? document.cookie.split("; ") : [];
    for (var i = 0; i < wpisy.length; i++) {
      var para = wpisy[i].split("=");
      if (para[0] === klucz) { return para.slice(1).join("=").split(".").filter(Boolean); }
    }
    return [];
  }

  function zapisz() {
    var tutaj = {};
    var zwiniete = [];
    grupy.forEach(function (g) {
      var k = g.getAttribute("data-nav-group");
      tutaj[k] = true;
      if (g.classList.contains("is-zwinieta")) { zwiniete.push(k); }
    });
    zapisane().forEach(function (k) { if (!tutaj[k]) { zwiniete.push(k); } });
    document.cookie = klucz + "=" + zwiniete.join(".") + "; Path=/; Max-Age=31536000; SameSite=Lax";
  }

  grupy.forEach(function (g) {
    var przycisk = g.querySelector("[data-nav-group-toggle]");
    if (!przycisk) { return; }
    przycisk.addEventListener("click", function () {
      var zwinieta = g.classList.toggle("is-zwinieta");
      przycisk.setAttribute("aria-expanded", zwinieta ? "false" : "true");
      zapisz();
    });
  });
})();

// Menu konta w gornym pasku: <details> otwiera sie sam, tutaj tylko
// zamykanie klikiem poza menu i klawiszem Esc.
(function () {
  "use strict";
  var menu = document.querySelectorAll("[data-konto-menu]");
  if (!menu.length) { return; }
  document.addEventListener("click", function (event) {
    menu.forEach(function (m) {
      if (m.open && !m.contains(event.target)) { m.open = false; }
    });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") { return; }
    menu.forEach(function (m) {
      if (m.open) { m.open = false; m.querySelector("summary").focus(); }
    });
  });
})();

// Formularz monitorowanej uslugi: pola zalezne od sposobu sprawdzenia.
// Sciezka HTTP nie ma sensu przy sprawdzeniu TCP, a nazwa w certyfikacie
// przy protokole bez TLS - pokazywanie ich zawsze kaze wypelniac pola,
// ktore i tak zostana pominiete przy zapisie. Bez skryptu widac wszystkie
// pola i formularz nadal dziala; serwer i tak sprawdza je od nowa.
(function () {
  "use strict";
  var Z_TLS = ["tls", "https"];
  var Z_HTTP = ["http", "https"];

  document.querySelectorAll("[data-monitoring-form]").forEach(function (form) {
    var wybor = form.querySelector("[data-monitoring-protokol]");
    var port = form.querySelector("[data-monitoring-port]");
    var blokHttp = form.querySelector("[data-monitoring-http]");
    var blokTls = form.querySelector("[data-monitoring-tls]");
    if (!wybor) { return; }

    // Port sprzed zmiany, zeby nie nadpisac wartosci wpisanej recznie:
    // ktos, kto wpisal 8443, nie chce dostac z powrotem 443 tylko dlatego,
    // ze przelaczyl sposob sprawdzenia.
    var poprzedniDomyslny = wybor.options[wybor.selectedIndex]
      ? wybor.options[wybor.selectedIndex].dataset.port : null;

    function rysuj() {
      var wybrany = wybor.value;
      if (blokHttp) { blokHttp.hidden = Z_HTTP.indexOf(wybrany) === -1; }
      if (blokTls) { blokTls.hidden = Z_TLS.indexOf(wybrany) === -1; }

      var domyslny = wybor.options[wybor.selectedIndex]
        ? wybor.options[wybor.selectedIndex].dataset.port : null;
      if (port && domyslny && (!port.value || port.value === poprzedniDomyslny)) {
        port.value = domyslny;
      }
      poprzedniDomyslny = domyslny;
    }

    wybor.addEventListener("change", rysuj);
    rysuj();
  });

  // --- rodzaj konta panelu -------------------------------------------------
  // Formularz konta pokazywal naraz wszystkie pola: rodzaj, firme i role -
  // takze wtedy, gdy do wybranego rodzaju nie pasowaly. Superadmin nie ma
  // firmy, audytor nie ma roli (nigdzie nie zapisuje), a technik dostaje firmy
  // w osobnej kolumnie. Pole, ktorego wartosc i tak nic nie zmienia, tylko
  // podpowiada, ze cos znaczy.
  //
  // Pola niepasujace do rodzaju sa WYLACZANE, a nie tylko chowane: wylaczone
  // pole nie idzie w formularzu, wiec serwer nie dostaje wartosci, ktorej nikt
  // nie widzial na ekranie.
  document.querySelectorAll("[data-konto-form]").forEach(function (form) {
    var wybor = form.querySelector("[data-konto-zakres]");
    if (!wybor) { return; }
    var opis = form.querySelector("[data-konto-opis]")
      || (form.parentElement && form.parentElement.querySelector("[data-konto-opis]"));

    function rysuj() {
      var opcja = wybor.options[wybor.selectedIndex];
      var pasuje = (opcja && opcja.dataset.pola ? opcja.dataset.pola : "").split(" ");

      form.querySelectorAll("[data-konto-pole]").forEach(function (blok) {
        var potrzebne = pasuje.indexOf(blok.dataset.kontoPole) !== -1;
        blok.hidden = !potrzebne;
        blok.querySelectorAll("input, select").forEach(function (pole) {
          pole.disabled = !potrzebne;
        });
      });
      if (opis && opcja) { opis.textContent = opcja.dataset.opis || ""; }
    }

    wybor.addEventListener("change", rysuj);
    rysuj();
  });

  // --- podsumowanie przy zamykaniu zgloszenia -------------------------------
  // Tresc doklejana do maila o zakonczeniu ma sens tylko wtedy, gdy zgloszenie
  // wlasnie sie zamyka. Przy pozostalych statusach nic nie wychodzi, a przy
  // zgloszeniu juz zamknietym nie ma przejscia, wiec i maila. Bez skryptu pole
  // zostaje widoczne - puste niczego nie psuje.
  document.querySelectorAll("[data-status-wybor]").forEach(function (wybor) {
    var form = wybor.closest("form");
    var blok = form && form.querySelector("[data-status-podsumowanie]");
    if (!blok) { return; }
    var juzZamkniete = wybor.dataset.statusWybor === "zamkniete";

    function rysuj() {
      blok.hidden = juzZamkniete || wybor.value !== "zamkniete";
    }

    wybor.addEventListener("change", rysuj);
    rysuj();
  });

  // --- znacznik "czeka na odpowiedz" ---------------------------------------
  // Panel jest skladany na serwerze, wiec bez tego licznik zmienialby sie
  // dopiero przy przejsciu na inna strone - a technik siedzi na jednym
  // ekranie dlugimi kwadransami i to wlasnie wtedy przychodzi poczta.
  //
  // Odpytywanie NICZEGO nie oznacza jako zalatwione. Licznik kasuje wylacznie
  // odpowiedz wyslana do klienta, wiec moze tu spokojnie chodzic w kolko.
  document.querySelectorAll("[data-czeka]").forEach(function (znacznik) {
    var liczba = znacznik.querySelector("[data-czeka-liczba]");
    var napis = znacznik.querySelector("[data-czeka-napis]");
    if (!liczba || !napis) { return; }

    var CO_ILE = 60000;
    var bledy = 0;

    function pokaz(ile) {
      znacznik.classList.toggle("czeka-sa", ile > 0);
      liczba.textContent = ile > 0 ? String(ile) : "";
      napis.textContent = ile > 0 ? "Czeka na odpowiedź" : "Wszystko odpisane";
    }

    function sprawdz() {
      fetch("/helpdesk/licznik", { headers: { "Accept": "application/json" } })
        .then(function (odp) {
          if (!odp.ok) { throw new Error(odp.status); }
          return odp.json();
        })
        .then(function (dane) {
          bledy = 0;
          pokaz(Number(dane.czeka) || 0);
        })
        .catch(function () {
          // Zerwana siec albo wygasla sesja. Zostawiamy ostatnia znana liczbe
          // - wyzerowanie jej wygladaloby jak "wszystko odpisane" i bylo by
          // po prostu nieprawda. Po kilku probach przestajemy pytac, zeby nie
          // dobijac serwera, ktory i tak nie odpowiada.
          bledy += 1;
          if (bledy >= 5) { clearInterval(zegar); }
        });
    }

    var zegar = setInterval(sprawdz, CO_ILE);

    // Powrot do karty po dluzszej przerwie: sprawdzamy od razu, zamiast czekac
    // do konca biezacego odstepu.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { sprawdz(); }
    });
  });
})();
