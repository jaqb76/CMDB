/* Mapa relacji - widok "grupy".
 *
 * Kazdy host to zwarta grupa: blok hosta, pod nim jego maszyny w kolumnach,
 * polaczone liniami jak w schemacie organizacyjnym. Grupy ukladaja sie
 * w rzedy pod klastrem, a szerokosc rzedu dobieramy tak, zeby calosc miala
 * proporcje okna. Linie prowadzone sa wylacznie wewnatrz grupy (i wzdluz
 * lewej krawedzi sekcji klastra), wiec nie przecinaja sie wcale. Wyjatkiem
 * sa aplikacje - jedna bywa na kilku hostach - dlatego sa domyslnie ukryte.
 *
 * Nazwy pochodza z raportow i z Nutanixa: wstawiamy je wylacznie przez
 * textContent, nigdy jako znaczniki.
 */
(function () {
  "use strict";
  var korzen = document.querySelector("[data-mapa-grupy]");
  if (!korzen) { return; }

  fetch(korzen.dataset.mapaZrodlo, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (odp) { if (!odp.ok) { throw new Error("HTTP " + odp.status); } return odp.json(); })
    .then(uruchom)
    .catch(function (blad) {
      korzen.querySelector("[data-mg-plotno]").textContent =
        "Nie udało się wczytać mapy (" + blad.message + "). Odśwież stronę.";
    });

  function uruchom(dane) {
    var NS = "http://www.w3.org/2000/svg";
    var BEZ_KLASTRA = "__bez-klastra", BEZ_HOSTA = "__bez-hosta";
    var MASZYNY = { vm: 1, komputer: 1 };
    var STANY = { ok: "z agentem CMDB", bez: "bez agenta CMDB", wyl: "wyłączona", bad: "agent milczy" };
    var TYPY = { klaster: "klaster", host: "host", aplikacja: "aplikacja", vm: "maszyna wirtualna", komputer: "komputer / serwer" };

    // --- hierarchia z relacji -------------------------------------------------
    var W = {};
    dane.wezly.forEach(function (w) { W[w.id] = w; });
    var hostKlastra = {}, hostMaszyny = {}, apkiMaszyny = [];
    dane.krawedzie.forEach(function (k) {
      if (!W[k.z] || !W[k.do]) { return; }
      if (k.rodzaj === "host_cluster") { hostKlastra[k.z] = k.do; }
      else if (k.rodzaj === "vm_host") { hostMaszyny[k.z] = k.do; }
      else if (k.rodzaj === "application_server") { apkiMaszyny.push([k.do, k.z]); } // [maszyna, aplikacja]
    });
    var hostyKlastra = {}, vmHosta = {};
    Object.keys(W).forEach(function (id) {
      var w = W[id];
      var jestHostem = w.typ === "host" || hostKlastra[id] || Object.keys(hostMaszyny).some(function (v) { return hostMaszyny[v] === id; });
      if (w.typ === "klaster") { hostyKlastra[id] = hostyKlastra[id] || []; return; }
      if (w.typ === "aplikacja") { return; }
      if (jestHostem && !hostMaszyny[id]) {
        var k = hostKlastra[id] || BEZ_KLASTRA;
        (hostyKlastra[k] = hostyKlastra[k] || []).push(id);
        vmHosta[id] = vmHosta[id] || [];
      }
    });
    Object.keys(W).forEach(function (id) {
      var w = W[id];
      if (w.typ === "klaster" || w.typ === "aplikacja" || vmHosta[id]) { return; }
      var h = hostMaszyny[id];
      if (!h || !vmHosta[h]) { h = BEZ_HOSTA; }
      (vmHosta[h] = vmHosta[h] || []).push(id);
    });
    if (vmHosta[BEZ_HOSTA]) {
      W[BEZ_HOSTA] = { id: BEZ_HOSTA, nazwa: "Bez hosta", typ: "host", sztuczny: true, opis: "maszyny bez relacji VM → host" };
      (hostyKlastra[BEZ_KLASTRA] = hostyKlastra[BEZ_KLASTRA] || []).push(BEZ_HOSTA);
    }
    if (hostyKlastra[BEZ_KLASTRA]) {
      W[BEZ_KLASTRA] = { id: BEZ_KLASTRA, nazwa: "Bez klastra", typ: "klaster", sztuczny: true, opis: "hosty bez relacji host → klaster" };
    }
    var nazwa = function (id) { return W[id].nazwa; };
    Object.keys(vmHosta).forEach(function (h) { vmHosta[h].sort(function (a, b) { return nazwa(a).localeCompare(nazwa(b)); }); });
    Object.keys(hostyKlastra).forEach(function (k) { hostyKlastra[k].sort(function (a, b) { return nazwa(a).localeCompare(nazwa(b)); }); });
    var klastry = Object.keys(hostyKlastra).sort(function (a, b) {
      return (a === BEZ_KLASTRA) - (b === BEZ_KLASTRA) || nazwa(a).localeCompare(nazwa(b));
    });
    var hosty = [].concat.apply([], klastry.map(function (k) { return hostyKlastra[k]; }));
    var aplikacje = Object.keys(W).filter(function (id) { return W[id].typ === "aplikacja"; });
    var klastrHosta = {};
    klastry.forEach(function (k) { hostyKlastra[k].forEach(function (h) { klastrHosta[h] = k; }); });
    var hostVm = {};
    Object.keys(vmHosta).forEach(function (h) { vmHosta[h].forEach(function (v) { hostVm[v] = h; }); });
    function stan(id) { return W[id].stan || "ok"; }

    // --- wymiary (jednostki sceny) ---------------------------------------------
    var VM_S = 158, VM_W = 20, VM_ODST = 5, KOL_S = VM_S + 22;
    var HOST_S = 180, HOST_W = 28, KL_S = 200, KL_W = 34, APP_S = 160, APP_W = 26;
    var MAKS_W_KOLUMNIE = 12, ODST_GRUP = 26, ODST_RZEDOW = 40, ODST_KLASTROW = 60;

    var filtr = { ok: true, bez: true, wyl: true, bad: true }, apki = false, zwiniete = {}, wgRozmiaru = false;
    var zaznaczony = W[korzen.dataset.mapaWybrany] ? korzen.dataset.mapaWybrany : null;
    var widok = { x: 0, y: 0, s: 1 }, sledzOkno = !zaznaczony;
    var plotno = korzen.querySelector("[data-mg-plotno]"), svg = korzen.querySelector("[data-mg-svg]");
    var scena = korzen.querySelector("[data-mg-scena]"), gK = korzen.querySelector("[data-mg-krawedzie]");
    var gW = korzen.querySelector("[data-mg-wezly]");

    function el(n, a, t) {
      var e = document.createElementNS(NS, n);
      Object.keys(a || {}).forEach(function (k) { e.setAttribute(k, a[k]); });
      if (t !== undefined && t !== null) { e.textContent = t; }
      return e;
    }
    function skroc(t, n) { return t.length > n ? t.slice(0, n - 1) + "…" : t; }
    function vmWidoczne(h) {
      return zwiniete[h] ? [] : (vmHosta[h] || []).filter(function (v) { return filtr[stan(v)]; });
    }

    // --- uklad --------------------------------------------------------------------
    function grupa(h) {
      var vms = vmWidoczne(h), kol = Math.max(1, Math.ceil(vms.length / MAKS_W_KOLUMNIE));
      var wKol = Math.ceil(vms.length / kol);
      return { h: h, vms: vms, kol: kol, wKol: wKol, szer: Math.max(HOST_S, kol * KOL_S),
               wys: HOST_W + (vms.length ? 16 + wKol * (VM_W + VM_ODST) : 0) };
    }
    function sekcja(k, maxSzer) {
      var gr = hostyKlastra[k].map(grupa);
      if (wgRozmiaru) { gr.sort(function (a, b) { return b.vms.length - a.vms.length; }); }
      var rzedy = [], rz = null;
      gr.forEach(function (g) {
        if (!rz || rz.szer + ODST_GRUP + g.szer > maxSzer) { rz = { grupy: [], szer: 30 - ODST_GRUP, wys: 0 }; rzedy.push(rz); }
        rz.grupy.push(g); rz.szer += ODST_GRUP + g.szer; rz.wys = Math.max(rz.wys, g.wys);
      });
      return { k: k, rzedy: rzedy,
               szer: Math.max(KL_S, Math.max.apply(null, rzedy.map(function (r) { return r.szer; }).concat([0]))),
               wys: KL_W + rzedy.reduce(function (s, r) { return s + ODST_RZEDOW + r.wys; }, 0) };
    }
    function uklad() {
      var b = plotno.getBoundingClientRect(), cel = b.width / Math.max(1, b.height);
      var szer = hosty.map(function (h) { return grupa(h).szer; });
      var najw = Math.max.apply(null, szer.concat([KL_S])) + 30;
      var suma = szer.reduce(function (s, x) { return s + x + ODST_GRUP; }, 30);
      var najlepszy = null;
      for (var t = 0; t <= 24; t++) {
        var maxSzer = najw + Math.max(0, suma - najw) * t / 24;
        var sek = klastry.map(function (k) { return sekcja(k, maxSzer); });
        var S = Math.max.apply(null, sek.map(function (s) { return s.szer; })) + (apki ? APP_S + 80 : 0);
        var H = sek.reduce(function (s, x) { return s + x.wys; }, 0) + ODST_KLASTROW * (sek.length - 1);
        var blad = Math.abs(Math.log((S / H) / cel));
        if (!najlepszy || blad < najlepszy.blad) { najlepszy = { blad: blad, sek: sek }; }
      }
      return najlepszy.sek;
    }

    // --- rysowanie -----------------------------------------------------------------
    var poz = {}, dom = {}, sciezki = [];
    function blok(id, x, y, sz, wy) {
      var w = W[id], s = MASZYNY[w.typ] ? stan(id) : "";
      var klasa = "mg-w mg-" + (MASZYNY[w.typ] ? "vm" : w.typ) + (s ? " mg-" + s : "") +
                  (zwiniete[id] ? " mg-zwiniety" : "") + (w.wycofany ? " mg-wycofany" : "") + (w.sztuczny ? " mg-sztuczny" : "");
      var g = el("g", { "class": klasa, transform: "translate(" + x + "," + y + ")", tabindex: "0", role: "button",
                        "aria-label": (TYPY[w.typ] || w.typ) + " " + w.nazwa });
      g.appendChild(el("title", {}, w.nazwa + (w.opis ? " — " + w.opis : "") + (s ? " (" + STANY[s] + ")" : "")));
      g.appendChild(el("rect", { "class": "mg-ramka", width: sz, height: wy, rx: 5 }));
      g.appendChild(el("rect", { "class": "mg-pas", width: 5, height: wy, rx: 2 }));
      var t = el("text", { x: 11, y: wy / 2 + 4 }, MASZYNY[w.typ] ? skroc(w.nazwa, 24) : skroc(w.nazwa, 30));
      if (w.typ === "host" && vmHosta[id]) {
        t.appendChild(el("tspan", { "class": "mg-licznik", dx: 6 }, vmHosta[id].length + " VM" + (zwiniete[id] ? " · zwinięty" : "")));
      }
      if (w.typ === "klaster") { t.appendChild(el("tspan", { "class": "mg-licznik", dx: 6 }, hostyKlastra[id].length + " hostów")); }
      g.appendChild(t);
      podepnij(g, id);
      gW.appendChild(g); dom[id] = g;
      poz[id] = { x: x, y: y, sz: sz, wy: wy };
    }
    function linia(d, klasa, ids) {
      var p = el("path", { "class": "mg-kr " + klasa, d: d });
      gK.appendChild(p); sciezki.push({ p: p, ids: ids });
    }
    function rysuj() {
      gK.replaceChildren(); gW.replaceChildren(); poz = {}; dom = {}; sciezki = [];
      var y0 = 0;
      uklad().forEach(function (s) {
        blok(s.k, 0, y0, KL_S, KL_W);
        var pien = 16, y = y0 + KL_W, dolPnia = y;
        s.rzedy.forEach(function (rz) {
          var szyna = y + ODST_RZEDOW / 2;
          y += ODST_RZEDOW;
          var x = 30;
          rz.grupy.forEach(function (g) {
            blok(g.h, x, y, HOST_S, HOST_W);
            linia("M" + pien + "," + szyna + " H" + (x + 20) + " V" + y, "mg-host", [s.k, g.h]);
            if (g.vms.length) {
              var szynaH = y + HOST_W + 8, xs = [];
              for (var c = 0; c < g.kol; c++) { xs.push(x + 10 + c * KOL_S); }
              linia("M" + (x + 10) + "," + (y + HOST_W) + " V" + szynaH + (g.kol > 1 ? " H" + xs[xs.length - 1] : ""), "mg-host-vm", [g.h]);
              g.vms.forEach(function (v, q) {
                var c = Math.floor(q / g.wKol), r = q % g.wKol;
                var vx = xs[c] + 14, vy = szynaH + 8 + r * (VM_W + VM_ODST), st = stan(v);
                blok(v, vx, vy, VM_S, VM_W);
                linia("M" + xs[c] + "," + (r === 0 ? szynaH : vy - VM_ODST - VM_W / 2) + " V" + (vy + VM_W / 2) + " H" + vx,
                      st === "bez" ? "mg-bez" : st === "wyl" ? "mg-wyl" : "", [g.h, v]);
              });
            }
            x += g.szer + ODST_GRUP;
          });
          dolPnia = szyna; y += rz.wys;
        });
        if (s.rzedy.length) { linia("M" + pien + "," + (y0 + KL_W) + " V" + dolPnia, "mg-host", [s.k]); }
        y0 = y + ODST_KLASTROW;
      });
      if (apki) { rysujAplikacje(); }
      podswietl();
      if (sledzOkno) { dopasuj(); }
      else if (zaznaczony && !wstepnie) { wstepnie = true; najedz(zaznaczony); }
    }
    var wstepnie = false;
    function rysujAplikacje() {
      var maxX = 0, minY = Infinity;
      Object.keys(poz).forEach(function (id) { maxX = Math.max(maxX, poz[id].x + poz[id].sz); minY = Math.min(minY, poz[id].y); });
      aplikacje.forEach(function (a, i) {
        var x = maxX + 80, y = minY + i * 50;
        blok(a, x, y, APP_S, APP_W);
        apkiMaszyny.forEach(function (e) {
          if (e[1] !== a || !poz[e[0]]) { return; }
          var v = poz[e[0]];
          linia("M" + (v.x + v.sz) + "," + (v.y + v.wy / 2) + " L" + x + "," + (y + APP_W / 2), "mg-app", [a, e[0]]);
        });
      });
    }
    function zastosujWidok() {
      scena.setAttribute("transform", "translate(" + widok.x + "," + widok.y + ") scale(" + widok.s + ")");
      svg.setAttribute("data-lod", widok.s < 0.7 ? "daleko" : "blisko");
    }
    function dopasuj(ids) {
      var b = plotno.getBoundingClientRect();
      var lista = (ids || Object.keys(poz)).map(function (id) { return poz[id]; }).filter(Boolean);
      if (!lista.length) { return; }
      var x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
      lista.forEach(function (p) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x + p.sz); y0 = Math.min(y0, p.y); y1 = Math.max(y1, p.y + p.wy); });
      var m = 24, s = Math.min((b.width - 2 * m) / (x1 - x0 || 1), (b.height - 2 * m) / (y1 - y0 || 1), 1.6);
      widok.s = s; widok.x = b.width / 2 - s * (x0 + x1) / 2; widok.y = b.height / 2 - s * (y0 + y1) / 2;
      zastosujWidok();
    }

    // --- zaznaczenie ------------------------------------------------------------------
    function powiazane(id) {
      var w = W[id], s = {}; s[id] = true;
      if (MASZYNY[w.typ] || hostVm[id]) {
        var h = hostVm[id]; if (h) { s[h] = true; s[klastrHosta[h]] = true; }
        apkiMaszyny.forEach(function (e) { if (e[0] === id) { s[e[1]] = true; } });
      }
      if (vmHosta[id]) { s[klastrHosta[id]] = true; vmHosta[id].forEach(function (v) { s[v] = true; }); }
      if (hostyKlastra[id]) { hostyKlastra[id].forEach(function (h) { s[h] = true; (vmHosta[h] || []).forEach(function (v) { s[v] = true; }); }); }
      if (w.typ === "aplikacja") {
        apkiMaszyny.forEach(function (e) {
          if (e[1] !== id) { return; }
          s[e[0]] = true; var h = hostVm[e[0]]; if (h) { s[h] = true; s[klastrHosta[h]] = true; }
        });
      }
      return s;
    }
    function podswietl() {
      var p = zaznaczony ? powiazane(zaznaczony) : null;
      Object.keys(dom).forEach(function (id) {
        dom[id].classList.toggle("mg-dim", !!p && !p[id]);
        dom[id].classList.toggle("mg-zaznaczony", id === zaznaczony);
      });
      var droga = zaznaczony && (hostVm[zaznaczony] || W[zaznaczony].typ === "aplikacja");
      sciezki.forEach(function (q) {
        var on = p && q.ids.every(function (id) { return p[id]; });
        q.p.classList.toggle("mg-dim", !!p && !on);
        q.p.classList.toggle("mg-sciezka", !!(droga && on));
      });
      bok();
      if (window.history && history.replaceState) {
        history.replaceState(null, "", "/relacje/mapa?widok=grupy" + (zaznaczony && !W[zaznaczony].sztuczny ? "&zasob=" + encodeURIComponent(zaznaczony) : ""));
      }
    }
    function najedz(id) {
      var cel = vmHosta[id] ? [id].concat(vmWidoczne(id)) : hostVm[id] ? [id, hostVm[id]].concat(vmWidoczne(hostVm[id]))
              : Object.keys(powiazane(id));
      dopasuj(cel);
    }
    function wybierz(id, przybliz) {
      var h = hostVm[id];
      if (h && (zwiniete[h] || !filtr[stan(id)])) {
        zwiniete[h] = false; filtr[stan(id)] = true;
        korzen.querySelectorAll("[data-mg-filtr]").forEach(function (c) { c.checked = filtr[c.dataset.mgFiltr]; });
        zaznaczony = id; sledzOkno = false; rysuj();
      } else {
        zaznaczony = id; sledzOkno = false; podswietl();
      }
      if (przybliz) { najedz(id); }
    }

    // --- panel boczny --------------------------------------------------------------
    function bok() {
      var b = korzen.querySelector("[data-mg-bok]"), dzieci = [];
      function d(tag, kl, t) { var e = document.createElement(tag); if (kl) { e.className = kl; } if (t != null) { e.textContent = t; } dzieci.push(e); return e; }
      function kafle(ids) {
        var k = d("div", "mg-kafle");
        [["ok", "z agentem"], ["bez", "bez agenta"], ["wyl", "wyłączone"], ["bad", "agent milczy"]].forEach(function (p) {
          var ile = ids.filter(function (v) { return stan(v) === p[0]; }).length;
          if (p[0] === "bad" && !ile) { return; }
          var x = document.createElement("div"); x.className = "mg-kafel";
          var bb = document.createElement("b"); bb.textContent = ile;
          var sp = document.createElement("span"); sp.textContent = p[1];
          x.appendChild(bb); x.appendChild(sp); k.appendChild(x);
        });
      }
      function lista(ids) {
        var ul = d("ul", "mapa-lista");
        ids.slice().sort(function (a, c) { return nazwa(a).localeCompare(nazwa(c)); }).forEach(function (v) {
          var li = document.createElement("li"), k = document.createElement("span");
          k.className = "mg-kropka mg-k-" + stan(v);
          var bt = document.createElement("button"); bt.type = "button"; bt.textContent = nazwa(v);
          bt.addEventListener("click", function () { wybierz(v, true); });
          li.appendChild(k); li.appendChild(bt); ul.appendChild(li);
        });
      }
      var maszyny = Object.keys(hostVm);
      if (!zaznaczony) {
        d("h3", "", "Cała infrastruktura");
        d("div", "small muted", klastry.filter(function (k) { return !W[k].sztuczny; }).length + " klastrów · " +
          hosty.filter(function (h) { return !W[h].sztuczny; }).length + " hostów · " + maszyny.length + " maszyn");
        kafle(maszyny);
        var ranking = hosty.map(function (h) { return [h, (vmHosta[h] || []).filter(function (v) { return stan(v) === "bez"; }).length]; })
          .filter(function (p) { return p[1] > 0; }).sort(function (a, c) { return c[1] - a[1]; }).slice(0, 8);
        if (ranking.length) {
          d("div", "small muted", "Najwięcej maszyn bez agenta:");
          var ul = d("ul", "mapa-lista");
          ranking.forEach(function (p) {
            var li = document.createElement("li"), bt = document.createElement("button");
            bt.type = "button"; bt.textContent = nazwa(p[0]);
            bt.addEventListener("click", function () { wybierz(p[0], true); });
            li.appendChild(bt); li.appendChild(document.createTextNode(" — " + p[1])); ul.appendChild(li);
          });
        }
      } else {
        var w = W[zaznaczony], h3 = d("h3", "", w.nazwa + " ");
        var s = MASZYNY[w.typ] || hostVm[zaznaczony] ? stan(zaznaczony) : "";
        var pil = document.createElement("span"); pil.className = "badge" + (s === "ok" ? " badge-ok" : s === "bez" || s === "bad" ? " badge-warn" : "");
        pil.textContent = s ? STANY[s] : (TYPY[w.typ] || w.typ);
        h3.appendChild(pil);
        if (w.opis) { d("div", "small muted", w.opis); }
        if (hostVm[zaznaczony]) {
          var h = hostVm[zaznaczony];
          d("div", "small", "Stoi na: " + nazwa(h) + (klastrHosta[h] ? " → " + nazwa(klastrHosta[h]) : ""));
          var ap = apkiMaszyny.filter(function (e) { return e[0] === zaznaczony; }).map(function (e) { return nazwa(e[1]); });
          d("div", "small", "Aplikacje: " + (ap.length ? ap.join(", ") : "brak"));
        } else {
          var pod = vmHosta[zaznaczony] ? vmHosta[zaznaczony]
            : hostyKlastra[zaznaczony] ? [].concat.apply([], hostyKlastra[zaznaczony].map(function (x) { return vmHosta[x] || []; }))
            : apkiMaszyny.filter(function (e) { return e[1] === zaznaczony; }).map(function (e) { return e[0]; });
          d("div", "small muted", (w.typ === "aplikacja" ? "Działa na " : "Gdy przestanie działać, ucierpi ") + pod.length + " maszyn:");
          kafle(pod); lista(pod);
        }
        if (!w.sztuczny) {
          var p = d("p", "small"), a = document.createElement("a");
          a.href = "/assets/" + encodeURIComponent(w.id); a.textContent = "Otwórz kartę zasobu →"; p.appendChild(a);
          var p2 = d("p", "small"), a2 = document.createElement("a");
          a2.href = "/relacje/mapa?widok=sciezka&zasob=" + encodeURIComponent(w.id); a2.textContent = "Pokaż w widoku ścieżki →"; p2.appendChild(a2);
        }
      }
      b.replaceChildren.apply(b, dzieci);
    }

    // --- mysz i klawiatura ------------------------------------------------------------
    var ciag = null, przesunieto = false;
    function podepnij(g, id) {
      g.addEventListener("click", function (e) { e.stopPropagation(); if (!przesunieto) { wybierz(id, !hostVm[id]); } });
      g.addEventListener("dblclick", function (e) {
        e.stopPropagation();
        if (vmHosta[id]) { zwiniete[id] = !zwiniete[id]; rysuj(); }
      });
      g.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); wybierz(id, true); } });
    }
    plotno.addEventListener("pointerdown", function (e) {
      if (e.target.closest(".mg-zoom")) { return; }
      ciag = { x: e.clientX, y: e.clientY, wx: widok.x, wy: widok.y }; przesunieto = false;
      plotno.classList.add("mg-ciagnie");
    });
    window.addEventListener("pointermove", function (e) {
      if (!ciag) { return; }
      var dx = e.clientX - ciag.x, dy = e.clientY - ciag.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) { przesunieto = true; }
      if (!przesunieto) { return; }
      widok.x = ciag.wx + dx; widok.y = ciag.wy + dy; sledzOkno = false; zastosujWidok();
    });
    window.addEventListener("pointerup", function (e) {
      if (!ciag) { return; }
      ciag = null; plotno.classList.remove("mg-ciagnie");
      if (!przesunieto && !e.target.closest(".mg-w") && e.target.closest("[data-mg-plotno]") && zaznaczony) { zaznaczony = null; podswietl(); }
      setTimeout(function () { przesunieto = false; }, 0);
    });
    function zoom(f, cx, cy) {
      var b = plotno.getBoundingClientRect();
      cx = cx == null ? b.width / 2 : cx; cy = cy == null ? b.height / 2 : cy;
      var s = Math.max(0.05, Math.min(4, widok.s * f)); f = s / widok.s;
      widok.x = cx - (cx - widok.x) * f; widok.y = cy - (cy - widok.y) * f; widok.s = s;
      sledzOkno = false; zastosujWidok();
    }
    plotno.addEventListener("wheel", function (e) {
      e.preventDefault();
      var b = plotno.getBoundingClientRect();
      zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - b.left, e.clientY - b.top);
    }, { passive: false });
    korzen.querySelector("[data-mg-plus]").addEventListener("click", function () { zoom(1.3); });
    korzen.querySelector("[data-mg-minus]").addEventListener("click", function () { zoom(1 / 1.3); });
    korzen.querySelector("[data-mg-calosc]").addEventListener("click", function () { zaznaczony = null; sledzOkno = true; rysuj(); });
    korzen.querySelector("[data-mg-rozmiar]").addEventListener("change", function (e) { wgRozmiaru = e.target.checked; sledzOkno = true; rysuj(); });
    korzen.querySelector("[data-mg-zwin]").addEventListener("click", function (e) {
      var wszystkie = hosty.every(function (h) { return zwiniete[h]; });
      hosty.forEach(function (h) { zwiniete[h] = !wszystkie; });
      e.target.textContent = wszystkie ? "Zwiń wszystkie hosty" : "Rozwiń wszystkie hosty";
      sledzOkno = true; rysuj();
    });
    korzen.querySelectorAll("[data-mg-filtr]").forEach(function (c) {
      c.addEventListener("change", function () {
        filtr[c.dataset.mgFiltr] = c.checked;
        if (c.dataset.mgFiltr === "ok") { filtr.bad = c.checked; }
        sledzOkno = !zaznaczony; rysuj();
      });
    });
    korzen.querySelector("[data-mg-apki]").addEventListener("change", function (e) { apki = e.target.checked; sledzOkno = !zaznaczony; rysuj(); });
    var lista = document.getElementById("mg-nazwy"), poNazwie = {};
    Object.keys(W).forEach(function (id) {
      if (W[id].sztuczny) { return; }
      poNazwie[W[id].nazwa.toLowerCase()] = id;
      var o = document.createElement("option"); o.value = W[id].nazwa; lista.appendChild(o);
    });
    korzen.querySelector("[data-mg-szukaj]").addEventListener("change", function (e) {
      var v = e.target.value.trim().toLowerCase(); if (!v) { return; }
      var id = poNazwie[v] || poNazwie[Object.keys(poNazwie).find(function (n) { return n.indexOf(v) >= 0; })];
      if (id) { wybierz(id, true); }
    });
    var czekaj;
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        clearTimeout(czekaj);
        czekaj = setTimeout(function () { if (!zaznaczony) { sledzOkno = true; } rysuj(); }, 120);
      }).observe(plotno);
    }
    rysuj();
  }
})();
