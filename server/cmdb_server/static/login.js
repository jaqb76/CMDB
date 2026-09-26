// Podpowiedz pod polem loginu: czy haslo sprawdzi AD firmy, czy CMDB.
// Uzytkownik niczego nie wybiera - sposob wynika z domeny loginu. Serwer nie
// zdradza, czy konto istnieje: nieznana domena to zawsze "konto lokalne".
(function () {
  "use strict";
  var pole = document.querySelector("[data-login-pole]");
  var sposob = document.querySelector("[data-login-sposob]");
  var podpowiedz = document.querySelector("[data-login-podpowiedz]");
  if (!pole || !sposob) return;

  var zegar = null;
  var ostatnia = null;

  function domena(login) {
    login = login.trim().toLowerCase();
    if (login.indexOf("\\") > 0) return login.split("\\")[0] + "\\";
    var at = login.lastIndexOf("@");
    return at > 0 ? login.slice(at + 1) : "";
  }

  function pokaz(dane) {
    sposob.textContent = "";
    sposob.className = "login-sposob";
    var znak = document.createElement("span");
    znak.className = "login-sposob-znak";
    var opis = document.createElement("span");
    if (dane.sposob === "ad") {
      sposob.classList.add("ad");
      znak.textContent = "AD";
      opis.appendChild(document.createTextNode("Konto domenowe "));
      var b = document.createElement("b");
      b.textContent = dane.firma || "";
      opis.appendChild(b);
    } else {
      znak.textContent = "C";
      opis.textContent = "Konto lokalne CMDB";
    }
    sposob.appendChild(znak);
    sposob.appendChild(opis);
    sposob.hidden = false;
    if (podpowiedz) podpowiedz.hidden = dane.sposob !== "ad";
  }

  function sprawdz() {
    var d = domena(pole.value);
    if (!d) { sposob.hidden = true; if (podpowiedz) podpowiedz.hidden = true; ostatnia = null; return; }
    if (d === ostatnia) return;
    ostatnia = d;
    fetch("/login/sposob?login=" + encodeURIComponent(pole.value.trim()), { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (dane) { if (dane && domena(pole.value) === d) pokaz(dane); })
      .catch(function () { /* podpowiedz to dodatek - bez niej logowanie dziala */ });
  }

  pole.addEventListener("input", function () {
    clearTimeout(zegar);
    zegar = setTimeout(sprawdz, 250);
  });
  sprawdz();
})();
