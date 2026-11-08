// Applies the saved appearance (theme, accent colour, glass) before the page draws, so there's no flash.
// The real settings live on the server; this is a copy kept by app.js.
(function () {
  try {
    var root = document.documentElement;
    var a = JSON.parse(localStorage.getItem("ta-appearance") || "null") || {};
    var t = a.theme || localStorage.getItem("ta-theme");
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    if (/^#[0-9a-fA-F]{6}$/.test(a.accent || "")) {
      root.style.setProperty("--accent", a.accent);
      if (a.ink) root.style.setProperty("--accent-ink", a.ink);
    }
    if (a.glass === false) root.setAttribute("data-glass", "off");
  } catch (e) { /* storage unavailable: defaults apply */ }
})();
