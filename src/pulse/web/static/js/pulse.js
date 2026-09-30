// VBR Pulse — the small amount of client behaviour htmx doesn't cover (PLAN §4.2, §7.7, §7.9).
(function () {
  "use strict";
  var root = document.documentElement;
  var reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ------------------------------------------------------------ preferences
  function loadPrefs() {
    try { return JSON.parse(window.localStorage.getItem("pulse.prefs") || "{}"); } catch (e) { return {}; }
  }
  function savePref(name, value) {
    var prefs = loadPrefs();
    prefs[name] = value;
    try { window.localStorage.setItem("pulse.prefs", JSON.stringify(prefs)); } catch (e) { /* ignore */ }
  }
  function setPresenter(on) { root.classList.toggle("presenter", on); savePref("presenter", on); syncToggles(); }
  function setInspector(open) {
    root.classList.toggle("inspector-collapsed", !open);
    savePref("inspector", open);
    var btn = document.querySelector('[data-action="inspector-toggle"]');
    if (btn) {
      btn.setAttribute("aria-expanded", String(open));
      btn.setAttribute("aria-label", open ? "Collapse the inspector (I)" : "Open the inspector (I)");
    }
  }
  function syncToggles() {
    document.querySelectorAll("[data-pref]").forEach(function (box) {
      var name = box.getAttribute("data-pref");
      box.checked = name === "presenter" ? root.classList.contains("presenter")
        : name === "headers" ? !root.classList.contains("headers-off") : !!loadPrefs()[name];
    });
  }

  document.addEventListener("change", function (e) {
    var box = e.target.closest && e.target.closest("[data-pref]");
    if (!box) return;
    var name = box.getAttribute("data-pref");
    if (name === "presenter") setPresenter(box.checked);
    if (name === "headers") { root.classList.toggle("headers-off", !box.checked); savePref("headers", box.checked); }
  });

  // ------------------------------------------------------------ overlays and panels
  var lastFocus = null;
  function openOverlay(id) {
    var el = document.getElementById(id);
    if (!el) return;
    lastFocus = document.activeElement;
    el.hidden = false;
    var first = el.querySelector("input:checked, button, input, select");
    if (first) first.focus();
  }
  function closeOverlays() {
    var closed = false;
    document.querySelectorAll(".overlay:not([hidden])").forEach(function (el) { el.hidden = true; closed = true; });
    if (closed && lastFocus) lastFocus.focus();
    return closed;
  }
  function closePanel() {
    var panel = document.getElementById("job-panel");
    if (panel && panel.innerHTML.trim()) { panel.innerHTML = ""; return true; }
    return false;
  }

  document.addEventListener("click", function (e) {
    var t = e.target;
    var opener = t.closest("[data-open]");
    if (opener) { openOverlay(opener.getAttribute("data-open")); return; }
    if (t.closest("[data-close]")) { closeOverlays(); return; }
    if (t.classList.contains("overlay")) { closeOverlays(); return; }
    if (t.closest('[data-action="close-panel"]')) { closePanel(); return; }
    if (t.closest('[data-action="inspector-toggle"]')) { setInspector(root.classList.contains("inspector-collapsed")); return; }
    if (t.closest('[data-action="inspector-wide"]')) {
      root.classList.toggle("inspector-wide");
      savePref("wide", root.classList.contains("inspector-wide"));
      return;
    }
    if (t.closest(".toast-close")) { t.closest(".toast").remove(); return; }
    if (t.closest("[data-clear-filters]")) {
      var filter = document.getElementById("job-filter");
      var type = document.getElementById("job-type");
      if (filter) filter.value = "";
      if (type) type.value = "";
      if (window.htmx) window.htmx.trigger("#job-filters", "submit");
      return;
    }
    var copy = t.closest("[data-copy]");
    if (copy) {
      var source = document.getElementById(copy.getAttribute("data-copy"));
      if (source && navigator.clipboard) {
        navigator.clipboard.writeText(source.textContent).then(function () {
          var label = copy.textContent;
          copy.textContent = "Copied";
          setTimeout(function () { copy.textContent = label; }, 1500);
        });
      }
      return;
    }
    // A click anywhere on a job row opens its detail panel (the name is the real button).
    var row = t.closest(".job-row");
    if (row && !t.closest("button, a, input, select")) {
      var open = row.querySelector(".job-open");
      if (open) open.click();
    }
  });

  document.addEventListener("submit", function (e) {
    if (e.target.hasAttribute("data-close-on-submit")) closeOverlays();
  });

  // ------------------------------------------------------------ keyboard shortcuts (PLAN §7.9)
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (closeOverlays() || closePanel()) e.preventDefault();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    var el = e.target;
    if (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
    if (!document.body.classList.contains("app")) return;
    var key = e.key;
    if (key === "p" || key === "P") { setPresenter(!root.classList.contains("presenter")); }
    else if (key === "i" || key === "I") { setInspector(root.classList.contains("inspector-collapsed")); }
    else if (key === "?") { openOverlay("shortcuts"); }
    else if (key === "s" || key === "S") { openOverlay("scenario-picker"); }
    else if (key === "/") {
      var filter = document.getElementById("job-filter");
      if (!filter) return;
      filter.focus();
      filter.select();
    }
    else if (/^[1-4]$/.test(key)) {
      var link = document.querySelector('.rail a[data-key="' + key + '"]');
      if (link) link.click();
    } else { return; }
    e.preventDefault();
  });

  // ------------------------------------------------------------ token ring (PLAN §7.7)
  var refreshRequested = false;
  function fmt(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    return Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
  }
  function tickRing() {
    var ring = document.querySelector(".token-ring[data-expires-at]");
    if (!ring) return;
    var now = Date.now();
    var lifetime = Number(ring.dataset.lifetime);
    var left = (Number(ring.dataset.expiresAt) - now) / 1000;
    var refreshLeft = (Number(ring.dataset.expiresAt) - Number(ring.dataset.refreshAt)) / 1000;
    var fill = ring.querySelector(".ring-fill");
    if (fill) fill.setAttribute("stroke-dashoffset", String(100 - Math.max(0, 100 * left / lifetime)));
    var text = ring.querySelector("[data-ring-text]");
    if (text) text.textContent = fmt(left);
    ring.classList.toggle("is-low", left < 120);
    ring.title = "Access token · " + fmt(left) + " left · refreshes automatically at " + fmt(refreshLeft);
    if (now >= Number(ring.dataset.refreshAt) && !refreshRequested && window.htmx) {
      refreshRequested = true;  // ask the server to refresh once; the new ring arrives by SSE
      window.htmx.ajax("GET", "/ui/token", { target: "#token-ring", swap: "innerHTML" });
    }
  }
  setInterval(tickRing, 1000);

  // ------------------------------------------------------------ htmx integration
  function ringSwapped() {
    refreshRequested = false;
    var ring = document.querySelector("#token-ring .token-ring");
    if (ring && !reducedMotion) {
      ring.classList.add("is-pulse");
      setTimeout(function () { ring.classList.remove("is-pulse"); }, 600);
    }
    tickRing();
  }
  // A token refresh pushes a new ring over the inspector stream (event "token").
  document.addEventListener("htmx:sseMessage", function (e) {
    if (e.target && e.target.id === "token-ring") ringSwapped();
  });

  // Each poll replaces a grouped inspector row ("GET /sessions/… ×14") with a fresh copy at
  // the top. Keep it expanded, and keep keyboard focus on it, so it doesn't snap shut mid-read.
  var openRows = null;
  document.addEventListener("htmx:sseBeforeMessage", function (e) {
    if (!e.target || e.target.id !== "inspector-rows") return;
    openRows = [];
    e.target.querySelectorAll("li[id^='insp-grp-'] > details[open]").forEach(function (d) {
      var row = d.parentElement;
      openRows.push({ id: row.id, focused: row.contains(document.activeElement) });
    });
  });
  document.addEventListener("htmx:sseMessage", function (e) {
    if (!e.target || e.target.id !== "inspector-rows" || !openRows) return;
    openRows.forEach(function (was) {
      var row = document.getElementById(was.id);
      var details = row && row.querySelector("details");
      if (!details) return;
      details.open = true;
      if (was.focused) details.querySelector("summary").focus({ preventScroll: true });
      // htmx wires the row's lazy-load trigger only after settling, so the "toggle" above can
      // go unheard: load the latest call's detail ourselves once the swap has settled.
      setTimeout(function () {
        var body = details.querySelector(".insp-body[hx-get]");
        if (body && document.body.contains(body) && window.htmx) {
          window.htmx.ajax("GET", body.getAttribute("hx-get"), { target: body, swap: "outerHTML" });
        }
      }, 60);
    });
    openRows = null;
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var target = e.detail.target;
    if (target && target.id === "token-ring") ringSwapped();
    // Starting a job moves focus to the session track (PLAN §7.5.2).
    var focusWrap = target && target.querySelector && target.querySelector("[data-focus]");
    if (focusWrap) {
      focusWrap.removeAttribute("data-focus");
      var track = focusWrap.querySelector(".track");
      if (track) { track.focus({ preventScroll: true }); track.scrollIntoView({ block: "nearest" }); }
    }
    if (target && target.id === "job-panel") {
      var heading = target.querySelector("h2");
      if (heading) { heading.setAttribute("tabindex", "-1"); heading.focus(); }
    }
  });
  // New toasts dismiss themselves after 8 s.
  document.addEventListener("htmx:oobAfterSwap", function (e) {
    if (!e.detail.target || e.detail.target.id !== "toasts") return;
    var toast = e.detail.target.lastElementChild;
    if (toast) setTimeout(function () { toast.remove(); }, 8000);
  });

  document.addEventListener("DOMContentLoaded", function () { syncToggles(); tickRing(); });
})();
