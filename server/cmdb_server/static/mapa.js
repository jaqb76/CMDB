/* Mapa relacji: klaster -> host -> VM/serwer -> aplikacja.
 *
 * Dane pobieramy z adresu w data-mapa-zrodlo (CSP panelu nie dopuszcza
 * skryptow ani blokow danych w tresci strony). Wszystko
 * budujemy przez DOM (textContent), a nie przez innerHTML: nazwy zasobow
 * pochodza z raportow agentow i z Nutanixa, wiec nie wolno ich traktowac
 * jak znacznikow.
 */
(function () {
  "use strict";
  var korzen = document.querySelector("[data-mapa]");
  if (!korzen) { return; }
  fetch(korzen.dataset.mapaZrodlo, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (odp) {
      if (!odp.ok) { throw new Error("HTTP " + odp.status); }
      return odp.json();
    })
    .then(uruchom)
    .catch(function (blad) {
      korzen.querySelector("[data-mapa-plotno]").textContent = "Nie udało się wczytać mapy (" + blad.message + "). Odśwież stronę.";
    });

  function uruchom(dane) {
  var SVG = "http://www.w3.org/2000/svg";
  var TYTULY = ["Klastry", "Hosty", "VM / serwery", "Aplikacje"];
  var ETYKIETY = { klaster: "Klaster", host: "Host", vm: "Maszyna wirtualna", komputer: "Komputer / serwer", aplikacja: "Aplikacja" };
  var ZRODLA = { agent: "agent CMDB", nutanix: "Nutanix", reczne: "wpis ręczny" };

  var wezly = {};
  dane.wezly.forEach(function (w) { wezly[w.id] = w; });
  // "gora": od czego zasob zalezy (zrodlo relacji -> cel); "dol": co zalezy od niego.
  var gora = {}, dol = {};
  dane.krawedzie.forEach(function (k) {
    (gora[k.z] = gora[k.z] || []).push(k.do);
    (dol[k.do] = dol[k.do] || []).push(k.z);
  });

  var stan = {
    focus: wezly[dane.zasob] ? dane.zasob : dane.wezly[0].id,
    kierunek: dane.kierunek || "w-dol",
    tylko: true,
    historia: []
  };
  stan.historia.push(stan.focus);

  function el(nazwa, atrybuty, tekst, przestrzen) {
    var e = przestrzen ? document.createElementNS(przestrzen, nazwa) : document.createElement(nazwa);
    Object.keys(atrybuty || {}).forEach(function (k) { e.setAttribute(k, atrybuty[k]); });
    if (tekst !== undefined && tekst !== null) { e.textContent = tekst; }
    return e;
  }

  function zasieg(start, mapa) {
    var widziane = {}, kolejka = [start];
    while (kolejka.length) {
      var n = kolejka.shift();
      (mapa[n] || []).forEach(function (m) {
        if (!widziane[m] && m !== start) { widziane[m] = true; kolejka.push(m); }
      });
    }
    return widziane;
  }

  function skroc(tekst, dlugosc) {
    tekst = tekst || "";
    return tekst.length > dlugosc ? tekst.slice(0, dlugosc - 1) + "…" : tekst;
  }

  function bezAgenta(w) {
    return (w.typ === "vm" || w.typ === "komputer") && w.zrodlo !== "agent";
  }

  function rysuj() {
    var wDol = stan.kierunek !== "w-gore" ? zasieg(stan.focus, dol) : {};
    var wGore = stan.kierunek !== "w-dol" ? zasieg(stan.focus, gora) : {};
    var powiazane = {};
    Object.keys(wDol).concat(Object.keys(wGore)).forEach(function (id) { powiazane[id] = true; });
    powiazane[stan.focus] = true;

    var widoczne = dane.wezly.filter(function (w) { return !stan.tylko || powiazane[w.id]; });
    var kolumny = [[], [], [], []];
    widoczne.forEach(function (w) { kolumny[w.kolumna].push(w); });

    var szer = 180, odstep = 48, wys = 46, pion = 10, gorny = 34, lewy = 12;
    var maks = Math.max(1, kolumny[0].length, kolumny[1].length, kolumny[2].length, kolumny[3].length);
    var H = gorny + maks * (wys + pion) + 8;
    var W = lewy * 2 + 4 * szer + 3 * odstep;
    var poz = {};
    kolumny.forEach(function (lista, i) {
      var przes = gorny + (maks - lista.length) * (wys + pion) / 2;
      lista.forEach(function (w, j) { poz[w.id] = { x: lewy + i * (szer + odstep), y: przes + j * (wys + pion) }; });
    });

    var svg = el("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img",
                          "aria-label": "Mapa relacji zasobów" }, null, SVG);
    TYTULY.forEach(function (t, i) {
      svg.appendChild(el("text", { "class": "mapa-kolumna", x: lewy + i * (szer + odstep), y: 18 }, t, SVG));
    });
    dane.krawedzie.forEach(function (k) {
      var a = poz[k.z], b = poz[k.do];
      if (!a || !b) { return; }
      var on = powiazane[k.z] && powiazane[k.do];
      // Krawedz od prawej krawedzi "rodzica" (lewa kolumna) do lewej krawedzi dziecka.
      var lewo = a.x < b.x ? a : b, prawo = a.x < b.x ? b : a;
      var x1 = lewo.x + szer, y1 = lewo.y + wys / 2, x2 = prawo.x, y2 = prawo.y + wys / 2;
      if (a.x === b.x) { x1 = a.x + szer; x2 = b.x + szer; }
      var cx = (x1 + x2) / 2 + (a.x === b.x ? 40 : 0);
      svg.appendChild(el("path", {
        "class": "mapa-krawedz" + (on ? " on" : " dim"),
        d: "M" + x1 + "," + y1 + " C" + cx + "," + y1 + " " + cx + "," + y2 + " " + x2 + "," + y2
      }, null, SVG));
    });
    widoczne.forEach(function (w) {
      var p = poz[w.id];
      var klasa = "mapa-wezel" + (w.id === stan.focus ? " focus" : powiazane[w.id] ? " on" : " dim") +
                  (w.wycofany ? " wycofany" : "");
      var g = el("g", { "class": klasa, tabindex: "0", role: "button", "data-id": w.id,
                        transform: "translate(" + p.x + "," + p.y + ")",
                        "aria-label": (ETYKIETY[w.typ] || w.typ) + " " + w.nazwa }, null, SVG);
      g.appendChild(el("title", {}, w.nazwa + (w.opis ? " — " + w.opis : ""), SVG));
      g.appendChild(el("rect", { "class": "ramka", width: szer, height: wys, rx: 7 }, null, SVG));
      g.appendChild(el("rect", { "class": "mapa-pas typ-" + w.kolumna, width: 5, height: wys, rx: 2 }, null, SVG));
      g.appendChild(el("text", { x: 14, y: 19 }, skroc(w.nazwa, 22), SVG));
      g.appendChild(el("text", { "class": "opis", x: 14, y: 35 },
                       skroc(w.wycofany ? "wycofany" : (w.opis || ETYKIETY[w.typ] || w.typ), 26), SVG));
      if (bezAgenta(w)) {
        g.appendChild(el("text", { "class": "ostrzezenie", x: szer - 10, y: 19, "text-anchor": "end" }, "⚠", SVG));
      }
      g.addEventListener("click", function () { ustaw(w.id); });
      g.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); ustaw(w.id); }
      });
      svg.appendChild(g);
    });

    var plotno = korzen.querySelector("[data-mapa-plotno]");
    plotno.replaceChildren(svg);
    boczny(wDol, wGore);
    sciezka();
    korzen.querySelector("[data-mapa-wybor]").value = stan.focus;
    korzen.querySelectorAll("[data-kierunek]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.kierunek === stan.kierunek));
    });
    if (window.history && history.replaceState) {
      history.replaceState(null, "", "/relacje/mapa?widok=sciezka&zasob=" + encodeURIComponent(stan.focus) +
                           "&kierunek=" + stan.kierunek);
    }
  }

  function lista(tytul, ids) {
    var blok = el("div");
    blok.appendChild(el("div", { "class": "small muted" }, tytul + " (" + ids.length + "):"));
    var ul = el("ul", { "class": "mapa-lista" });
    ids.sort(function (a, b) {
      return wezly[a].kolumna - wezly[b].kolumna || wezly[a].nazwa.localeCompare(wezly[b].nazwa);
    }).forEach(function (id) {
      var li = el("li");
      var b = el("button", { type: "button" }, wezly[id].nazwa);
      b.addEventListener("click", function () { ustaw(id); });
      li.appendChild(b);
      li.appendChild(el("span", { "class": "muted small" }, " " + (ETYKIETY[wezly[id].typ] || wezly[id].typ)));
      ul.appendChild(li);
    });
    blok.appendChild(ul);
    return blok;
  }

  function boczny(wDol, wGore) {
    var w = wezly[stan.focus];
    var bok = korzen.querySelector("[data-mapa-bok]");
    var zalezne = Object.keys(wDol), podstawy = Object.keys(wGore);
    var ile = function (ids, typy) { return ids.filter(function (id) { return typy.indexOf(wezly[id].typ) >= 0; }).length; };
    var dzieci = [];

    var h = el("h3", {}, w.nazwa + " ");
    h.appendChild(el("span", { "class": "badge" + (w.zrodlo === "agent" ? " badge-ok" : "") }, ZRODLA[w.zrodlo] || w.zrodlo));
    dzieci.push(h);
    dzieci.push(el("div", { "class": "muted small" }, (ETYKIETY[w.typ] || w.typ) + (w.opis ? " · " + w.opis : "")));
    if (bezAgenta(w)) {
      dzieci.push(el("div", { "class": "badge badge-warn" }, "⚠ bez agenta CMDB — brak inwentaryzacji oprogramowania"));
    }
    if (stan.kierunek !== "w-gore") {
      var zasieg = el("div", { "class": "mapa-zasieg" });
      zasieg.appendChild(el("span", { "class": "small muted" }, "Gdy ten zasób przestanie działać, ucierpi:"));
      zasieg.appendChild(el("b", {}, ile(zalezne, ["vm", "komputer"]) + " VM/serwerów · " +
                                    ile(zalezne, ["aplikacja"]) + " aplikacji"));
      if (ile(zalezne, ["host"])) {
        zasieg.appendChild(el("span", { "class": "small muted" }, "na " + ile(zalezne, ["host"]) + " hostach"));
      }
      dzieci.push(zasieg);
    }
    if (stan.kierunek !== "w-dol" && podstawy.length) { dzieci.push(lista("Stoi na", podstawy)); }
    if (stan.kierunek !== "w-gore" && zalezne.length) { dzieci.push(lista("Zależy od niego", zalezne)); }
    var karta = el("p", { "class": "small" });
    karta.appendChild(el("a", { href: "/assets/" + encodeURIComponent(w.id) }, "Otwórz kartę zasobu →"));
    dzieci.push(karta);
    bok.replaceChildren.apply(bok, dzieci);
  }

  function sciezka() {
    var s = korzen.querySelector("[data-mapa-sciezka]");
    var dzieci = [document.createTextNode("Ścieżka: ")];
    stan.historia.forEach(function (id, i) {
      if (i === stan.historia.length - 1) {
        dzieci.push(el("b", {}, wezly[id].nazwa));
      } else {
        var b = el("button", { type: "button" }, wezly[id].nazwa);
        b.addEventListener("click", function () {
          stan.historia = stan.historia.slice(0, i + 1);
          stan.focus = id;
          rysuj();
        });
        dzieci.push(b, document.createTextNode(" › "));
      }
    });
    s.replaceChildren.apply(s, dzieci);
  }

  function ustaw(id) {
    if (!wezly[id] || id === stan.focus) { return; }
    stan.focus = id;
    stan.historia.push(id);
    if (stan.historia.length > 8) { stan.historia.shift(); }
    rysuj();
  }

  var wybor = korzen.querySelector("[data-mapa-wybor]");
  [0, 1, 2, 3].forEach(function (k) {
    var grupa = el("optgroup", { label: TYTULY[k] });
    dane.wezly.filter(function (w) { return w.kolumna === k; }).forEach(function (w) {
      grupa.appendChild(el("option", { value: w.id }, w.nazwa));
    });
    if (grupa.children.length) { wybor.appendChild(grupa); }
  });
  wybor.addEventListener("change", function () { ustaw(wybor.value); });
  korzen.querySelectorAll("[data-kierunek]").forEach(function (b) {
    b.addEventListener("click", function () { stan.kierunek = b.dataset.kierunek; rysuj(); });
  });
  korzen.querySelector("[data-mapa-tylko]").addEventListener("change", function (e) {
    stan.tylko = e.target.checked;
    rysuj();
  });
  rysuj();
  }
})();
