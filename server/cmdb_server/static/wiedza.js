/* Edytor artykulu bazy wiedzy.
 *
 * Formularz dziala bez skryptu (pole Markdown, dwa puste wiersze warunkow).
 * Skrypt dokłada:
 *
 * - edytor wizualny (domyslny): tresc wyglada jak na stronie artykulu, pasek
 *   jak w edytorze tekstu, wklejanie z Worda/Confluence/Excela przez
 *   KbWklej (wiedza_wklej.js). Przy zapisie tresc edytora zamienia sie na
 *   Markdown w polu "tresc" - serwer dostaje to samo co dotad,
 * - tryb Markdown dla znajacych skladnie, z podgladem renderowanym przez serwer,
 * - obrazy ze schowka i z pliku: jada z formularzem w polu "obrazy", w tresci
 *   zostaje ![opis](kb-obraz:N); serwer zapisuje je jako zalaczniki,
 * - dopisywanie warunkow i licznik pasujacych maszyn na zywo.
 *
 * Wynik z serwera (liczba, nazwy maszyn) wstawiamy przez textContent - nigdy
 * jako HTML. Wyjatkiem jest HTML tresci: wyrenderowany przez serwer
 * z wylaczonym surowym HTML, ten sam, ktory pokazuje strona artykulu.
 */
