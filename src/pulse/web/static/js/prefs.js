// Applies saved display preferences before first paint, so presenter mode never flickers.
(function () {
  try {
    var prefs = JSON.parse(window.localStorage.getItem("pulse.prefs") || "{}");
    var root = document.documentElement.classList;
    if (prefs.presenter) root.add("presenter");
    if (prefs.inspector === false) root.add("inspector-collapsed");
    if (prefs.wide) root.add("inspector-wide");
    if (prefs.headers === false) root.add("headers-off");
  } catch (e) { /* storage unavailable: defaults apply */ }
})();
