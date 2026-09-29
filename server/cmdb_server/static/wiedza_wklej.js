/* Baza wiedzy: tresc wklejona do edytora i zamiana edytora na Markdown.
 *
 * Wiekszosc autorow nie zna Markdownu, a tresc przenosi z Worda, Confluence
 * i Excela. Ten plik:
 *
 * - oczysc(): dane ze schowka (text/html albo text/plain) -> czysty DOM
 *   z samych znacznikow, ktore umie zapisac Markdown portalu. Z Worda
 *   odtwarza listy (akapity z mso-list), tekst w Courier/Consolas zamienia na
 *   kod; z Confluence przenosi panele i makro kodu; tabela z Excela staje sie
 *   tabela z naglowkiem. Style, klasy, skrypty i obce atrybuty nie przechodza.
 * - doMarkdown(): tresc edytora -> Markdown, ktory serwer renderuje
 *   (commonmark + tabele + przekreslenie, panele :::rodzaj, pola {hostname},
 *   obrazy wklejone ze schowka jako ![opis](kb-obraz:N)).
 *
 * Bezpieczenstwo nie zalezy od tego pliku: serwer renderuje Markdown
 * z wylaczonym surowym HTML. Czyszczenie jest po to, zeby tresc wygladala
 * porzadnie i dawala sie edytowac.
 */
