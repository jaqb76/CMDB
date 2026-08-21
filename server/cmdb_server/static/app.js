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

})();
