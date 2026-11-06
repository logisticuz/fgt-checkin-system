/**
 * Kiosk hygiene for the check-in page (T-025). Same pattern as the membership v2 step:
 *   - Personal data that older flows left in localStorage is removed on load, so the next
 *     person on a shared kiosk never inherits it.
 *   - A page restored from the back/forward cache (someone pressing Back) is reset, because
 *     the browser would otherwise show the previous person's filled-in form.
 *   - In kiosk mode a half-filled form is cleared after 90 s without input, even while a
 *     submit is in flight (the page drops that late answer via its generation counter).
 * Nothing personal is written to any storage by the check-in page.
 */
(function () {
  "use strict";

  // Keys written by earlier versions of checkin.html / register.html.
  const STALE_KEYS = ["namn", "telefon", "tag", "eventSlug", "personnummer", "selectedGames"];
  STALE_KEYS.forEach(function (key) {
    try { localStorage.removeItem(key); } catch (e) { /* storage disabled */ }
  });

  function install(options) {
    const reset = options.reset;
    const idleMs = options.idleMs || 90000;

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

  window.CheckinHygiene = { install: install, staleKeys: STALE_KEYS.slice() };
})();