(function () {
  "use strict";

  var formularz = document.querySelector("[data-kb-edytor]");
  if (!formularz) { return; }

  var tresc = formularz.querySelector("#kb-tresc");
  var narzedzia = formularz.querySelector("[data-kb-narzedzia]");
  var podglad = formularz.querySelector("[data-kb-podglad]");
  var przyciskPodgladu = formularz.querySelector("[data-kb-podglad-przycisk]");
  var komunikat = formularz.querySelector("[data-kb-komunikat]");
  var poleObrazow = formularz.querySelector("[data-kb-obrazy]");
  var wyborObrazu = formularz.querySelector("[data-kb-wybor-obrazu]");
  var oknoLinku = formularz.querySelector("[data-kb-link]");
  var adresLinku = formularz.querySelector("[data-kb-adres-linku]");
  var csrf = formularz.querySelector("input[name=csrf_token]").value;
  var limitObrazu = (parseInt(formularz.dataset.limitMb, 10) || 10) * 1024 * 1024;
  var KLUCZ_TRYBU = "cmdb-wiedza-tryb-edytora";
  var TYPY_OBRAZOW = /^image\/(png|jpeg|gif|webp)$/;

  function pokaz(tekst, uwaga) {
    if (!komunikat) { return; }
    komunikat.textContent = tekst;
    komunikat.classList.toggle("kb-komunikat-uwaga", !!uwaga);
    komunikat.hidden = !tekst;
  }

  function esc(t) { var d = document.createElement("div"); d.textContent = t; return d.innerHTML; }

  function renderujNaSerwerze(markdown, doEdycji) {
    var dane = new FormData();
    dane.append("csrf_token", csrf);
    dane.append("tresc", markdown);
    if (doEdycji) { dane.append("edycja", "1"); }
    return fetch("/wiedza/podglad", { method: "POST", body: dane, credentials: "same-origin" })
      .then(function (odp) {
        if (!odp.ok) { throw new Error("HTTP " + odp.status); }
        return odp.text();
      });
  }

  // --- tryb Markdown: wstawianie skladni do pola -----------------------------------

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

  // --- edytor wizualny ------------------------------------------------------------------

  var edytor = null;           // div contenteditable
  var trybMd = false;
  var zmieniony = false;       // czy edytor wizualny zmienil tresc od wczytania
  var obrazy = [];             // wklejone obrazy: {plik: File, dane: "data:..."}

  if (narzedzia && window.KbWklej && "contentEditable" in document.body) {
    edytor = document.createElement("div");
    edytor.className = "kb-tresc kb-wizualny";
    edytor.contentEditable = "true";
    edytor.spellcheck = true;
    edytor.setAttribute("role", "textbox");
    edytor.setAttribute("aria-multiline", "true");
    edytor.setAttribute("aria-label", "Treść artykułu");
    tresc.parentNode.insertBefore(edytor, tresc.nextSibling);
    var szablon = formularz.querySelector("[data-kb-tresc-html]");
    ustawHtml(szablon ? szablon.innerHTML : "");
    try { document.execCommand("defaultParagraphSeparator", false, "p"); } catch (e) { /* stare przegladarki */ }
  }

  function ustawHtml(html) {
    edytor.innerHTML = html;
    // obrazy jeszcze nie zapisane (powrot z trybu Markdown): znacznik -> podglad
    edytor.querySelectorAll("img").forEach(function (img) {
      var m = /^kb-obraz:(\d+)$/.exec(img.getAttribute("src") || "");
      if (m && obrazy[+m[1]]) {
        img.setAttribute("src", obrazy[+m[1]].dane);
        img.setAttribute("data-kb-obraz", m[1]);
      } else if (m) { img.remove(); }
    });
    wypelnijPuste();
  }

  // Pusty akapit, element listy czy komorka bez <br> nie da sie kliknac.
  function wypelnijPuste() {
    edytor.querySelectorAll("p, li, td, th, h2, h3, h4").forEach(function (el) {
      if (!el.firstChild) { el.appendChild(document.createElement("br")); }
    });
    if (!edytor.firstElementChild) { edytor.innerHTML = "<p><br></p>"; }
  }

  // execCommand w Chrome potrafi dopisac style i <span>/<font> - edytor trzymamy czysty
  function sprzatnij() {
    edytor.querySelectorAll("[style]").forEach(function (el) { el.removeAttribute("style"); });
    edytor.querySelectorAll("span, font").forEach(function (s) {
      s.replaceWith.apply(s, Array.prototype.slice.call(s.childNodes));
    });
  }

  function zmiana() {
    zmieniony = true;
  }

  // Po "zaznacz wszystko + usun" Chrome zostawia pusta liste albo naglowek -
  // pisanie od nowa zaczynaloby sie w punkcie listy. Pusty edytor to akapit.
  function pustyNaAkapit() {
    if (edytor.textContent.trim() || edytor.querySelector("img, table, hr")) { return; }
    if (edytor.children.length === 1 && edytor.firstElementChild.nodeName === "P") { return; }
    edytor.innerHTML = "<p><br></p>";
    var r = document.createRange();
    r.setStart(edytor.firstChild, 0);
    r.collapse(true);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }

  function ustawTryb(naMd, zapamietaj) {
    if (!edytor || naMd === trybMd) { return Promise.resolve(); }
    if (zapamietaj) { try { localStorage.setItem(KLUCZ_TRYBU, naMd ? "md" : "wiz"); } catch (e) { /* bez pamieci */ } }
    var gotowe;
    if (naMd) {
      if (zmieniony) { tresc.value = KbWklej.doMarkdown(edytor); zmieniony = false; }
      gotowe = Promise.resolve();
    } else {
      gotowe = renderujNaSerwerze(tresc.value, true).then(ustawHtml).catch(function () {
        pokaz("Nie udało się przełączyć do edytora. Treść jest bezpieczna w polu Markdown.", true);
        throw new Error("render");
      });
    }
    return gotowe.then(function () {
      trybMd = naMd;
      zmieniony = false;
      edytor.hidden = naMd;
      tresc.hidden = !naMd;
      podglad.hidden = true;
      // Pole jest wymagane, ale w trybie wizualnym niewidoczne - przegladarka
      // nie pokazalaby komunikatu. Pusta tresc sprawdzamy sami przy zapisie.
      tresc.required = naMd;
      narzedzia.querySelectorAll("[data-kb-tryb]").forEach(function (b) {
        b.setAttribute("aria-pressed", String((b.dataset.kbTryb === "md") === naMd));
      });
      if (przyciskPodgladu) { przyciskPodgladu.hidden = !naMd; przyciskPodgladu.textContent = "Podgląd"; }
      narzedzia.querySelectorAll("[data-wiz]:not([data-md]):not([data-md-linia])").forEach(function (b) {
        if (b.dataset.wiz !== "obraz") { b.hidden = naMd; }
      });
      var pomocMd = formularz.querySelector("[data-kb-pomoc-md]");
      var pomocWiz = formularz.querySelector("[data-kb-pomoc-wiz]");
      if (pomocMd) { pomocMd.hidden = !naMd; }
      if (pomocWiz) { pomocWiz.hidden = naMd; }
      oknoLinku.hidden = true;
    });
  }

  // --- zaznaczenie w edytorze -------------------------------------------------------------

  var zapamietany = null;

  function wEdytorze() {
    var sel = window.getSelection();
    if (!sel.rangeCount || !edytor.contains(sel.anchorNode)) {
      edytor.focus();
      var r = document.createRange();
      if (zapamietany && edytor.contains(zapamietany.startContainer)) { r = zapamietany; }
      else { r.selectNodeContents(edytor); r.collapse(false); }
      sel.removeAllRanges();
      sel.addRange(r);
    }
    return sel;
  }

  function elementKursora() {
    var sel = window.getSelection();
    if (!sel.rangeCount || !edytor.contains(sel.anchorNode)) { return null; }
    var n = sel.anchorNode;
    return n.nodeType === 1 ? n : n.parentNode;
  }

  document.addEventListener("selectionchange", function () {
    if (!edytor || trybMd) { return; }
    var el = elementKursora();
    if (!el) { return; }
    zapamietany = window.getSelection().getRangeAt(0).cloneRange();
    var styl = narzedzia.querySelector("[data-kb-styl]");
    var blok = el.closest("h2, h3, h4, p, li, pre, td, th");
    if (styl) { styl.value = blok && /^H[234]$/.test(blok.nodeName) ? blok.nodeName.toLowerCase() : "p"; }
  });

  // --- obrazy -------------------------------------------------------------------------------

  function nazwaObrazu(plik) {
    var rozszerzenie = { "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp" }[plik.type];
    var nazwa = plik.name && !/^image\.\w+$/i.test(plik.name) ? plik.name : "";
    if (nazwa) { return nazwa; }
    var d = new Date();
    var dwa = function (x) { return (x < 10 ? "0" : "") + x; };
    return "obraz-" + d.getFullYear() + dwa(d.getMonth() + 1) + dwa(d.getDate()) + "-" +
      dwa(d.getHours()) + dwa(d.getMinutes()) + dwa(d.getSeconds()) + "-" + (obrazy.length + 1) + "." + rozszerzenie;
  }

  // Plik -> pozycja na liscie obrazow; wynik: numer (Promise).
  function dodajObraz(plik) {
    return new Promise(function (ok, blad) {
      if (!TYPY_OBRAZOW.test(plik.type)) { blad(new Error("Obraz musi być plikiem PNG, JPEG, GIF albo WebP.")); return; }
      if (plik.size > limitObrazu) { blad(new Error("Obraz jest większy niż " + (limitObrazu / 1048576) + " MB.")); return; }
      var czytnik = new FileReader();
      czytnik.onload = function () {
        var nazwany = new File([plik], nazwaObrazu(plik), { type: plik.type });
        obrazy.push({ plik: nazwany, dane: czytnik.result });
        ok(obrazy.length - 1);
      };
      czytnik.onerror = function () { blad(new Error("Nie udało się odczytać obrazu.")); };
      czytnik.readAsDataURL(plik);
    });
  }

  function wstawObrazy(pliki) {
    var lista = Array.prototype.slice.call(pliki).filter(function (p) { return /^image\//.test(p.type); });
    if (!lista.length) { return; }
    var sel = trybMd ? null : wEdytorze();
    var zakres = sel && sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
    Promise.all(lista.map(function (p) { return dodajObraz(p).catch(function (e) { return e; }); }))
      .then(function (wyniki) {
        var bledy = wyniki.filter(function (w) { return w instanceof Error; });
        var numery = wyniki.filter(function (w) { return !(w instanceof Error); });
        if (trybMd) {
          wstaw(numery.map(function (n) { return "![" + obrazy[n].plik.name + "](kb-obraz:" + n + ")"; }).join("\n"), "");
        } else if (numery.length) {
          if (zakres) { var s = window.getSelection(); s.removeAllRanges(); s.addRange(zakres); }
          document.execCommand("insertHTML", false, numery.map(function (n) {
            return '<img src="' + esc(obrazy[n].dane) + '" alt="" data-kb-obraz="' + n + '">';
          }).join(""));
          sprzatnij();
          zmiana();
        }
        var tekst = numery.length ? (numery.length === 1 ? "Wstawiono obraz" : "Wstawiono obrazy: " + numery.length) +
          " — zapisze się jako załącznik artykułu." : "";
        if (bledy.length) { tekst += (tekst ? " " : "") + bledy[0].message; }
        pokaz(tekst, bledy.length > 0);
      });
  }

  // Obrazy zapisane w HTML ze schowka jako data: - na pliki, jak wklejony zrzut.
  function przejmijObrazyZHtml() {
    var nowe = edytor.querySelectorAll("img[data-kb-nowy]");
    nowe.forEach(function (img) {
      img.removeAttribute("data-kb-nowy");
      // Bez fetch(): CSP panelu (connect-src 'self') nie wpuszcza adresow data:.
      Promise.resolve().then(function () {
        var m = /^data:([^;,]+);base64,(.*)$/.exec(img.getAttribute("src") || "");
        var bajty = atob(m[2]), tablica = new Uint8Array(bajty.length);
        for (var i = 0; i < bajty.length; i++) { tablica[i] = bajty.charCodeAt(i); }
        return dodajObraz(new File([tablica], "", { type: m[1] }));
      }).then(function (n) {
        img.setAttribute("data-kb-obraz", String(n));
        zmiana();
      }).catch(function () { img.remove(); });
    });
  }

  // --- wklejanie ----------------------------------------------------------------------------

  function opisRaportu(r) {
    var czesci = [];
    var liczba = function (n, jeden, kilka, wiele) {
      var d = n % 10, s = n % 100;
      return n + " " + (n === 1 ? jeden : d >= 2 && d <= 4 && (s < 10 || s >= 20) ? kilka : wiele);
    };
    if (r.listy) { czesci.push(liczba(r.listy, "lista", "listy", "list")); }
    if (r.tabele) { czesci.push(liczba(r.tabele, "tabela", "tabele", "tabel")); }
    if (r.kod) { czesci.push(liczba(r.kod, "blok kodu", "bloki kodu", "bloków kodu")); }
    if (r.panele) { czesci.push(liczba(r.panele, "panel", "panele", "paneli")); }
    if (r.wklejoneObrazy) { czesci.push(liczba(r.wklejoneObrazy, "obraz", "obrazy", "obrazów")); }
    var tekst = "Wklejono" + (r.zrodlo ? " z programu " + r.zrodlo : "") + (czesci.length ? ": " + czesci.join(", ") + "." : ".");
    if (r.obrazyOdnosniki) {
      tekst += " " + liczba(r.obrazyOdnosniki, "obraz z innego serwera wstawiono", "obrazy z innego serwera wstawiono",
        "obrazów z innego serwera wstawiono") + " jako odnośnik — zapisz go i dodaj przyciskiem Obraz, jeśli ma być widoczny.";
    }
    if (r.obrazy) {
      tekst += " Pominięto " + liczba(r.obrazy, "obraz", "obrazy", "obrazów") +
        " — obrazów z Worda nie da się przenieść razem z tekstem. Skopiuj każdy osobno (sam obraz) i wklej, albo użyj przycisku Obraz.";
    }
    return tekst;
  }

  function wklejHtml(html, tekst) {
    var el = elementKursora();
    // w bloku kodu i w komorce tabeli zawsze sam tekst
    if (el && el.closest("pre, td, th")) {
      document.execCommand("insertText", false, (tekst || "").replace(/\r\n?/g, "\n"));
      return;
    }
    var wynik = KbWklej.oczysc(html, tekst, document);
    if (!wynik.dom.firstChild) { return; }
    var bloki = wynik.dom.children;
    var li = el && el.closest("li");
    if (li && !li.textContent.trim() && !li.querySelector("img") &&
        (bloki.length > 1 || (bloki.length === 1 && bloki[0].nodeName !== "P"))) {
      wyjdzZListy(li);
    }
    document.execCommand("insertHTML", false, wynik.dom.innerHTML);
    sprzatnij();
    wypelnijPuste();
    przejmijObrazyZHtml();
    zmiana();
    if (html) { pokaz(opisRaportu(wynik.raport), wynik.raport.obrazy || wynik.raport.obrazyOdnosniki); }
  }

  // Caly dokument wklejony w pusty punkt listy (np. "1." z szablonu nowego
  // artykulu) wyladowalby wewnatrz listy. Pusty punkt znika, a tresc wchodzi
  // za liste.
  function wyjdzZListy(li) {
    var lista = li.parentNode;
    var p = document.createElement("p");
    p.appendChild(document.createElement("br"));
    lista.parentNode.insertBefore(p, lista.nextSibling);
    li.remove();
    if (!lista.querySelector("li")) { lista.remove(); }
    var r = document.createRange();
    r.setStart(p, 0);
    r.collapse(true);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }

  // Excel i przegladarka ("kopiuj obraz") daja obok HTML takze plik obrazu.
  // Obraz bierzemy tylko wtedy, gdy HTML nie niesie tekstu - inaczej
  // zaznaczenie komorek wkleiloby sie jako zrzut zamiast tabeli.
  function tylkoObraz(dt, html) {
    var pliki = Array.prototype.slice.call(dt.files || []).filter(function (p) { return /^image\//.test(p.type); });
    if (!pliki.length) { return null; }
    if (html && new DOMParser().parseFromString(html, "text/html").body.textContent.trim()) { return null; }
    if (!html && (dt.getData("text/plain") || "").trim()) { return null; }
    return pliki;
  }

  var samTekst = false;

  if (edytor) {
    edytor.addEventListener("input", function () { pustyNaAkapit(); zmiana(); });
    edytor.addEventListener("keydown", function (e) {
      samTekst = e.shiftKey && (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v";
      var el = elementKursora();
      if (e.key === "Tab" && el && el.closest("td, th")) {
        e.preventDefault();
        przejdzKomorka(el.closest("td, th"), e.shiftKey);
        return;
      }
      if (e.key === "Enter" && !e.shiftKey && el && el.closest("pre")) {
        e.preventDefault();
        enterWKodzie(el.closest("pre"));
      }
    });
    edytor.addEventListener("paste", function (e) {
      var dt = e.clipboardData;
      if (!dt) { return; }
      e.preventDefault();
      var html = samTekst ? "" : dt.getData("text/html");
      var obrazyZeSchowka = tylkoObraz(dt, html);
      if (obrazyZeSchowka) { wstawObrazy(obrazyZeSchowka); return; }
      wklejHtml(html, dt.getData("text/plain"));
    });
    edytor.addEventListener("drop", function (e) {
      var dt = e.dataTransfer;
      if (!dt) { return; }
      var html = dt.getData("text/html");
      var pliki = tylkoObraz(dt, html);
      if (!pliki && !html && !dt.getData("text/plain")) { return; }
      e.preventDefault();
      var r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
      if (r) { var s = window.getSelection(); s.removeAllRanges(); s.addRange(r); }
      if (pliki) { wstawObrazy(pliki); } else { wklejHtml(html, dt.getData("text/plain")); }
    });
  }

  function przejdzKomorka(komorka, wstecz) {
    var tabela = komorka.closest("table");
    var wszystkie = Array.prototype.slice.call(tabela.querySelectorAll("th, td"));
    var i = wszystkie.indexOf(komorka) + (wstecz ? -1 : 1);
    if (i < 0) { return; }
    if (i >= wszystkie.length) {
      var wiersz = komorka.closest("tr"), nowy = document.createElement("tr");
      Array.prototype.forEach.call(wiersz.children, function () {
        nowy.appendChild(document.createElement("td")).appendChild(document.createElement("br"));
      });
      (tabela.tBodies[0] || tabela.createTBody()).appendChild(nowy);
      wszystkie = Array.prototype.slice.call(tabela.querySelectorAll("th, td"));
      zmiana();
    }
    // Jak w arkuszu: zaznaczona zawartosc komorki, pisanie ja zastepuje.
    var r = document.createRange();
    r.selectNodeContents(wszystkie[i]);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }

  // Enter w bloku kodu to nowa linia kodu; Enter na pustej ostatniej linii
  // wychodzi z bloku do zwyklego akapitu.
  function enterWKodzie(pre) {
    var sel = window.getSelection(), zakres = sel.getRangeAt(0);
    var przed = document.createRange(), po = document.createRange();
    przed.selectNodeContents(pre);
    przed.setEnd(zakres.startContainer, zakres.startOffset);
    po.selectNodeContents(pre);
    po.setStart(zakres.endContainer, zakres.endOffset);
    if (!po.toString().replace(/\n$/, "") && /\n$/.test(przed.toString())) {
      pre.textContent = pre.textContent.replace(/\n+$/, "");
      var p = document.createElement("p");
      p.appendChild(document.createElement("br"));
      pre.parentNode.insertBefore(p, pre.nextSibling);
      var r = document.createRange();
      r.setStart(p, 0);
      r.collapse(true);
      sel.removeAllRanges();
      sel.addRange(r);
    } else {
      document.execCommand("insertLineBreak");
    }
    zmiana();
  }

  // --- pasek narzedzi ------------------------------------------------------------------------

  function akcjaWizualna(nazwa) {
    var sel = wEdytorze();
    var zaznaczone = sel.toString();
    if (nazwa === "kod") {
      document.execCommand("insertHTML", false, "<code>" + esc(zaznaczone || "polecenie") + "</code>\u200b");
    } else if (nazwa === "blok-kodu") {
      document.execCommand("insertHTML", false, '<pre data-kb-nowy-blok="">' + esc(zaznaczone || "polecenie") + "</pre><p><br></p>");
      zaznaczWstawiony("pre[data-kb-nowy-blok]", !zaznaczone);
    } else if (nazwa === "tabela") {
      var wiersz = "<tr><td><br></td><td><br></td><td><br></td></tr>";
      document.execCommand("insertHTML", false, '<table data-kb-nowy-blok=""><thead><tr><th>Kolumna</th><th>Kolumna</th><th>Kolumna</th></tr></thead><tbody>' +
        wiersz + wiersz + "</tbody></table><p><br></p>");
      zaznaczWstawiony("table[data-kb-nowy-blok] th", true);
    } else if (nazwa === "link") {
      zapamietany = sel.rangeCount ? sel.getRangeAt(0).cloneRange() : null;
      adresLinku.value = /^https?:\/\//.test(zaznaczone) ? zaznaczone : "";
      oknoLinku.hidden = false;
      adresLinku.focus();
      return;
    } else {
      document.execCommand(nazwa, false, null);
    }
    sprzatnij();
    wypelnijPuste();
    zmiana();
  }

  // Kursor do srodka wstawionego bloku (tekst przykladowy zaznaczony) -
  // inaczej zostawalby w akapicie pod blokiem.
  function zaznaczWstawiony(selektor, calosc) {
    var el = edytor.querySelector(selektor);
    edytor.querySelectorAll("[data-kb-nowy-blok]").forEach(function (b) { b.removeAttribute("data-kb-nowy-blok"); });
    if (!el) { return; }
    var r = document.createRange();
    r.selectNodeContents(el);
    if (!calosc) { r.collapse(false); }
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
  }

  function wstawLink() {
    var url = adresLinku.value.trim();
    if (url && !/^(https?:\/\/|mailto:|\/)/i.test(url)) { url = "https://" + url; }
    oknoLinku.hidden = true;
    adresLinku.value = "";
    if (!url) { edytor.focus(); return; }
    var sel = window.getSelection();
    edytor.focus();
    if (zapamietany) { sel.removeAllRanges(); sel.addRange(zapamietany); }
    if (sel.toString()) { document.execCommand("createLink", false, url); }
    else { document.execCommand("insertHTML", false, '<a href="' + esc(url) + '">' + esc(url) + "</a>\u200b"); }
    sprzatnij();
    zmiana();
  }

  if (narzedzia) {
    narzedzia.hidden = false;
    narzedzia.querySelectorAll("button[data-wiz], button[data-md], button[data-md-linia]").forEach(function (b) {
      // Klikniecie przycisku nie zabiera zaznaczenia z edytora.
      b.addEventListener("mousedown", function (e) { if (edytor && !trybMd) { e.preventDefault(); } });
      b.addEventListener("click", function () {
        if (b.dataset.wiz === "obraz") { wyborObrazu.click(); return; }
        if (edytor && !trybMd) { akcjaWizualna(b.dataset.wiz); return; }
        if (b.dataset.mdLinia) { naPoczatkuLinii(b.dataset.mdLinia); return; }
        if (b.dataset.md) { var czesci = b.dataset.md.split("|"); wstaw(czesci[0], czesci[1] || ""); }
      });
    });
    var styl = narzedzia.querySelector("[data-kb-styl]");
    if (styl) {
      styl.addEventListener("change", function () {
        if (edytor && !trybMd) {
          wEdytorze();
          document.execCommand("formatBlock", false, "<" + styl.value + ">");
          sprzatnij();
          zmiana();
          edytor.focus();
        } else {
          naPoczatkuLinii({ h2: "## ", h3: "### ", h4: "#### " }[styl.value] || "");
          styl.value = "p";
        }
      });
    }
    var panel = narzedzia.querySelector("[data-kb-panel]");
    if (panel) {
      panel.addEventListener("change", function () {
        var rodzaj = panel.value, nazwa = panel.options[panel.selectedIndex].text;
        panel.value = "";
        if (!rodzaj) { return; }
        if (edytor && !trybMd) {
          var zaznaczone = wEdytorze().toString();
          document.execCommand("insertHTML", false, '<div class="kb-panel kb-panel-' + rodzaj + '" data-kb-nowy-blok=""><div class="kb-panel-tytul">' +
            esc(nazwa) + "</div><p>" + (zaznaczone ? esc(zaznaczone) : "<br>") + "</p></div><p><br></p>");
          zaznaczWstawiony("[data-kb-nowy-blok] > p", false);
          sprzatnij();
          zmiana();
        } else {
          wstaw("\n:::" + rodzaj + " " + nazwa + "\n", "\n:::\n");
        }
      });
    }
    var pole = narzedzia.querySelector("[data-md-pole]");
    if (pole) {
      pole.addEventListener("change", function () {
        var wartosc = pole.value;
        pole.value = "";
        if (!wartosc) { return; }
        if (edytor && !trybMd) { wEdytorze(); document.execCommand("insertText", false, wartosc); zmiana(); }
        else { wstaw(wartosc, ""); }
      });
    }
    narzedzia.querySelectorAll("[data-kb-tryb]").forEach(function (b) {
      b.addEventListener("click", function () { ustawTryb(b.dataset.kbTryb === "md", true); });
    });
  }
  if (wyborObrazu) {
    wyborObrazu.addEventListener("change", function () {
      if (wyborObrazu.files.length) { wstawObrazy(wyborObrazu.files); }
      wyborObrazu.value = "";
    });
  }
  if (oknoLinku) {
    formularz.querySelector("[data-kb-link-wstaw]").addEventListener("click", wstawLink);
    formularz.querySelector("[data-kb-link-anuluj]").addEventListener("click", function () { oknoLinku.hidden = true; edytor.focus(); });
    adresLinku.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); wstawLink(); }
      if (e.key === "Escape") { oknoLinku.hidden = true; edytor.focus(); }
    });
  }

  // --- zapis ------------------------------------------------------------------------------------

  // Tresc do wyslania: z edytora (jesli go zmieniono) albo z pola Markdown.
  // Obrazy dostaja kolejne numery w kolejnosci wystapienia; wysylamy tylko
  // te, ktore zostaly w tresci.
  formularz.addEventListener("submit", function (e) {
    if (edytor && !trybMd && zmieniony) { tresc.value = KbWklej.doMarkdown(edytor); }
    if (!tresc.value.trim()) {
      e.preventDefault();
      pokaz("Treść artykułu jest pusta.", true);
      (edytor && !trybMd ? edytor : tresc).focus();
      return;
    }
    var numery = {}, pliki = [];
    tresc.value = tresc.value.replace(/\]\(kb-obraz:(\d+)\)/g, function (cale, n) {
      if (!obrazy[+n]) { return cale; }
      if (!(n in numery)) { numery[n] = pliki.length; pliki.push(obrazy[+n].plik); }
      return "](kb-obraz:" + numery[n] + ")";
    });
    if (!poleObrazow) { return; }
    try {
      var dt = new DataTransfer();
      pliki.forEach(function (p) { dt.items.add(p); });
      poleObrazow.files = dt.files;
    } catch (blad) {
      if (pliki.length) {
        e.preventDefault();
        pokaz("Ta przeglądarka nie umie wysłać wklejonych obrazów. Usuń je z treści i dodaj jako załączniki po zapisaniu.", true);
      }
    }
  });

  // --- podglad (tryb Markdown) ---------------------------------------------------------------------

  if (przyciskPodgladu && podglad) {
    przyciskPodgladu.addEventListener("click", function () {
      if (!podglad.hidden) {
        podglad.hidden = true;
        tresc.hidden = false;
        przyciskPodgladu.textContent = "Podgląd";
        tresc.focus();
        return;
      }
      przyciskPodgladu.disabled = true;
      renderujNaSerwerze(tresc.value, false)
        .then(function (html) {
          podglad.innerHTML = html;
          podglad.querySelectorAll("img").forEach(function (img) {
            var m = /^kb-obraz:(\d+)$/.exec(img.getAttribute("src") || "");
            if (m && obrazy[+m[1]]) { img.setAttribute("src", obrazy[+m[1]].dane); }
          });
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

  // Tryb startowy: edytor wizualny, chyba ze ktos wybral wczesniej Markdown.
  if (edytor) {
    tresc.hidden = true;
    tresc.required = false;
    var pomocWizualna = formularz.querySelector("[data-kb-pomoc-wiz]");
    var pomocMarkdown = formularz.querySelector("[data-kb-pomoc-md]");
    if (pomocWizualna) { pomocWizualna.hidden = false; }
    if (pomocMarkdown) { pomocMarkdown.hidden = true; }
    var zapisany = null;
    try { zapisany = localStorage.getItem(KLUCZ_TRYBU); } catch (e) { /* bez pamieci */ }
    if (zapisany === "md") { ustawTryb(true, false); }
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
