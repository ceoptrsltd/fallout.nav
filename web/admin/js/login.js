// Login page: POST api/login {password}. Paths are relative to the app root (docs/contracts/remote.md).
import { appUrl, safeNext } from "./api.js";
import { needsAppLogin, markAppLogin } from "./app-login.js";

const form = document.getElementById("form");
const pw = document.getElementById("pw");
const err = document.getElementById("err");
const go = document.getElementById("go");

// only paths inside admin/ (no open redirect)
const nextUrl = () => safeNext(new URLSearchParams(location.search).get("next"));

// the installed app's first start on this device: the password even if the browser is signed in (app-login.js)
const appFirst = needsAppLogin();
if (appFirst) document.getElementById("appnote").hidden = false;

// already signed in? go straight to the panel
if (!appFirst) {
  fetch(appUrl("api/session"), { credentials: "same-origin" })
    .then((r) => r.json())
    .then((s) => { if (s && s.authenticated) location.replace(nextUrl()); })
    .catch(() => {});
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
