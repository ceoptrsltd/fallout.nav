// Login page: POST /api/login {password}.
const form = document.getElementById("form");
const pw = document.getElementById("pw");
const err = document.getElementById("err");
const go = document.getElementById("go");

function nextUrl() {
  const n = new URLSearchParams(location.search).get("next");
  // only paths inside /admin/ (no open redirect)
  return n && /^\/admin\/[\w\-./#?=&%]*$/.test(n) && !n.includes("//") && !n.includes("login.html") ? n : "/admin/";
}

// already signed in? go straight to the panel
fetch("/api/session", { credentials: "same-origin" })
  .then((r) => r.json())
  .then((s) => { if (s && s.authenticated) location.replace(nextUrl()); })
  .catch(() => {});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  err.textContent = "";
  go.disabled = true;
  try {
    const r = await fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw.value }),
    });
    if (r.ok) {
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