var KbWklej = (function () {
  "use strict";

  var PANELE = { info: "Informacja", uwaga: "Uwaga", stop: "Stop", ok: "Gotowe" };
  var MONO = /courier|consolas|monaco|menlo|monospace|lucida console|source code|fira code|jetbrains/i;
  var WYRZUC = "script,style,meta,link,title,xml,head,iframe,object,embed,svg,noscript,template,button,input,select,textarea,colgroup,col";

  function styl(el, nazwa) {
    var s = (el.getAttribute && el.getAttribute("style")) || "";
    var m = new RegExp("(?:^|;)\\s*" + nazwa + "\\s*:\\s*([^;]+)", "i").exec(s);
    return m ? m[1].trim().toLowerCase() : "";
  }
  function klasy(el) { return (el.getAttribute && el.getAttribute("class")) || ""; }

  // --- Word: listy zapisane jako akapity z mso-list -------------------------------

  function listyWorda(doc, raport) {
    var akapity = Array.prototype.slice.call(doc.querySelectorAll("p, h1, h2, h3, h4, h5, h6"))
      .filter(function (p) { return /mso-list\s*:\s*l\d/i.test(p.getAttribute("style") || "") || /MsoListParagraph/.test(klasy(p)); });
    if (!akapity.length) { return; }
    var grupy = [], grupa = null, poprzedni = null;
    akapity.forEach(function (p) {
      var sasiad = p.previousSibling;
      while (sasiad && sasiad.nodeType !== 1) { sasiad = sasiad.previousSibling; }
      if (!grupa || sasiad !== poprzedni) { grupa = []; grupy.push(grupa); }
      grupa.push(p);
      poprzedni = p;
    });
    grupy.forEach(function (akapityGrupy) {
      var korzen = null, stos = [];
      akapityGrupy.forEach(function (p) {
        var poziom = parseInt((/level(\d+)/i.exec(p.getAttribute("style") || "") || [0, 1])[1], 10);
        var znacznik = "";
        p.querySelectorAll("[style]").forEach(function (s) {
          if (/mso-list\s*:\s*ignore/i.test(s.getAttribute("style"))) { znacznik = znacznik || s.textContent; s.remove(); }
        });
        znacznik = znacznik.replace(/\s|\u00a0/g, "");
        var numerowana = /^\(?[0-9a-zA-Z]{1,4}[.)]$/.test(znacznik) && !/^[oO§·]$/.test(znacznik);
        while (stos.length > poziom) { stos.pop(); }
        while (stos.length < poziom) {
          var lista = doc.createElement(numerowana ? "ol" : "ul");
          if (!stos.length) { korzen = korzen || lista; if (korzen !== lista) { korzen.appendChild(lista); } }
          else { var ostatni = stos[stos.length - 1].lastElementChild || stos[stos.length - 1].appendChild(doc.createElement("li")); ostatni.appendChild(lista); }
          stos.push(lista);
        }
        var li = doc.createElement("li");
        while (p.firstChild) { li.appendChild(p.firstChild); }
        stos[stos.length - 1].appendChild(li);
      });
      akapityGrupy[0].parentNode.insertBefore(korzen, akapityGrupy[0]);
      akapityGrupy.forEach(function (p) { p.remove(); });
      raport.listy++;
    });
  }

  // --- Word/Google Docs: akapity pisane czcionka o stalej szerokosci -> blok kodu ---

  function czyMono(el) {
    if (MONO.test(styl(el, "font-family"))) { return true; }
    var teksty = [], w = el.ownerDocument.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) { if (w.currentNode.nodeValue.replace(/\u00a0/g, " ").trim()) { teksty.push(w.currentNode); } }
    if (!teksty.length) { return false; }
    return teksty.every(function (t) {
      for (var n = t.parentNode; n && n !== el.parentNode; n = n.parentNode) {
        if (MONO.test(styl(n, "font-family")) || /^(CODE|KBD|SAMP|TT)$/.test(n.nodeName) || (n.nodeName === "FONT" && MONO.test(n.getAttribute("face") || ""))) { return true; }
      }
      return false;
    });
  }

  function kodZCzcionki(doc, raport) {
    var akapity = Array.prototype.slice.call(doc.querySelectorAll("p")).filter(function (p) { return !p.closest("pre, table, li") && czyMono(p); });
    var i = 0;
    while (i < akapity.length) {
      var seria = [akapity[i]];
      while (i + seria.length < akapity.length) {
        var nast = seria[seria.length - 1].nextElementSibling;
        if (nast !== akapity[i + seria.length]) { break; }
        seria.push(nast);
      }
      var pre = doc.createElement("pre");
      pre.textContent = seria.map(function (p) { return tekstZLiniami(p); }).join("\n");
      seria[0].parentNode.insertBefore(pre, seria[0]);
      seria.forEach(function (p) { p.remove(); });
      raport.kod++;
      i += seria.length;
    }
  }

  function tekstZLiniami(el) {
    var kopia = el.cloneNode(true);
    kopia.querySelectorAll("br").forEach(function (br) { br.replaceWith("\n"); });
    return kopia.textContent.replace(/\u00a0/g, " ").replace(/\s+$/, "");
  }

  // --- Confluence: panele i makro kodu ------------------------------------------------

  function rodzajPanelu(el) {
    var k = klasy(el);
    var typ = el.getAttribute("data-panel-type");
    if (typ) { return { info: "info", note: "info", warning: "uwaga", error: "stop", success: "ok", tip: "ok" }[typ] || "info"; }
    if (/confluence-information-macro-information/.test(k)) { return "info"; }
    if (/confluence-information-macro-note/.test(k)) { return "uwaga"; }
    if (/confluence-information-macro-warning/.test(k)) { return "stop"; }
    if (/confluence-information-macro-tip/.test(k)) { return "ok"; }
    if (/confluence-information-macro/.test(k)) { return "info"; }
    return null;
  }

  function jezykKodu(pre) {
    var parametry = pre.getAttribute("data-syntaxhighlighter-params") || "";
    var m = /brush:\s*([\w+#-]+)/.exec(parametry);
    if (m) { return m[1]; }
    var kod = pre.querySelector("code");
    var k = klasy(pre) + " " + (kod ? klasy(kod) : "");
    m = /(?:language|lang)-([\w+#-]+)/.exec(k);
    if (m) { return m[1]; }
    return pre.getAttribute("data-language") || (kod && kod.getAttribute("data-language")) || "";
  }

  // --- przepisanie na czysty DOM -----------------------------------------------------------

  var BLOKI = /^(P|DIV|H[1-6]|UL|OL|LI|TABLE|PRE|BLOCKQUOTE|HR|SECTION|ARTICLE|HEADER|FOOTER|MAIN|ASIDE|NAV|FIGURE|DL|DT|DD|ADDRESS|CENTER)$/;
  var NAGLOWKI = { H1: "h2", H2: "h3", H3: "h4", H4: "h4", H5: "h4", H6: "h4" };

  function przepisz(zrodlo, cel, raport) {
    var out = [];
    zrodlo.childNodes.forEach(function (n) { out = out.concat(wezel(n, cel, raport)); });
    return out;
  }

  function owin(tag, dzieci, cel) {
    var el = cel.createElement(tag);
    dzieci.forEach(function (d) { el.appendChild(d); });
    return el;
  }

  function wezel(n, cel, raport) {
    if (n.nodeType === 3) { return [cel.createTextNode(n.nodeValue)]; }
    if (n.nodeType !== 1) { return []; }
    var tag = n.nodeName.toUpperCase().replace(/^.*:/, "");
    if (n.matches(WYRZUC) || styl(n, "display") === "none" || n.getAttribute("aria-hidden") === "true" && !n.textContent.trim()) { return []; }

    var panel = rodzajPanelu(n);
    if (panel) { raport.panele++; return [panelZ(n, panel, cel, raport)]; }

    if (tag === "PRE") {
      var pre = cel.createElement("pre");
      var jezyk = jezykKodu(n);
      if (jezyk) { pre.setAttribute("data-jezyk", jezyk); }
      pre.textContent = tekstZLiniami(n).replace(/^\n+/, "");
      return [pre];
    }
    if (NAGLOWKI[tag]) { return [owin(NAGLOWKI[tag], przepisz(n, cel, raport), cel)]; }
    if (/^(UL|OL)$/.test(tag)) {
      var lista = owin(tag.toLowerCase(), przepisz(n, cel, raport), cel);
      var start = parseInt(n.getAttribute("start"), 10);
      if (tag === "OL" && start > 1) { lista.setAttribute("start", String(start)); }
      return [lista];
    }
    if (tag === "LI") {
      var li = owin("li", przepisz(n, cel, raport), cel);
      if (n.hasAttribute("data-inline-task-id") || /task/i.test(klasy(n.parentNode))) {
        li.insertBefore(cel.createTextNode(/checked/.test(klasy(n)) || n.getAttribute("data-task-state") === "DONE" ? "☑ " : "☐ "), li.firstChild);
      }
      return [li];
    }
    if (tag === "TABLE") { raport.tabele++; return [tabela(n, cel, raport)]; }
    if (tag === "BLOCKQUOTE") { return [owin("blockquote", przepisz(n, cel, raport), cel)]; }
    if (tag === "HR") { return [cel.createElement("hr")]; }
    if (tag === "BR") { return [cel.createElement("br")]; }
    if (tag === "IMG") { return obraz(n, cel, raport); }
    if (tag === "A") {
      var href = (n.getAttribute("href") || "").trim();
      var dzieciA = przepisz(n, cel, raport);
      if (/^(https?:|mailto:)/i.test(href)) { var a = owin("a", dzieciA, cel); a.setAttribute("href", href); return [a]; }
      return dzieciA;
    }
    if (/^(P|DIV|SECTION|ARTICLE|HEADER|FOOTER|MAIN|ASIDE|FIGURE|CENTER|ADDRESS|DD|DT)$/.test(tag)) {
      var dzieci = przepisz(n, cel, raport);
      var maBloki = dzieci.some(function (d) { return d.nodeType === 1 && BLOKI.test(d.nodeName); });
      return maBloki ? dzieci : [owin("p", dzieci, cel)];
    }

    // formatowanie w tekscie: znaczniki i style (Word i Google Docs pisza je w style)
    var wnetrze = przepisz(n, cel, raport);
    var waga = styl(n, "font-weight");
    var pogrubienie = (/^(B|STRONG)$/.test(tag) && !/^(normal|400|300)$/.test(waga)) || /^(bold|bolder|[6-9]00)$/.test(waga);
    var kursywa = /^(I|EM)$/.test(tag) && styl(n, "font-style") !== "normal" || styl(n, "font-style") === "italic";
    var skreslenie = /^(S|STRIKE|DEL)$/.test(tag) || /line-through/.test(styl(n, "text-decoration"));
    var kod = /^(CODE|KBD|SAMP|TT)$/.test(tag) || MONO.test(styl(n, "font-family")) || (tag === "FONT" && MONO.test(n.getAttribute("face") || ""));
    if (kod && !n.parentNode.closest("code, kbd, samp, tt")) {
      var c = cel.createElement("code");
      c.textContent = n.textContent.replace(/\u00a0/g, " ");
      return c.textContent.trim() ? [c] : wnetrze;
    }
    if (skreslenie) { wnetrze = [owin("s", wnetrze, cel)]; }
    if (kursywa) { wnetrze = [owin("em", wnetrze, cel)]; }
    if (pogrubienie) { wnetrze = [owin("strong", wnetrze, cel)]; }
    return wnetrze;
  }

  /* Obraz ze schowka:
   * - zapisany w HTML jako data: (Google Docs, czesc przegladarek) - edytor
   *   zrobi z niego zalacznik (atrybut data-kb-nowy),
   * - zalacznik bazy wiedzy tego portalu (kopia z innego artykulu) - zostaje,
   * - z obcego adresu - odnosnik; panel nie pokazuje obrazkow z innych
   *   serwerow (Content-Security-Policy), a tak adres nie ginie,
   * - plik lokalny Worda (file:///...) - nie do odczytania ze strony; liczymy
   *   go, zeby powiedziec autorowi, co trzeba dodac recznie.
   */
  var OBRAZ_DATA = /^data:image\/(png|jpeg|gif|webp);base64,/i;
  var ZALACZNIK = /^\/wiedza\/zalacznik\/[\w-]+\/podglad$/;

  function obraz(n, cel, raport) {
    var src = (n.getAttribute("src") || "").trim();
    var alt = (n.getAttribute("alt") || "").trim();
    var wlasny = src.indexOf(location.origin + "/") === 0 ? src.slice(location.origin.length) : src;
    var img;
    if (OBRAZ_DATA.test(src)) {
      img = cel.createElement("img");
      img.setAttribute("src", src);
      img.setAttribute("data-kb-nowy", "1");
      img.setAttribute("alt", alt);
      raport.wklejoneObrazy++;
      return [img];
    }
    if (ZALACZNIK.test(wlasny)) {
      img = cel.createElement("img");
      img.setAttribute("src", wlasny);
      img.setAttribute("alt", alt);
      return [img];
    }
    if (/^https?:\/\//i.test(src)) {
      var a = cel.createElement("a");
      a.setAttribute("href", src);
      a.textContent = alt || "obraz";
      raport.obrazyOdnosniki++;
      return [a];
    }
    raport.obrazy++;
    return [];
  }

  function panelZ(n, rodzaj, cel, raport) {
    var panel = cel.createElement("div");
    panel.className = "kb-panel kb-panel-" + rodzaj;
    var tytul = cel.createElement("div");
    tytul.className = "kb-panel-tytul";
    var zrTytul = n.querySelector(":scope > .title, :scope > p.title, :scope > .panelHeader");
    tytul.textContent = zrTytul ? zrTytul.textContent.trim() : PANELE[rodzaj];
    if (zrTytul) { zrTytul.remove(); }
    panel.appendChild(tytul);
    var tresc = n.querySelector(".confluence-information-macro-body, .panelContent") || n;
    przepisz(tresc, cel, raport).forEach(function (d) { panel.appendChild(d); });
    return panel;
  }

  function tabela(n, cel, raport) {
    var wiersze = Array.prototype.slice.call(n.querySelectorAll("tr")).filter(function (tr) { return tr.closest("table") === n; });
    var siatka = [], zajete = {};
    wiersze.forEach(function (tr, r) {
      siatka[r] = siatka[r] || [];
      var k = 0;
      Array.prototype.forEach.call(tr.children, function (td) {
        if (!/^(TD|TH)$/.test(td.nodeName)) { return; }
        while (zajete[r + ":" + k]) { k++; }
        var span = Math.min(parseInt(td.getAttribute("colspan"), 10) || 1, 50);
        var rspan = Math.min(parseInt(td.getAttribute("rowspan"), 10) || 1, 500);
        siatka[r][k] = td;
        for (var dr = 0; dr < rspan; dr++) { for (var dk = 0; dk < span; dk++) { if (dr || dk) { zajete[(r + dr) + ":" + (k + dk)] = true; } } }
        k += span;
      });
    });
    var kolumn = siatka.reduce(function (m, w) { return Math.max(m, w.length); }, 0);
    while (siatka.length && siatka[siatka.length - 1].every(function (td) { return !td || !td.textContent.trim(); })) { siatka.pop(); }
    var t = cel.createElement("table");
    siatka.forEach(function (w, r) {
      var tr = cel.createElement("tr");
      for (var k = 0; k < kolumn; k++) {
        var komorka = cel.createElement(r === 0 ? "th" : "td");
        if (w[k]) { komorkaInline(przepisz(w[k], cel, raport), komorka, cel); }
        // naglowek tabeli i tak jest pogrubiony - "**Host**" w Markdown to szum
        if (r === 0) { komorka.querySelectorAll("strong").forEach(function (s) { s.replaceWith.apply(s, Array.prototype.slice.call(s.childNodes)); }); }
        tr.appendChild(komorka);
      }
      (r === 0 ? (t.tHead || t.createTHead()) : (t.tBodies[0] || t.createTBody())).appendChild(tr);
    });
    return t;
  }

  // W komorce tabeli Markdown nie ma akapitow ani list - splaszczamy do jednej linii.
  function komorkaInline(dzieci, komorka, cel) {
    var pierwszy = true;
    (function dodaj(wezly) {
      wezly.forEach(function (d) {
        if (d.nodeType === 1 && (BLOKI.test(d.nodeName) || /^(TH|TD|TR|TBODY|THEAD)$/.test(d.nodeName))) {
          if (d.nodeName === "PRE") { var c = cel.createElement("code"); c.textContent = d.textContent.replace(/\n/g, " "); d = c; }
          else {
            if (!pierwszy) { komorka.appendChild(cel.createTextNode(d.nodeName === "LI" ? " • " : " ")); }
            else if (d.nodeName === "LI") { komorka.appendChild(cel.createTextNode("• ")); }
            dodaj(Array.prototype.slice.call(d.childNodes));
            pierwszy = false;
            return;
          }
        }
        if (d.nodeType === 1 && d.nodeName === "BR") { d = cel.createTextNode(" "); }
        komorka.appendChild(d);
        if (d.nodeType !== 3 || d.nodeValue.trim()) { pierwszy = false; }
      });
    })(dzieci);
  }

  // --- porzadki po przepisaniu ---------------------------------------------------------------

  function porzadki(kontener, cel) {
    // luzny tekst na poziomie blokow -> akapit
    var bufor = [];
    function zamknij(przed) {
      if (!bufor.length) { return; }
      if (bufor.some(function (d) { return d.nodeType !== 3 || d.nodeValue.replace(/\u00a0/g, " ").trim(); })) {
        var p = cel.createElement("p");
        kontener.insertBefore(p, przed);
        bufor.forEach(function (d) { p.appendChild(d); });
      } else { bufor.forEach(function (d) { d.remove(); }); }
      bufor = [];
    }
    Array.prototype.slice.call(kontener.childNodes).forEach(function (d) {
      if (d.nodeType === 1 && BLOKI.test(d.nodeName)) { zamknij(d); } else { bufor.push(d); }
    });
    zamknij(null);

    kontener.querySelectorAll("p, h2, h3, h4, li, th, td").forEach(function (el) {
      // biale znaki: Word wstawia nowe linie i &nbsp; w srodku akapitow
      var w = cel.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      while (w.nextNode()) {
        if (!w.currentNode.parentNode.closest("pre")) { w.currentNode.nodeValue = w.currentNode.nodeValue.replace(/[\s\u00a0]+/g, " "); }
      }
    });
    // puste akapity i naglowki (Word: <p>&nbsp;</p>) oraz puste formatowanie
    kontener.querySelectorAll("strong, em, s, code, a").forEach(function (el) {
      if (!el.textContent.trim() && !el.querySelector("img")) { el.replaceWith(cel.createTextNode(el.textContent)); }
    });
    kontener.querySelectorAll("p, h2, h3, h4").forEach(function (el) {
      if (!el.textContent.trim() && !el.querySelector("img")) { el.remove(); }
    });
    kontener.querySelectorAll("ul, ol").forEach(function (l) { if (!l.querySelector("li")) { l.remove(); } });
    // akapit w elemencie listy z jednym akapitem -> sam tekst (lista zwarta)
    kontener.querySelectorAll("li").forEach(function (li) {
      var akapity = li.querySelectorAll(":scope > p");
      if (akapity.length === 1) { while (akapity[0].firstChild) { li.insertBefore(akapity[0].firstChild, akapity[0]); } akapity[0].remove(); }
    });
    kontener.querySelectorAll(".kb-panel").forEach(function (p) { porzadkiPanelu(p, cel); });
    kontener.normalize();
  }

  function porzadkiPanelu(panel, cel) {
    var tytul = panel.querySelector(".kb-panel-tytul");
    var tymczasowy = cel.createElement("div");
    Array.prototype.slice.call(panel.childNodes).forEach(function (d) { if (d !== tytul) { tymczasowy.appendChild(d); } });
    porzadki(tymczasowy, cel);
    while (tymczasowy.firstChild) { panel.appendChild(tymczasowy.firstChild); }
  }

  // --- zwykly tekst i TSV z arkusza --------------------------------------------------------

  function tekstNaHtml(tekst, cel, raport) {
    var frag = cel.createElement("div");
    tekst = tekst.replace(/\r\n?/g, "\n").replace(/\n+$/, "");
    var linie = tekst.split("\n");
    var tsv = linie.length >= 2 && linie.every(function (l) { return l.indexOf("\t") >= 0; }) &&
      linie.every(function (l) { return l.split("\t").length === linie[0].split("\t").length; });
    if (tsv) {
      var zr = cel.createElement("table");
      linie.forEach(function (l) {
        var tr = zr.insertRow();
        l.split("\t").forEach(function (v) { tr.insertCell().textContent = v; });
      });
      raport.tabele++;
      frag.appendChild(tabela(zr, cel, raport));
      return frag;
    }
    tekst.split(/\n{2,}/).forEach(function (akapit) {
      var p = cel.createElement("p");
      akapit.split("\n").forEach(function (l, i) {
        if (i) { p.appendChild(cel.createElement("br")); }
        p.appendChild(cel.createTextNode(l));
      });
      frag.appendChild(p);
    });
    return frag;
  }

  /* Glowna funkcja: dane ze schowka -> czysty kontener <div> w dokumencie `cel`. */
  function oczysc(html, tekst, cel) {
    var raport = { zrodlo: "", listy: 0, kod: 0, tabele: 0, panele: 0, obrazy: 0, obrazyOdnosniki: 0, wklejoneObrazy: 0 };
    if (!html) { return { dom: tekstNaHtml(tekst || "", cel, raport), raport: raport }; }
    if (/urn:schemas-microsoft-com:office:word|class="?Mso/i.test(html)) { raport.zrodlo = "Word"; }
    else if (/urn:schemas-microsoft-com:office:excel|ProgId.*Excel/i.test(html)) { raport.zrodlo = "Excel"; }
    else if (/confluence|data-panel-type|syntaxhighlighter/i.test(html)) { raport.zrodlo = "Confluence"; }
    // Excel i Word opakowuja wklejany fragment komentarzami StartFragment/EndFragment
    var doc = new DOMParser().parseFromString(html, "text/html");
    // Stary Word: znacznik listy miedzy <!--[if !supportLists]--> a <!--[endif]-->
    var w = doc.createTreeWalker(doc.body, NodeFilter.SHOW_COMMENT), komentarze = [];
    while (w.nextNode()) { komentarze.push(w.currentNode); }
    komentarze.forEach(function (k) {
      if (/\[if !supportLists\]/.test(k.nodeValue)) {
        var n = k.nextSibling;
        while (n && !(n.nodeType === 8 && /\[endif\]/.test(n.nodeValue))) {
          var dalej = n.nextSibling;
          if (n.nodeType === 1) { n.setAttribute("style", "mso-list:Ignore"); }
          n = dalej;
        }
      }
    });
    komentarze.forEach(function (k) { if (k.parentNode) { k.remove(); } });
    listyWorda(doc, raport);
    kodZCzcionki(doc, raport);
    doc.querySelectorAll("p.MsoTitle").forEach(function (p) { var h = doc.createElement("h1"); while (p.firstChild) { h.appendChild(p.firstChild); } p.replaceWith(h); });
    var kontener = cel.createElement("div");
    przepisz(doc.body, cel, raport).forEach(function (d) { kontener.appendChild(d); });
    porzadki(kontener, cel);
    return { dom: kontener, raport: raport };
  }

  /* ==========================================================================
     Tresc edytora -> Markdown (w formie, ktora serwer renderuje: commonmark +
     tabele + przekreslenie, panele :::rodzaj, pola {hostname}).
     ======================================================================== */

  function escapeTekst(t) {
    return t
      .replace(/\\/g, "\\\\")
      .replace(/([*`\[\]<~|])/g, "\\$1")
      .replace(/(^|[^\p{L}\p{N}])_|_(?=[^\p{L}\p{N}]|$)/gu, function (m) { return m.replace("_", "\\_"); })
      .replace(/&(?=#?\w+;)/g, "\\&");
  }

  // Poczatek linii, ktory Markdown wzialby za blok: naglowek, lista, cytat, panel...
  function escapePoczatkuLinii(linia) {
    return linia
      .replace(/^(\s*)(#{1,6}(?=\s|$)|>|[-+](?=\s|$)|=+\s*$|:::)/, "$1\\$2")
      .replace(/^(\s*\d{1,9})([.)])(?=\s|$)/, "$1\\$2");
  }

  function inline(el, wKomorce) {
    var wynik = "";
    el.childNodes.forEach(function (n) {
      // \u200b wstawia edytor za kodem i odnosnikiem, zeby dalej pisac zwyklym tekstem
      if (n.nodeType === 3) { wynik += escapeTekst(n.nodeValue.replace(/\u200b/g, "").replace(/[\s\u00a0]+/g, " ")); return; }
      if (n.nodeType !== 1) { return; }
      var tag = n.nodeName;
      if (tag === "BR") { wynik += wKomorce ? " " : "\\\n"; return; }
      if (tag === "CODE" || tag === "KBD") { wynik += kodInline(n.textContent.replace(/\u00a0/g, " ")); return; }
      if (tag === "IMG") {
        // Obraz jeszcze nie zapisany (wklejony ze schowka): serwer podmieni
        // znacznik na adres zalacznika. Numer to pozycja pliku w formularzu.
        var numer = n.getAttribute("data-kb-obraz");
        var cel = numer !== null ? "kb-obraz:" + numer : adres(n.getAttribute("src") || "");
        if (cel) { wynik += "![" + escapeTekst(n.getAttribute("alt") || "") + "](" + cel + ")"; }
        return;
      }
      var srodek = inline(n, wKomorce);
      if (tag === "A" && n.getAttribute("href")) { wynik += "[" + srodek + "](" + adres(n.getAttribute("href")) + ")"; return; }
      var znak = { STRONG: "**", B: "**", EM: "*", I: "*", S: "~~", STRIKE: "~~", DEL: "~~" }[tag];
      if (znak) { wynik += otocz(srodek, znak); return; }
      if (BLOKI.test(tag)) { wynik += " " + srodek + " "; return; }
      wynik += srodek;
    });
    return wynik;
  }

  // Spacje na brzegach wychodza poza znaczniki - "**tekst **" nie bylby pogrubieniem.
  function otocz(srodek, znak) {
    var m = /^(\s*)([\s\S]*?)(\s*)$/.exec(srodek);
    return m[2] ? m[1] + znak + m[2] + znak + m[3] : srodek;
  }

  function kodInline(t) {
    if (!t.trim()) { return t; }
    var najdluzszy = (t.match(/`+/g) || []).reduce(function (m, s) { return Math.max(m, s.length); }, 0);
    var ploty = new Array(najdluzszy + 2).join("`");
    var odstep = /^`|`$|^ .* $/.test(t) ? " " : "";
    return ploty + odstep + t + odstep + ploty;
  }

  function adres(url) {
    return url.trim().replace(/\s/g, "%20").replace(/[()]/g, function (z) { return "\\" + z; });
  }

  function akapit(tekst) {
    // <br> na koncu akapitu (tak edytor trzyma pusty akapit) to nie
    // twarde zlamanie linii - inaczej w Markdown zostalby samotny "\".
    tekst = tekst.replace(/(?:\\\n\s*)+$/, "");
    return tekst.split("\n").map(function (l) { return escapePoczatkuLinii(l.replace(/^[ \t]+/, "")); }).join("\n").trim();
  }

  function bloki(kontener, wPanelu) {
    var wynik = [], bufor = null;
    function zamknij() { if (bufor) { var t = akapit(inline(bufor)); if (t) { wynik.push(t); } bufor = null; } }
    kontener.childNodes.forEach(function (n) {
      if (n.nodeType === 1 && (BLOKI.test(n.nodeName) || n.classList.contains("kb-panel"))) {
        zamknij();
        var b = blok(n, wPanelu);
        if (b) { wynik.push(b); }
      } else {
        bufor = bufor || document.createElement("span");
        bufor.appendChild(n.cloneNode(true));
      }
    });
    zamknij();
    return wynik;
  }

  function blok(n, wPanelu) {
    var tag = n.nodeName;
    if (n.classList.contains("kb-panel") && !wPanelu) {
      var rodzaj = (/kb-panel-(\w+)/.exec(n.className) || [])[1];
      if (PANELE[rodzaj]) {
        var tytulEl = n.querySelector(".kb-panel-tytul");
        var tytul = tytulEl ? tytulEl.textContent.replace(/\s+/g, " ").trim() : "";
        var kopia = n.cloneNode(true);
        var tk = kopia.querySelector(".kb-panel-tytul");
        if (tk) { tk.remove(); }
        return ":::" + rodzaj + (tytul && tytul !== PANELE[rodzaj] ? " " + tytul : "") + "\n" + bloki(kopia, true).join("\n\n") + "\n:::";
      }
    }
    if (n.classList.contains("kb-panel-tytul")) { return akapit("**" + inline(n) + "**"); }
    var h = /^H([1-6])$/.exec(tag);
    if (h) { var t = inline(n).replace(/\\\n/g, " ").trim(); return t ? new Array(+h[1] + 1).join("#") + " " + t : ""; }
    if (tag === "P") { return akapit(inline(n)); }
    if (tag === "UL" || tag === "OL") { return lista(n); }
    if (tag === "PRE") { return blokKodu(n); }
    if (tag === "BLOCKQUOTE") { return bloki(n, wPanelu).join("\n\n").split("\n").map(function (l) { return l ? "> " + l : ">"; }).join("\n"); }
    if (tag === "HR") { return "---"; }
    if (tag === "TABLE") { return tabelaMd(n); }
    return bloki(n, wPanelu).join("\n\n");
  }

  function blokKodu(pre) {
    var tekst = tekstZLiniami(pre).replace(/^\n+/, "");
    var najdluzszy = (tekst.match(/`{3,}/g) || []).reduce(function (m, s) { return Math.max(m, s.length); }, 2);
    var plot = new Array(najdluzszy + 2).join("`");
    var kod = pre.querySelector("code");
    var jezyk = pre.getAttribute("data-jezyk") || ((/language-([\w+#-]+)/.exec(kod ? kod.className : "") || [])[1]) || "";
    return plot + jezyk + "\n" + tekst + "\n" + plot;
  }

  function lista(l) {
    var numer = parseInt(l.getAttribute("start"), 10) || 1;
    var luzna = false;
    var elementy = [];
    Array.prototype.forEach.call(l.children, function (li) {
      if (li.nodeName !== "LI") { return; }
      var znacznik = l.nodeName === "OL" ? (numer++) + ". " : "- ";
      var wcięcie = new Array(znacznik.length + 1).join(" ");
      var czesci = bloki(li);
      if (czesci.filter(function (c) { return !/^(\s*)([-*+]|\d+\.) /.test(c); }).length > 1) { luzna = true; }
      var tekst = czesci.join("\n") || "";
      elementy.push(znacznik + tekst.split("\n").map(function (x, i) { return i && x ? wcięcie + x : x; }).join("\n"));
    });
    return elementy.join(luzna ? "\n\n" : "\n");
  }

  function tabelaMd(t) {
    var wiersze = Array.prototype.slice.call(t.querySelectorAll("tr")).filter(function (tr) { return tr.closest("table") === t; });
    if (!wiersze.length) { return ""; }
    var dane = wiersze.map(function (tr) {
      return Array.prototype.slice.call(tr.children).map(function (td) {
        return inline(td, true).replace(/\n/g, " ").replace(/\s+/g, " ").trim().replace(/^([-+#>])/, "\\$1");
      });
    });
    var kolumn = dane.reduce(function (m, w) { return Math.max(m, w.length); }, 0);
    var linia = function (w) { var k = w.slice(); while (k.length < kolumn) { k.push(""); } return "| " + k.join(" | ") + " |"; };
    var wynik = [linia(dane[0]), "|" + new Array(kolumn + 1).join(" --- |")];
    dane.slice(1).forEach(function (w) { wynik.push(linia(w)); });
    return wynik.join("\n");
  }

  function doMarkdown(edytor) {
    return bloki(edytor).join("\n\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
  }

  return { oczysc: oczysc, doMarkdown: doMarkdown, PANELE: PANELE };
})();
