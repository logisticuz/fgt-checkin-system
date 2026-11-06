/**
 * Membership v2 kiosk step: details + consent -> one-time code -> Sverok registration.
 *
 * Contract (docs/contracts.md, T-014):
 *   POST /api/membership/start
 *        {personnummer, email, phone, channel, accepted_bylaws, accepted_privacy,
 *         bylaws_version, privacy_version, checkin_id}
 *     -> {ok, state:"code_sent", attempt_id, masked_destination, resend_after_s}
 *   POST /api/membership/verify  {attempt_id, code}
 *     -> {ok, state, error_code?, user_message?, tries_left?}
 *   POST /api/membership/resend  {attempt_id} -> like start
 *   GET  /api/membership/{attempt_id}/status -> {state, error_code?}
 *
 * Rules this file follows:
 *   - API data is only ever written with textContent; no HTML string APIs at all.
 *   - The personnummer lives only in the input field: never localStorage/sessionStorage.
 *     sessionStorage holds at most the non-sensitive attempt_id while a registration is pending.
 *   - Everything is wiped on "Cancel and clear" or after 90 s of inactivity (kiosk hygiene),
 *     except while a registration is actually in flight with Sverok.
 */
(function () {
  "use strict";

  // Kiosk hygiene, part 1: older flows left PII in localStorage. Remove it before the
  // page's inline script runs (this file is included above it).
  ["personnummer", "telefon"].forEach(function (key) {
    try { localStorage.removeItem(key); } catch (e) { /* storage disabled */ }
  });

  const section = document.getElementById("section-ebas");
  if (!section || section.dataset.membershipFlow !== "v2") return;

  const ATTEMPT_KEY = "mv2_attempt_id";
  const START_TIMEOUT_MS = 20000;
  const VERIFY_TIMEOUT_MS = 200000; // Sverok can take ~3 min; a timeout means "unknown", not "failed".
  const STATUS_TIMEOUT_MS = 5000;
  const BUSY_DELAY_MS = 800; // don't flash the 3-minute message for a fast wrong-code answer
  const IDLE_MS = Number(section.dataset.idleMs) || 90000;
  const POLL_MS = Number(section.dataset.pollMs) || 5000;
  const POLL_MAX_MS = Number(section.dataset.pollMaxMs) || 240000;

  const params = new URLSearchParams(window.location.search);
  const CHECKIN_ID = params.get("checkin_id") || null;

  // --- Copy -----------------------------------------------------------------------------
  // English only for now; the app-wide Swedish switch comes later (sverok-texts.md).
  // Keeping every dynamic string here makes that switch a dictionary, not a hunt.
  const MESSAGES = {
    start_button: "Become a member",
    verify_button: "Confirm code",
    sending: "Sending code…",
    checking_code: "Checking the code…",
    destination_sms: "We sent a code by text message to {dest}.",
    destination_email: "We sent a code by email to {dest}.",
    destination_unknown: "We sent you a code.",
    resend: "Send again",
    resend_wait: "Send again ({s} s)",
    start_wait: "Become a member ({s} s)",
    verify_wait: "Confirm code ({s} s)",
    resent: "A new code has been sent.",
    submitting: "Registering with Sverok – this can take up to 3 minutes",
    checking: "We're checking your registration…",
    success: "You're a member! Thanks for supporting FGC Trollhättan.",
    already_member: "You're already a member – perfect!",
    err_pnr: "Check your personal ID: 12 digits, YYYYMMDDXXXX.",
    err_email: "Enter a valid email address.",
    err_phone: "Enter a valid phone number.",
    err_code_format: "The code is 6 digits.",
    wrong_code: "Wrong code. Try again.",
    wrong_code_tries: "Wrong code. {n} tries left.",
    wrong_code_one: "Wrong code. 1 try left.",
    expired: "The code has expired. Tap “Send again” to get a new one.",
    rate_limited: "Too many attempts. Wait {s} seconds and try again.",
    rate_limited_nos: "Too many attempts. Wait a moment and try again.",
    err_network: "Couldn't reach the server. Try again in a moment.",
    err_generic: "Something went wrong. Try again or ask a TO for help.",
    help_locked_title: "Too many wrong codes",
    help_locked: "For your security this attempt is locked. Please ask a TO for help.",
    help_failed_title: "The registration didn't go through",
    help_failed: "Please ask a TO for help and we'll sort it out together.",
    help_unknown_title: "We couldn't confirm your registration yet",
    help_unknown: "Please don't register again. Ask a TO to check your membership.",
    reset_button: "Cancel and clear",
    next_person: "Next person – clear screen",
    previous_pending:
      "The previous registration is still being processed. If it was yours, please ask a TO " +
      "before registering again.",
  };

  function t(key, vars) {
    let text = MESSAGES[key] || "";
    Object.keys(vars || {}).forEach(function (name) {
      text = text.split("{" + name + "}").join(String(vars[name]));
    });
    return text;
  }

  // --- Elements -------------------------------------------------------------------------
  const $ = function (id) { return document.getElementById(id); };
  const el = {
    form: $("mv2-step-form"),
    codeForm: $("mv2-step-code"),
    busy: $("mv2-step-busy"),
    help: $("mv2-step-help"),
    status: $("mv2-status"),
    error: $("mv2-error"),
    pnr: $("mv2-pnr"),
    email: $("mv2-email"),
    phone: $("mv2-phone"),
    bylaws: $("mv2-consent-bylaws"),
    privacy: $("mv2-consent-privacy"),
    consentHint: $("mv2-consent-hint"),
    start: $("mv2-start"),
    code: $("mv2-code"),
    verify: $("mv2-verify"),
    resend: $("mv2-resend"),
    edit: $("mv2-edit"),
    reset: $("mv2-reset"),
    destination: $("mv2-destination"),
    busyText: $("mv2-busy-text"),
    helpHeading: $("mv2-help-heading"),
    helpText: $("mv2-help-text"),
  };
  const fieldErrors = {
    pnr: $("mv2-pnr-error"),
    email: $("mv2-email-error"),
    phone: $("mv2-phone-error"),
    code: $("mv2-code-error"),
  };

  // --- State ----------------------------------------------------------------------------
  // `gen` is bumped on every wipe; async responses from an older generation are ignored,
  // so a slow answer can never re-populate the screen for the next person.
  const S = {
    gen: 0,
    attemptId: null,
    channel: "sms",
    inFlight: false,
    busy: false,
    pollDeadline: 0,
    timers: {},
    countdowns: {},
    pageMessage: null,
  };

  function clearTimer(name) {
    if (S.timers[name]) { clearTimeout(S.timers[name]); delete S.timers[name]; }
  }

  // --- Small UI helpers -----------------------------------------------------------------
  function showStep(name) {
    el.form.hidden = name !== "form";
    el.codeForm.hidden = name !== "code";
    el.busy.hidden = name !== "busy";
    el.help.hidden = name !== "help";
    // Always available, also while Sverok works (the server finishes on its own).
    el.reset.textContent = name === "busy" ? t("next_person") : t("reset_button");
  }

  function say(region, text) {
    region.textContent = text || "";
    region.hidden = !text;
  }
  function showInfo(text) { say(el.error, ""); say(el.status, text); }
  function showError(text) { say(el.status, ""); say(el.error, text); }
  function clearAlerts() { say(el.status, ""); say(el.error, ""); }

  function setFieldError(name, input, text) {
    fieldErrors[name].textContent = text || "";
    if (text) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function clearFieldErrors() {
    setFieldError("pnr", el.pnr, "");
    setFieldError("email", el.email, "");
    setFieldError("phone", el.phone, "");
    setFieldError("code", el.code, "");
  }

  function setButtonSpinner(btn, text) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.replaceChildren(spinner, document.createTextNode(text));
  }

  function restoreButton(btn, text, disabled) {
    btn.removeAttribute("aria-busy");
    btn.textContent = text;
    btn.disabled = !!disabled;
  }

  function consentsGiven() { return el.bylaws.checked === true && el.privacy.checked === true; }

  // "Become a member" stays disabled until both boxes are ticked (sverok-texts.md).
  function restoreStart() {
    if (S.countdowns[el.start.id]) return; // a rate-limit countdown owns the button
    restoreButton(el.start, t("start_button"), !consentsGiven());
    el.consentHint.hidden = consentsGiven();
  }

  // A button that is disabled with a live "(N s)" countdown, then handed to onReady.
  function countdown(btn, seconds, waitKey, onReady) {
    stopCountdown(btn);
    const end = Date.now() + Math.max(0, seconds) * 1000;
    function tick() {
      const left = Math.ceil((end - Date.now()) / 1000);
      if (left <= 0) {
        stopCountdown(btn);
        onReady();
        return;
      }
      restoreButton(btn, t(waitKey, { s: left }), true);
      S.countdowns[btn.id] = setTimeout(tick, Math.min(1000, end - Date.now()));
    }
    tick();
  }

  function stopCountdown(btn) {
    if (S.countdowns[btn.id]) { clearTimeout(S.countdowns[btn.id]); delete S.countdowns[btn.id]; }
  }

  const resendReady = function () { restoreButton(el.resend, t("resend"), false); };
  const verifyReady = function () { restoreButton(el.verify, t("verify_button"), false); };

  function digits(value) { return String(value || "").replace(/\D/g, ""); }

  function legalVersion(doc) {
    const node = section.querySelector('[data-legal-doc="' + doc + '"]');
    return node ? node.dataset.legalVersion || null : null;
  }

  function setDestination(channel, masked) {
    if (!masked) { el.destination.textContent = t("destination_unknown"); return; }
    const key = channel === "email" ? "destination_email" : "destination_sms";
    el.destination.textContent = t(key, { dest: masked });
  }

  function errorCopy(data, retryAfter) {
    const code = data && data.error_code;
    if (code === "rate_limited") {
      return retryAfter ? t("rate_limited", { s: retryAfter }) : t("rate_limited_nos");
    }
    if (code === "expired") return t("expired");
    if (code === "wrong_code") {
      const n = data.tries_left;
      if (n === 1) return t("wrong_code_one");
      return Number.isInteger(n) && n > 1 ? t("wrong_code_tries", { n: n }) : t("wrong_code");
    }
    // Unknown codes: the backend's own message (plain text), else a generic fallback.
    return (data && typeof data.user_message === "string" && data.user_message) || t("err_generic");
  }

  // --- API ------------------------------------------------------------------------------
  async function api(method, url, body, timeoutMs) {
    const init = { method: method, credentials: "same-origin" };
    if (body) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    if (timeoutMs && typeof AbortSignal !== "undefined" && AbortSignal.timeout) {
      init.signal = AbortSignal.timeout(timeoutMs);
    }
    const res = await fetch(url, init);
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!data || typeof data !== "object") data = {};
    const header = parseInt(res.headers.get("Retry-After") || "", 10);
    const retryAfter = Number.isFinite(data.retry_after_s) ? data.retry_after_s
      : (Number.isFinite(header) ? header : null);
    return { status: res.status, ok: res.ok && data.ok !== false, data: data, retryAfter: retryAfter };
  }

  // --- Step 1: details -> code_sent -----------------------------------------------------
  function validateForm() {
    clearFieldErrors();
    const problems = [];
    const pnr = digits(el.pnr.value);
    const pnrOk = typeof Validation !== "undefined"
      ? Validation.validatePersonnummer(pnr).valid
      : pnr.length === 10 || pnr.length === 12;
    if (!pnrOk) { setFieldError("pnr", el.pnr, t("err_pnr")); problems.push(el.pnr); }

    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(el.email.value.trim())) {
      setFieldError("email", el.email, t("err_email"));
      problems.push(el.email);
    }
    if (digits(el.phone.value).length < 7) {
      setFieldError("phone", el.phone, t("err_phone"));
      problems.push(el.phone);
    }
    return problems;
  }

  function enterCodeStep(data, channel) {
    S.attemptId = data.attempt_id;
    S.channel = channel;
    setDestination(channel, data.masked_destination);
    el.code.value = "";
    setFieldError("code", el.code, "");
    verifyReady();
    const wait = Number.isFinite(data.resend_after_s) ? data.resend_after_s : 60;
    countdown(el.resend, wait, "resend_wait", resendReady);
    showStep("code");
    el.code.focus();
  }

  el.form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (S.inFlight || !consentsGiven()) return;
    const problems = validateForm();
    if (problems.length) { problems[0].focus(); return; }

    const channel = el.form.querySelector('input[name="mv2-channel"]:checked').value;
    const payload = {
      personnummer: digits(el.pnr.value),
      email: el.email.value.trim(),
      phone: el.phone.value.trim(),
      channel: channel,
      // Strict booleans: the backend rejects anything but `true`.
      accepted_bylaws: el.bylaws.checked === true,
      accepted_privacy: el.privacy.checked === true,
      bylaws_version: legalVersion("bylaws"),
      privacy_version: legalVersion("privacy"),
      checkin_id: CHECKIN_ID,
    };

    const gen = S.gen;
    S.inFlight = true;
    clearAlerts();
    setButtonSpinner(el.start, t("sending"));
    let result;
    try {
      result = await api("POST", "/api/membership/start", payload, START_TIMEOUT_MS);
    } catch (e) {
      result = null;
    }
    if (gen !== S.gen) return;
    S.inFlight = false;

    if (!result) {
      restoreStart();
      showError(t("err_network"));
      return;
    }
    if (result.ok && result.data.state === "code_sent" && result.data.attempt_id) {
      restoreStart();
      clearAlerts();
      enterCodeStep(result.data, channel);
      return;
    }
    if (result.data.error_code === "already_member" || result.data.state === "already_member") {
      succeed(true);
      return;
    }
    if (result.data.error_code === "rate_limited") {
      countdown(el.start, result.retryAfter || 30, "start_wait", restoreStart);
    } else {
      restoreStart();
    }
    showError(errorCopy(result.data, result.retryAfter));
  });

  el.pnr.addEventListener("input", function () {
    const cleaned = el.pnr.value.replace(/[^\d-]/g, "");
    if (cleaned !== el.pnr.value) el.pnr.value = cleaned;
    setFieldError("pnr", el.pnr, "");
  });
  el.email.addEventListener("input", function () { setFieldError("email", el.email, ""); });
  el.phone.addEventListener("input", function () { setFieldError("phone", el.phone, ""); });
  el.bylaws.addEventListener("change", restoreStart);
  el.privacy.addEventListener("change", restoreStart);

  // --- Step 2: code -> verify -----------------------------------------------------------
  el.code.addEventListener("input", function () {
    const cleaned = digits(el.code.value).slice(0, 6);
    if (cleaned !== el.code.value) el.code.value = cleaned;
    setFieldError("code", el.code, "");
  });

  function backToCode(message, opts) {
    S.busy = false;
    clearTimer("busyDelay");
    showStep("code");
    showError(message);
    el.code.value = "";
    if (opts && opts.cooldown) countdown(el.verify, opts.cooldown, "verify_wait", verifyReady);
    else verifyReady();
    if (opts && opts.resendNow) { stopCountdown(el.resend); resendReady(); }
    el.code.focus();
  }

  el.codeForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    // The in-flight flag (not just `disabled`) blocks double clicks AND Enter+click races.
    if (S.inFlight || el.verify.disabled) return;
    const code = digits(el.code.value);
    if (code.length !== 6) {
      setFieldError("code", el.code, t("err_code_format"));
      el.code.focus();
      return;
    }

    const gen = S.gen;
    S.inFlight = true;
    S.busy = true;
    clearAlerts();
    setButtonSpinner(el.verify, t("checking_code"));
    el.resend.disabled = true;
    el.edit.disabled = true;
    // From here the registration may reach Sverok, so a reload must resume, not restart.
    try {
      sessionStorage.setItem(ATTEMPT_KEY,
        JSON.stringify({ attempt_id: S.attemptId, checkin_id: CHECKIN_ID }));
    } catch (e) { /* ignore */ }
    S.timers.busyDelay = setTimeout(function () { enterBusy(t("submitting")); }, BUSY_DELAY_MS);

    let result;
    try {
      result = await api("POST", "/api/membership/verify",
        { attempt_id: S.attemptId, code: code }, VERIFY_TIMEOUT_MS);
    } catch (e) {
      result = null;
    }
    if (gen !== S.gen) return;
    clearTimer("busyDelay");
    S.inFlight = false;
    el.edit.disabled = false;
    if (!S.countdowns[el.resend.id]) el.resend.disabled = false;
    handleVerify(result);
  });

  function handleVerify(result) {
    // No answer, or a server error: Sverok may or may not have the registration. Never let
    // the person retry blindly; ask the backend instead.
    if (!result || (result.status >= 500 && !result.data.error_code)) {
      startPolling(t("checking"));
      return;
    }
    const data = result.data;
    const code = data.error_code;
    if (data.state === "submitted") { succeed(false); return; }
    if (data.state === "already_member" || code === "already_member") { succeed(true); return; }
    if (data.state === "submitting") { startPolling(t("submitting")); return; }
    if (data.state === "outcome_unknown" || code === "outcome_unknown") {
      startPolling(t("checking"));
      return;
    }
    if (code === "locked" || (code === "wrong_code" && data.tries_left === 0)) {
      showHelp("help_locked_title", "help_locked");
      return;
    }
    if (code === "failed") { showHelp("help_failed_title", "help_failed"); return; }
    // Everything below was rejected before Sverok: nothing to resume after a reload.
    forgetAttempt();
    if (code === "expired") { backToCode(t("expired"), { resendNow: true }); return; }
    if (code === "rate_limited") {
      backToCode(errorCopy(data, result.retryAfter), { cooldown: result.retryAfter || 30 });
      return;
    }
    backToCode(errorCopy(data, result.retryAfter));
  }

  el.resend.addEventListener("click", async function () {
    if (S.inFlight || el.resend.disabled || !S.attemptId) return;
    const gen = S.gen;
    S.inFlight = true;
    clearAlerts();
    setButtonSpinner(el.resend, t("sending"));
    let result;
    try {
      result = await api("POST", "/api/membership/resend",
        { attempt_id: S.attemptId }, START_TIMEOUT_MS);
    } catch (e) {
      result = null;
    }
    if (gen !== S.gen) return;
    S.inFlight = false;
    if (!result) {
      resendReady();
      showError(t("err_network"));
      return;
    }
    const data = result.data;
    if (result.ok && data.state === "code_sent") {
      if (data.attempt_id) S.attemptId = data.attempt_id;
      if (data.masked_destination) setDestination(S.channel, data.masked_destination);
      const wait = Number.isFinite(data.resend_after_s) ? data.resend_after_s : 60;
      countdown(el.resend, wait, "resend_wait", resendReady);
      verifyReady();
      el.code.value = "";
      showInfo(t("resent"));
      el.code.focus();
      return;
    }
    if (data.error_code === "locked") { showHelp("help_locked_title", "help_locked"); return; }
    if (data.error_code === "rate_limited") {
      countdown(el.resend, result.retryAfter || 30, "resend_wait", resendReady);
    } else {
      resendReady();
    }
    showError(errorCopy(data, result.retryAfter));
  });

  el.edit.addEventListener("click", function () {
    if (S.inFlight) return;
    // Back to the (still filled-in) form; the next submit starts a fresh attempt.
    S.attemptId = null;
    stopCountdown(el.resend);
    clearAlerts();
    restoreStart();
    showStep("form");
    el.pnr.focus();
  });

  // --- Step 3: waiting for Sverok -------------------------------------------------------
  function enterBusy(text) {
    S.busy = true;
    clearAlerts();
    // The details are already sent; nothing personal needs to stay on screen while waiting.
    [el.pnr, el.email, el.phone, el.code].forEach(function (input) { input.value = ""; });
    showStep("busy");
    el.busyText.textContent = text; // inside a polite live region
  }

  function startPolling(text) {
    enterBusy(text);
    S.pollDeadline = Date.now() + POLL_MAX_MS;
    schedulePoll();
  }

  function schedulePoll() {
    clearTimer("poll");
    const gen = S.gen;
    S.timers.poll = setTimeout(async function () {
      if (gen !== S.gen) return;
      let result = null;
      try {
        result = await api("GET",
          "/api/membership/" + encodeURIComponent(S.attemptId) + "/status",
          null, Math.min(STATUS_TIMEOUT_MS, Math.max(1000, S.pollDeadline - Date.now())));
      } catch (e) {
        result = null; // transient: keep polling until the deadline
      }
      if (gen !== S.gen) return;
      const data = (result && result.data) || {};
      if (data.state === "submitted") { succeed(false); return; }
      if (data.state === "already_member") { succeed(true); return; }
      if (data.state === "failed" || data.error_code === "failed") {
        showHelp("help_failed_title", "help_failed");
        return;
      }
      if (data.state === "locked" || data.error_code === "locked") {
        showHelp("help_locked_title", "help_locked");
        return;
      }
      if (result && result.status === 404) {
        showHelp("help_unknown_title", "help_unknown");
        return;
      }
      if (data.state === "outcome_unknown" && el.busyText.textContent !== t("checking")) {
        enterBusy(t("checking"));
      }
      if (Date.now() >= S.pollDeadline) {
        showHelp("help_unknown_title", "help_unknown");
        return;
      }
      schedulePoll();
    }, POLL_MS);
  }

  // --- Terminal states ------------------------------------------------------------------
  function forgetAttempt() {
    try { sessionStorage.removeItem(ATTEMPT_KEY); } catch (e) { /* ignore */ }
  }

  function showHelp(titleKey, textKey) {
    S.busy = false;
    S.inFlight = false;
    clearTimer("poll");
    clearTimer("busyDelay");
    forgetAttempt();
    clearAlerts();
    el.helpHeading.textContent = t(titleKey);
    el.helpText.textContent = t(textKey);
    showStep("help");
    el.helpHeading.focus();
    bumpIdle();
  }

  function writePageMessage(text) {
    // Same shared #messages container (and look) as the legacy success message.
    const container = document.getElementById("messages");
    if (!container) return;
    const message = document.createElement("div");
    message.className = "message success";
    message.setAttribute("role", "status");
    message.textContent = text;
    container.replaceChildren(message);
    S.pageMessage = message;
  }

  // Hand off to register.html exactly like the legacy success path does.
  function succeed(already) {
    const membershipOnly = params.get("membership_only") === "true";
    wipe();
    writePageMessage(already ? t("already_member") : t("success"));
    /* global state, setStatus, updateFinalButton */
    if (typeof state === "object" && state) state.needsEbas = false;
    if (typeof setStatus === "function") setStatus("status-member", true);
    section.classList.add("hidden");

    if (membershipOnly) {
      const name = (typeof state === "object" && state && state.name) || params.get("name") || "";
      const qs = [
        sessionStorage.getItem("kioskMode") === "true" ? "kiosk=true" : "",
        CHECKIN_ID ? "checkin_id=" + encodeURIComponent(CHECKIN_ID) : "",
      ].filter(Boolean).join("&");
      setTimeout(function () {
        window.location.href = "/status/" + encodeURIComponent(name) + (qs ? "?" + qs : "");
      }, 2000);
      return;
    }
    if (typeof updateFinalButton === "function") updateFinalButton();
    const next = document.getElementById("btn-final-checkin");
    if (next && next.offsetParent !== null) next.focus();
  }

  // --- Kiosk hygiene --------------------------------------------------------------------
  // Clears every field, timer and message of this step. Does not touch section visibility.
  function wipe() {
    S.gen += 1;
    Object.keys(S.timers).forEach(clearTimer);
    [el.start, el.verify, el.resend].forEach(stopCountdown);
    S.attemptId = null;
    S.inFlight = false;
    S.busy = false;
    S.channel = "sms";
    forgetAttempt();
    el.form.reset();
    el.codeForm.reset();
    section.querySelectorAll("details").forEach(function (d) { d.open = false; });
    clearFieldErrors();
    clearAlerts();
    el.destination.textContent = "";
    el.busyText.textContent = "";
    el.helpHeading.textContent = "";
    el.helpText.textContent = "";
    restoreStart();
    verifyReady();
    restoreButton(el.resend, t("resend"), true);
    el.edit.disabled = false;
    showStep("form");
  }

  function reset() {
    wipe();
    if (S.pageMessage && S.pageMessage.isConnected) S.pageMessage.remove();
    S.pageMessage = null;
    bumpIdle();
  }

  // After 90 s without input the screen ALWAYS goes back to a clean start, even while Sverok
  // is still working: the server finishes on its own, and the generation counter drops any
  // late answer, so it can never land on the next person's screen.
  function bumpIdle() {
    clearTimer("idle");
    S.timers.idle = setTimeout(function () {
      const wasPending = S.busy || S.inFlight;
      reset();
      // Neutral (no personal data): tells a returning person not to register twice.
      if (wasPending) showInfo(t("previous_pending"));
    }, IDLE_MS);
  }

  el.reset.addEventListener("click", function () {
    reset();
    if (!section.classList.contains("hidden")) el.pnr.focus();
  });
  // Leaving via the page's "Back to start" link ends this person's attempt for the tab.
  const backLink = document.getElementById("back-link");
  if (backLink) backLink.addEventListener("click", forgetAttempt);
  ["pointerdown", "keydown", "input", "touchstart", "wheel"].forEach(function (type) {
    document.addEventListener(type, bumpIdle, { passive: true, capture: true });
  });

  // --- Init -----------------------------------------------------------------------------
  restoreStart();
  restoreButton(el.resend, t("resend"), true);
  showStep("form");
  bumpIdle();

  // A reload during a pending registration resumes the status check instead of offering
  // the form again (which could create a duplicate). The stored attempt is bound to the
  // check-in it was started for: it resumes only on a page for that same, known check-in,
  // so a later person in the same tab can never inherit someone else's result.
  // Runs after register.html's initUI.
  document.addEventListener("DOMContentLoaded", function () {
    let pending = null;
    try { pending = JSON.parse(sessionStorage.getItem(ATTEMPT_KEY) || "null"); } catch (e) {
      pending = null;
    }
    const sameCheckin = !!(pending && pending.attempt_id && CHECKIN_ID &&
      pending.checkin_id === CHECKIN_ID);
    if (!sameCheckin || section.classList.contains("hidden")) { forgetAttempt(); return; }
    S.attemptId = pending.attempt_id;
    startPolling(t("checking"));
  });

  window.MembershipV2 = { reset: reset };
})();
