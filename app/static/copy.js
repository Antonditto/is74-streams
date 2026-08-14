/* Copy-to-clipboard with non-secure-context fallback + 3-state theme toggle. No deps. */
(function () {
  "use strict";

  function flash(btn, text) {
    if (btn.dataset.busy) return;
    btn.dataset.busy = "1";
    var toast = btn.querySelector(".copy-toast");
    if (toast) toast.textContent = text;
    btn.classList.add("copied");
    setTimeout(function () {
      btn.classList.remove("copied");
      delete btn.dataset.busy;
    }, 1400);
  }

  function fallback(btn, url) {
    // Clipboard API needs a secure context (https / localhost). Over plain
    // http://<lan-ip>:8090 we reveal the URL in a selectable field instead.
    var card = btn.closest("[data-stream]") || btn.parentElement;
    var box = card.querySelector(".copy-fallback");
    if (!box) { window.prompt("Скопируйте ссылку:", url); return; }
    var input = box.querySelector("input");
    input.value = url;
    box.classList.add("show");
    input.focus();
    input.select();
    try {
      if (document.execCommand("copy")) { flash(btn, "Скопировано"); return; }
    } catch (e) {}
    flash(btn, "Выделено");
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    e.preventDefault();
    var url = new URL(btn.getAttribute("data-copy"), location.href).href;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(url).then(
        function () { flash(btn, "Скопировано"); },
        function () { fallback(btn, url); }
      );
    } else {
      fallback(btn, url);
    }
  });

  // Тема: auto (по умолчанию, следует prefers-color-scheme) -> light -> dark -> auto -> ...
  var THEME_ORDER = ["auto", "light", "dark"];
  var THEME_ICON = { auto: "ic-theme", light: "ic-sun", dark: "ic-moon" };
  var THEME_LABEL = { auto: "Тема: автоматически", light: "Тема: светлая", dark: "Тема: тёмная" };
  var root = document.documentElement;

  function currentTheme() {
    try { return localStorage.getItem("is74-theme") || "auto"; } catch (e) { return "auto"; }
  }

  function applyTheme(theme) {
    if (theme === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);

    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    var use = btn.querySelector("use");
    if (use) use.setAttribute("href", (window.BASE_PATH || "") + "/static/icons.svg#" + THEME_ICON[theme]);
    btn.title = THEME_LABEL[theme];
    btn.setAttribute("aria-label", THEME_LABEL[theme] + " — нажмите для смены");
  }

  applyTheme(currentTheme());

  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-theme-toggle]");
    if (!t) return;
    var next = THEME_ORDER[(THEME_ORDER.indexOf(currentTheme()) + 1) % THEME_ORDER.length];
    try {
      if (next === "auto") localStorage.removeItem("is74-theme");
      else localStorage.setItem("is74-theme", next);
    } catch (err) {}
    applyTheme(next);
  });
})();
