// Applies the saved appearance (theme, accent colour) before the page draws; the default amber accent comes from app.css, so there's no flash.
// The real settings live on the server; this is a copy kept by app.js.
(function () {
  try {
    var root = document.documentElement;
    var a = JSON.parse(localStorage.getItem("ta-appearance") || "null") || {};
    // One-time move from the old blue default to amber; picking Blue afterwards sticks.
    if (a.accent === "#0a84ff" && !localStorage.getItem("ta-theme-v2")) {
      a.accent = "#d9912b";
      localStorage.setItem("ta-appearance", JSON.stringify(a));
    }
    var t = a.theme || localStorage.getItem("ta-theme");
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    if (/^#[0-9a-fA-F]{6}$/.test(a.accent || "") && !/^#d9912b$/i.test(a.accent)) {
      root.style.setProperty("--accent", a.accent);
      if (a.ink) root.style.setProperty("--accent-ink", a.ink);
    }
  } catch (e) { /* storage unavailable: defaults apply */ }
})();
