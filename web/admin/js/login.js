// Login page: POST api/login {password}. Paths are relative to the app root (docs/contracts/remote.md).
// A guest link (admin/login.html#guest=<token>, docs/contracts/api.md "Guest access") is exchanged for a read-only
// guest session at once (POST api/guest/login) and the token is taken out of the address bar before anything else.
import { appUrl, safeNext } from "./api.js";
import { needsAppLogin, markAppLogin } from "./app-login.js";
import { guestToken } from "./guest-fmt.js";

const form = document.getElementById("form");
const pw = document.getElementById("pw");
const err = document.getElementById("err");
const go = document.getElementById("go");
const guestNote = document.getElementById("guestnote");

// only paths inside admin/ (no open redirect)
const nextUrl = () => safeNext(new URLSearchParams(location.search).get("next"));

// the installed app's first start on this device: the password even if the browser is signed in (app-login.js)
const appFirst = needsAppLogin();

const guest = guestToken(location.hash);
if (guest) {
  // out of the address bar (and the history entry) right away: the token is only sent in the POST body below
  history.replaceState(null, "", location.pathname + location.search);
  openGuest(guest);
} else {
  if (appFirst) document.getElementById("appnote").hidden = false;
  // already signed in? go straight to the panel
  if (!appFirst) {
    fetch(appUrl("api/session"), { credentials: "same-origin" })
      .then((r) => r.json())
      .then((s) => { if (s && s.authenticated) location.replace(nextUrl()); })
      .catch(() => {});
  }
}

async function openGuest(token) {
  guestNote.hidden = false;
  go.disabled = true;
  try {
    const r = await fetch(appUrl("api/guest/login"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
    if (r.ok) {
      markAppLogin(); // the installed app would otherwise ask a guest for the password (app-login.js)
      location.replace(appUrl("admin/"));
      return;
    }
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* */ }
    err.textContent = r.status === 401 ? "This guest link is invalid, expired or revoked"
      : r.status === 429 ? detail || "Too many attempts, wait 30 seconds" : detail || "Guest link error (" + r.status + ")";
  } catch {
    err.textContent = "No connection to the device";
  } finally {
    guestNote.hidden = true;
    go.disabled = false;
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.textContent = "";
  go.disabled = true;
  try {
    const r = await fetch(appUrl("api/login"), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw.value }),
    });
    if (r.ok) {
      markAppLogin();
      pw.blur(); // the keyboard closes before the panel loads (its height is measured without it)
      location.replace(nextUrl());
      return;
    }
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* */ }
    err.textContent = r.status === 401 ? "Wrong password" : r.status === 429 ? detail || "Too many attempts, wait 30 seconds" : detail || "Sign-in error (" + r.status + ")";
    pw.select();
  } catch {
    err.textContent = "No connection to the device";
  } finally {
    go.disabled = false;
  }
});
