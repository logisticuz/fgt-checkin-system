/**
 * Kiosk hygiene for the check-in page (T-025). Same pattern as the membership v2 step:
 *   - Personal data left in localStorage (by older flows, or by register.html after this page
 *     loaded) is purged on load, on a back/forward-cache restore, on every idle reset and
 *     before each redirect, so the next person on a shared kiosk never inherits it.
 *   - A page restored from the back/forward cache (someone pressing Back) is reset, because
 *     the browser would otherwise show the previous person's filled-in form.
 *   - In kiosk mode a half-filled form is cleared after 90 s without input, even while a
 *     submit is in flight (the page drops that late answer via its generation counter).
 * Nothing personal is written to any storage by the check-in page.
 */
(function () {
  "use strict";

  // Keys written by earlier versions of checkin.html and (still) by register.html.
  const STALE_KEYS = ["namn", "telefon", "tag", "eventSlug", "personnummer", "selectedGames"];

  function purgePersonalStorage() {
    STALE_KEYS.forEach(function (key) {
      try { localStorage.removeItem(key); } catch (e) { /* storage disabled */ }
    });
  }

  function install(options) {
    // The check-in page purges on load. (Player pages also load this file for the kiosk
    // helpers below, without changing their storage behaviour.)
    purgePersonalStorage();
    const idleMs = options.idleMs || 90000;
    // Every reset boundary (Back restore, idle, next person) also purges storage: other pages
    // in the flow can write these keys after this page has loaded.
    function reset() {
      purgePersonalStorage();
      options.reset();
    }

    window.addEventListener("pageshow", function (event) {
      if (event.persisted) reset();
    });

    if (!options.kiosk) return;
    let timer = null;
    function bump() {
      clearTimeout(timer);
      timer = setTimeout(reset, idleMs);
    }
    ["pointerdown", "keydown", "input", "touchstart", "wheel"].forEach(function (type) {
      document.addEventListener(type, bump, { passive: true, capture: true });
    });
    bump();
  }

  // --- Kiosk history (T-062) ------------------------------------------------------------
  // On the shared kiosk, a player page URL carries that player's capability token. Kiosk
  // navigations therefore REPLACE the current history entry, so the browser's Back button
  // can't reopen the previous player's page. As a second line of defence, each kiosk player
  // page stamps its history entry; if it is shown again after a newer kiosk start, it leaves.
  // A timestamp only, nothing personal; per tab (sessionStorage), like the kioskMode flag.
  const KIOSK_START_KEY = "kioskStartedAt";
  const KIOSK_HOME = "/kiosk";

  function now() { return Date.now(); }

  function kioskStartedAt() {
    try { return Number(sessionStorage.getItem(KIOSK_START_KEY) || 0); } catch (e) { return 0; }
  }

  function markKioskStart() {
    try { sessionStorage.setItem(KIOSK_START_KEY, String(now())); } catch (e) { /* disabled */ }
  }

  // Kiosk: purge + replace (no history entry). Normal mode: exactly the old behaviour.
  function navigate(url, kiosk) {
    if (!kiosk) {
      window.location.href = url;
      return;
    }
    purgePersonalStorage();
    window.location.replace(url);
  }

  // A page is a kiosk page if the server says so, the URL says so (?kiosk=true), or the
  // kiosk start page flagged this tab (sessionStorage, cleared again in normal mode).
  function isKioskPage(serverFlag) {
    if (serverFlag === true) return true;
    try {
      if (new URLSearchParams(window.location.search).get("kiosk") === "true") return true;
      return sessionStorage.getItem("kioskMode") === "true";
    } catch (e) { return false; }
  }

  function isPlainSameOriginLink(link, event) {
    if (!link || event.defaultPrevented || event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.hasAttribute("download")) return false;
    const raw = link.getAttribute("href") || "";
    if (!raw || raw.charAt(0) === "#" || /^javascript:/i.test(raw)) return false;
    return link.origin === window.location.origin;
  }

  // In kiosk mode, same-origin links also replace the entry instead of pushing a new one.
  function replaceKioskLinks() {
    document.addEventListener("click", function (event) {
      const link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
      if (!isPlainSameOriginLink(link, event)) return;
      event.preventDefault();
      navigate(link.href, true);
    });
  }

  function kioskPlayerPage(options) {
    if (!options || !options.kiosk) return false;
    const state = history.state && typeof history.state === "object" ? history.state : {};
    if (state.kioskPageAt && kioskStartedAt() > state.kioskPageAt) {
      // An old player page opened from history after a newer kiosk start: show nothing, go home.
      document.documentElement.style.visibility = "hidden";
      navigate(KIOSK_HOME, true);
      return true;
    }
    const stamp = state.kioskPageAt || now();
    if (!state.kioskPageAt) {
      try {
        history.replaceState(Object.assign({}, state, { kioskPageAt: stamp }), "");
      } catch (e) { /* history API unavailable */ }
    }
    window.addEventListener("pageshow", function (event) {
      if (event.persisted && kioskStartedAt() > stamp) {
        document.documentElement.style.visibility = "hidden";
        navigate(KIOSK_HOME, true);
      }
    });
    replaceKioskLinks();
    return false;
  }

  // --- Kiosk idle return (T-062) --------------------------------------------------------
  // After `idleMs` without interaction, go back to the kiosk start. The last `warnMs` show a
  // countdown with a "Still here" button; any interaction restarts the timer.
  function kioskIdleReturn(options) {
    const idleMs = options.idleMs || 90000;
    const warnMs = Math.min(options.warnMs || 10000, idleMs);
    const shouldRun = options.shouldRun || function () { return true; };
    let warnTimer = null;
    let tick = null;
    let waitTimer = null;
    let deadline = 0;

    const bar = document.createElement("div");
    bar.id = "kiosk-idle-warning";
    bar.setAttribute("role", "alertdialog");
    bar.setAttribute("aria-live", "assertive");
    bar.setAttribute("aria-label", "Returning to check-in");
    bar.hidden = true;
    bar.style.cssText = [
      "position:fixed", "left:50%", "bottom:1.5rem", "transform:translateX(-50%)",
      "z-index:1000", "width:min(92vw,420px)", "box-sizing:border-box",
      "background:#1a1a2e", "border:1px solid rgba(88,170,255,0.5)", "border-radius:12px",
      "box-shadow:0 8px 24px rgba(0,0,0,0.5)", "padding:1rem 1.25rem", "color:#e0e0e0",
      "font-family:'Segoe UI',system-ui,sans-serif", "text-align:center",
    ].join(";");
    const text = document.createElement("p");
    text.id = "kiosk-idle-text";
    text.style.cssText = "margin:0 0 0.75rem;font-size:1rem;";
    const stay = document.createElement("button");
    stay.type = "button";
    stay.id = "kiosk-idle-stay";
    stay.textContent = "Still here";
    stay.style.cssText = [
      "background:#58aaff", "color:#000", "border:none", "border-radius:8px",
      "padding:0.7rem 1.5rem", "font-size:1rem", "font-weight:600", "cursor:pointer",
    ].join(";");
    bar.append(text, stay);
    document.body.appendChild(bar);

    function render() {
      const left = Math.max(0, Math.ceil((deadline - now()) / 1000));
      text.textContent = "No activity – returning to check-in in " + left + " s";
      if (left <= 0) leave();
    }

    function leave() {
      clearInterval(tick);
      clearTimeout(warnTimer);
      clearTimeout(waitTimer);
      if (options.onLeave) {
        try { options.onLeave(); } catch (e) { /* never block the return */ }
      }
      navigate(KIOSK_HOME, true);
    }

    function restart() {
      clearTimeout(warnTimer);
      clearTimeout(waitTimer);
      clearInterval(tick);
      bar.hidden = true;
      if (!shouldRun()) {
        // Another component owns idle handling right now (the v2 step). Check back every
        // second, so the full idle period starts as soon as control is handed back, even if
        // nobody touches the screen.
        waitTimer = setTimeout(restart, 1000);
        return;
      }
      deadline = now() + idleMs;
      warnTimer = setTimeout(function () {
        if (!shouldRun()) { restart(); return; }
        bar.hidden = false;
        render();
        tick = setInterval(render, 250);
      }, idleMs - warnMs);
    }

    stay.addEventListener("click", restart);
    ["pointerdown", "keydown", "input", "touchstart", "wheel"].forEach(function (type) {
      document.addEventListener(type, restart, { passive: true, capture: true });
    });
    restart();
    return { restart: restart };
  }

  window.CheckinHygiene = {
    install: install,
    purgePersonalStorage: purgePersonalStorage,
    staleKeys: STALE_KEYS.slice(),
    navigate: navigate,
    isKioskPage: isKioskPage,
    markKioskStart: markKioskStart,
    kioskPlayerPage: kioskPlayerPage,
    replaceKioskLinks: replaceKioskLinks,
    kioskIdleReturn: kioskIdleReturn,
  };
})();
