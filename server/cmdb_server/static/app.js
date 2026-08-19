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

  // Przelacznik firmy dla superadmina.
  document.querySelectorAll("select[data-autosubmit]").forEach(function (select) {
    select.addEventListener("change", function () { select.form.submit(); });
  });
})();
