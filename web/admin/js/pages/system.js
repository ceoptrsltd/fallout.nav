// System: services, logs, the time zone, Wi-Fi, password change, guest links (read-only access), reboot / shutdown.
import { h, icon, clear, fmt, isNum, fmtUptime } from "../dom.js";
import { api, errText, ROOT } from "../api.js";
import { toast, confirmDialog, poll, badge, banner, switchEl } from "../ui.js";
import { store, on, applySettings } from "../store.js";
import { zoneText } from "../devtime.js";
import { EXPIRY_CHOICES, ttlSeconds, localInputValue, guestUrls, fmtLeft, fmtAgo } from "../guest-fmt.js";

// journals the backend gives (fnav/core/system.py _UNIT_RE: any fnav-*, mediamtx, gpsd)
const DEFAULT_UNITS = ["fnav-core", "fnav-camera", "fnav-camera-rear", "fnav-watch", "fnav-display", "fnav-radio",
  "fnav-tunnel", "fnav-games", "mediamtx", "gpsd"];
const SVC_STATE = { active: ["running", "ok"], activating: ["starting", "warn"], inactive: ["stopped", "warn"], failed: ["failed", "bad"], deactivating: ["stopping", "warn"] };

export function mount(root) {
  let stopped = false;
  // ── services ──
  const svcBody = h("tbody");
  const sysKv = h(".kv");
  async function loadStatus() {
    try {
      const s = await api.get("/api/system/status");
      if (stopped) return;
      clear(svcBody);
      const svcs = s.services || {};
      for (const [name, st] of Object.entries(svcs)) {
        const [txt, kind] = SVC_STATE[st] || [st, st === "active" ? "ok" : ""];
        svcBody.append(h("tr", null, h("td.mono", null, name), h("td", null, badge(txt, kind)), h("td.num", null, h("span.dim", null, st))));
      }
      const sys = s.system || {}, d = s.disk || {};
      clear(sysKv).append(
        ...[["Version", s.version || "—"], ["IP", sys.ip || "—"], ["Wi‑Fi", sys.wifi_ssid ? `${sys.wifi_ssid} (${sys.wifi_signal_pct ?? "?"}%)` : "—"], ["Uptime", fmtUptime(sys.uptime_s)],
          ["CPU temperature", isNum(sys.cpu_temp_c) ? sys.cpu_temp_c.toFixed(1) + " °C" : "—"], ["Disk, free", isNum(d.free_gb) ? `${d.free_gb.toFixed(1)} of ${fmt(d.total_gb, 1)} GB` : "—"],
          ["Time synchronized", sys.time_synced === true ? "yes" : sys.time_synced === false ? "no" : "—"], ["Time zone", zoneText(sys)]]
          .flatMap(([k, v]) => [h("span.k", null, k), h("span.v", null, v)]),
      );
      if (s.mock || (s.version || "").includes("mock")) sysKv.append(h("span.k", null, "Mode"), h("span.v.amber", null, "mock"));
    } catch (e) {
      if (!stopped) clear(svcBody).append(h("tr", null, h("td", { colspan: 3, class: "red" }, errText(e))));
    }
  }
  const stopStatus = poll(loadStatus, 5000);

  // ── time zone: follows the van (settings.system.auto_timezone, fnav/core/timezone.py) ──
  const tzSw = switchEl({ checked: store.settings?.system?.auto_timezone !== false, label: "Time zone follows the van", onchange: async (v) => {
    try {
      const res = await api.patch("/api/settings", { system: { auto_timezone: v } });
      if (res && res.system) applySettings(res);
      toast("ok", v ? "The time zone will follow the van" : "The time zone stays as it is");
    } catch (e) { toast("error", errText(e)); tzSw.input.checked = !v; }
  } });
  const offSettings = on("settings", (st) => { if (st && st.system) tzSw.input.checked = st.system.auto_timezone !== false; });

  // ── logs ──
  const unitSel = h("select", null, DEFAULT_UNITS.map((u) => h("option", { value: u }, u)));
  const linesSel = h("select", null, [100, 200, 500, 1000].map((n) => h("option", { value: n, selected: n === 200 }, n + " lines")));
  const logEl = h(".log", { style: { height: "340px" } }, "Loading…");
  let logTimer = null;
  async function loadLogs(keepScroll) {
    try {
      const r = await api.get(`/api/system/logs?unit=${encodeURIComponent(unitSel.value)}&lines=${linesSel.value}`);
      if (stopped) return;
      const stick = !keepScroll || logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30;
      logEl.textContent = (r.lines || []).join("\n") || "(empty)";
      if (stick) logEl.scrollTop = logEl.scrollHeight;
    } catch (e) { logEl.textContent = "Error: " + errText(e); }
  }
  unitSel.addEventListener("change", () => loadLogs());
  linesSel.addEventListener("change", () => loadLogs());
  // auto-refresh only while the page is on screen; back on screen — at once
  const autoSw = switchEl({ checked: false, label: "Auto-refresh", onchange: (v) => {
    clearInterval(logTimer);
    logTimer = v ? setInterval(() => { if (!document.hidden) loadLogs(true); }, 3000) : null;
  } });
  const onVisible = () => { if (!document.hidden && logTimer) loadLogs(true); };
  document.addEventListener("visibilitychange", onVisible);
  loadLogs();

  // ── Wi‑Fi ──
  const wifiBody = h("tbody");
  async function loadWifi() {
    try {
      const list = await api.get("/api/system/wifi");
      if (stopped) return;
      clear(wifiBody);
      if (!list.length) wifiBody.append(h("tr", null, h("td", { colspan: 5, class: "dim" }, "No saved networks")));
      for (const w of list) {
        const prio = h("input", { type: "number", step: 1, value: w.priority ?? 0, style: { width: "80px" }, title: "Priority (higher is preferred)" });
        const apply = h("button.btn.sm", { type: "button", hidden: true, onclick: async () => {
          try { await api.post("/api/system/wifi", { ssid: w.ssid, priority: Number(prio.value) }); toast("ok", "Priority saved"); loadWifi(); } catch (e) { toast("error", errText(e)); }
        } }, "Save");
        prio.addEventListener("input", () => { apply.hidden = Number(prio.value) === (w.priority ?? 0); });
        wifiBody.append(h("tr", null,
          h("td", null, w.ssid, w.active ? " " : null, w.active ? badge("connected", "ok") : null),
          h("td.num", null, isNum(w.signal_pct) ? w.signal_pct + " %" : "—"),
          h("td", null, h("div", { style: { display: "flex", gap: "6px" } }, prio, apply)),
          h("td.right", null, h("button.btn.sm.danger", { type: "button", onclick: async () => {
            const ok = await confirmDialog({ title: "Forget network?", text: h("div", null, "The network “", h("b", null, w.ssid), "” will be forgotten." + (w.active ? " You are connected to it right now, so the connection to the device may be lost!" : "")), okText: "Forget", danger: true });
            if (!ok) return;
            // the SSID goes in the query: a "/" in it would not survive the path (%2F is decoded before routing)
            try { await api.del("/api/system/wifi?ssid=" + encodeURIComponent(w.ssid)); toast("ok", "Network removed"); loadWifi(); } catch (e) { toast("error", errText(e)); }
          } }, icon("trash"))),
        ));
      }
    } catch (e) { clear(wifiBody).append(h("tr", null, h("td", { colspan: 5, class: "red" }, errText(e)))); }
  }
  loadWifi();
  const ssid = h("input", { type: "text", placeholder: "Network name (SSID)", autocomplete: "off" });
  const wpw = h("input", { type: "password", placeholder: "Network password", autocomplete: "new-password" });
  const wprio = h("input", { type: "number", value: 0, step: 1, style: { width: "80px" }, title: "Priority" });
  const wifiForm = h("form", { class: "btnrow", style: { marginTop: "12px" }, onsubmit: async (e) => {
    e.preventDefault();
    if (!ssid.value.trim()) return;
    try {
      await api.post("/api/system/wifi", { ssid: ssid.value.trim(), password: wpw.value, priority: Number(wprio.value) || 0 });
      toast("ok", "Network added");
      ssid.value = ""; wpw.value = "";
      loadWifi();
    } catch (err) { toast("error", errText(err)); }
  } }, ssid, wpw, h("span.dim", null, "priority"), wprio, h("button.btn.primary", { type: "submit" }, icon("plus"), "Add"));

  // ── password ──
  const pOld = h("input", { type: "password", autocomplete: "current-password" });
  const pNew = h("input", { type: "password", autocomplete: "new-password" });
  const pNew2 = h("input", { type: "password", autocomplete: "new-password" });
  const pMsg = h(".hint");
  const pwForm = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    pMsg.className = "hint red";
    if (pNew.value.length < 6) return (pMsg.textContent = "The new password must be at least 6 characters");
    if (pNew.value !== pNew2.value) return (pMsg.textContent = "Passwords do not match");
    try {
      await api.post("/api/system/password", { old: pOld.value, new: pNew.value });
      pOld.value = pNew.value = pNew2.value = "";
      pMsg.className = "hint green";
      pMsg.textContent = "Password changed";
      toast("ok", "Password changed");
    } catch (err) { pMsg.textContent = errText(err); }
  } },
    h("div", { style: { display: "grid", gridTemplateColumns: "140px minmax(0,260px)", gap: "8px 12px", alignItems: "center" } },
      h("label.dim", null, "Current"), pOld, h("label.dim", null, "New"), pNew, h("label.dim", null, "Repeat"), pNew2),
    h("div.btnrow", { style: { marginTop: "12px" } }, h("button.btn.primary", { type: "submit" }, "Change password")),
    pMsg);

  // ── guest links: the whole admin for someone else, read only (docs/contracts/api.md "Guest access") ──
  const guestCard = h(".card", { hidden: true });
  const guestBody = h("tbody");
  const guestNew = h("div");
  const gLabel = h("input", { type: "text", placeholder: "Who is it for (optional)", maxlength: 64, autocomplete: "off" });
  // app.css styles text inputs only: the same look inline
  const gCustom = h("input", { type: "datetime-local", hidden: true, title: "The link stops working at", style: {
    background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line2)", borderRadius: "4px", padding: "6px 9px",
    font: "inherit", minHeight: "34px", colorScheme: "dark" } });
  const gExpiry = h("select", { title: "How long the link works", onchange: () => {
    gCustom.hidden = gExpiry.value !== "custom";
    if (!gCustom.hidden && !gCustom.value) gCustom.value = localInputValue(Date.now() + 3 * 86400e3);
  } }, EXPIRY_CHOICES.map((c) => h("option", { value: c.value }, c.label)));
  const fmtDate = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

  function renderGuests(res) {
    const now = typeof res.now === "number" ? res.now : Date.now() / 1000; // the device's clock
    clear(guestBody);
    if (!res.links.length) guestBody.append(h("tr", null, h("td", { colspan: 4, class: "dim" }, "No active guest links")));
    for (const l of res.links) {
      guestBody.append(h("tr", null,
        h("td", null, l.label || h("span.dim", null, "no label")),
        h("td", { title: fmtDate(l.expires) }, fmtLeft(l.expires - now)),
        h("td", { title: l.last_used ? fmtDate(l.last_used) : "" }, fmtAgo(l.last_used, now)),
        h("td.right", null, h("button.btn.sm.danger", { type: "button", title: "Revoke: the link stops working at once", onclick: async () => {
          const ok = await confirmDialog({ title: "Revoke the guest link?", text: h("div", null, "Whoever has the link “", h("b", null, l.label || "no label"), "” loses access at once."), okText: "Revoke", danger: true });
          if (!ok) return;
          try { renderGuests(await api.del("/api/guest/links/" + encodeURIComponent(l.id))); clear(guestNew); toast("ok", "Guest link revoked"); } catch (e) { toast("error", errText(e)); }
        } }, icon("trash"), "Revoke")),
      ));
    }
  }
  async function loadGuests() {
    try {
      const res = await api.get("/api/guest/links");
      if (stopped) return;
      guestCard.hidden = false;
      renderGuests(res);
    } catch (e) {
      if (e.status === 403) { guestCard.hidden = true; return; } // a guest: links are the owner's business
      guestCard.hidden = false;
      clear(guestBody).append(h("tr", null, h("td", { colspan: 4, class: "red" }, errText(e))));
    }
  }
  async function copyText(text, input) {
    try { await navigator.clipboard.writeText(text); toast("ok", "Link copied"); } catch { input.select(); toast("info", "Select and copy the link"); }
  }
  function showNewLink(res) {
    const urls = guestUrls(res.path, { origin: location.origin, root: ROOT, remoteOrigin: res.remote_origin });
    clear(guestNew).append(banner("ok", [
      h("div", null, h("b", null, "Copy the link now"), ": it is shown only once (the device keeps only a hash of it)."),
      ...urls.map((u) => {
        const inp = h("input", { type: "text", readonly: true, value: u.url, style: { flex: "1", minWidth: "0" }, onfocus: (e) => e.target.select() });
        return h("div", { style: { marginTop: "8px" } }, h("div.dim", null, u.where),
          h("div", { style: { display: "flex", gap: "6px" } }, inp,
            h("button.btn.sm", { type: "button", onclick: () => copyText(u.url, inp) }, "Copy"),
            navigator.share ? h("button.btn.sm", { type: "button", onclick: () => navigator.share({ title: "fallout.nav (read only)", url: u.url }).catch(() => {}) }, icon("share")) : null));
      }),
      ROOT === "/" && !res.remote_origin ? h("div.hint", null, "This address only works on the van's network. Create the link in the remote admin (<server>/fnadmin/) once and the address through the server is shown here too.") : null,
    ]));
  }
  const guestForm = h("form", { class: "btnrow", style: { marginTop: "12px" }, onsubmit: async (e) => {
    e.preventDefault();
    const t = ttlSeconds(gExpiry.value, gCustom.value, Date.now());
    if (t.error) return toast("error", t.error);
    try {
      const res = await api.post("/api/guest/links", { label: gLabel.value.trim(), expires_in_s: t.s });
      gLabel.value = "";
      renderGuests(res);
      showNewLink(res);
    } catch (err) { toast("error", errText(err)); }
  } }, gLabel, gExpiry, gCustom, h("button.btn.primary", { type: "submit" }, icon("plus"), "Create link"));
  guestCard.append(
    h(".card-head", null, h("h2", null, "Guest access (read only)"), h("button.btn.sm", { type: "button", onclick: loadGuests }, icon("refresh"))),
    h(".hint", null, "A guest link opens this whole admin for someone else, at the van and through the server: they see everything, "
      + "but cannot change anything. Wi-Fi passwords and keys are not shown to guests."),
    guestForm, guestNew,
    h(".tbl-wrap", { style: { marginTop: "12px" } }, h("table", null, h("thead", null, h("tr", null, h("th", null, "Guest"), h("th", null, "Expires"), h("th", null, "Last used"), h("th"))), guestBody)));
  loadGuests();

  // ── power ──
  async function power(kind) {
    const word = kind === "reboot" ? "reboot" : "shutdown";
    const ok = await confirmDialog({
      title: kind === "reboot" ? "Reboot the device?" : "Shut down the device?",
      text: kind === "reboot" ? "Recording will stop and the device will be unreachable for about a minute." : "Recording will stop and the device will power off. It can only be turned on again by cycling power.",
      okText: kind === "reboot" ? "Reboot" : "Shut down",
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await api.post("/api/system/" + kind);
      toast(r && r.mock ? "info" : "warn", r && r.mock ? `Mock mode: ${word} not performed` : kind === "reboot" ? "Rebooting…" : "Shutting down…");
    } catch (e) { toast("error", errText(e)); }
  }
  async function resetSettings() {
    if (!(await confirmDialog({ title: "Reset all settings?", text: "All parameters go back to their defaults (including the compass calibration, Esri key and screen size).", okText: "Reset", danger: true }))) return;
    try {
      const s = await api.post("/api/settings/reset");
      if (s && s.display) applySettings(s);
      toast("ok", "Settings reset");
    } catch (e) { toast("error", errText(e)); }
  }

  root.append(
    h(".page-head", null, h("h1", null, "System")),
    h(".grid.g2", null,
      h(".card", null, h("h2", null, "Services"), h(".tbl-wrap", null, h("table", null, h("thead", null, h("tr", null, h("th", null, "Service"), h("th", null, "State"), h("th.num", null, "systemd"))), svcBody))),
      h(".card", null, h("h2", null, "Device"), sysKv,
        h(".btnrow", { style: { marginTop: "12px" } }, tzSw),
        h(".hint", null, "Recording names, alarm notifications and the car screen clock use the device's time zone. "
          + "When it follows the van, it changes to the local one some 30–60 min after a border (by the weather forecast for the GPS position).")),
    ),
    h(".card", null,
      h(".card-head", null, h("h2", null, "Logs"), unitSel, linesSel, autoSw, h("button.btn.sm", { type: "button", onclick: () => loadLogs() }, icon("refresh"), "Refresh")),
      logEl),
    h(".card", null, h(".card-head", null, h("h2", null, "Wi‑Fi networks"), h("button.btn.sm", { type: "button", onclick: loadWifi }, icon("refresh"))),
      h(".tbl-wrap", null, h("table", null, h("thead", null, h("tr", null, h("th", null, "Network"), h("th.num", null, "Signal"), h("th", null, "Priority"), h("th"))), wifiBody)),
      h("h3", { style: { marginTop: "14px" } }, "Add a network"), wifiForm,
      h(".hint", null, "The device connects to the available network with the highest priority.")),
    guestCard,
    h(".grid.g2", null,
      h(".card", null, h("h2", null, "Login password"), pwForm),
      h(".card", null, h("h2", null, "Power and reset"),
        h("div.btnrow", null,
          h("button.btn.danger", { type: "button", onclick: () => power("reboot") }, icon("refresh"), "Reboot"),
          h("button.btn.danger", { type: "button", onclick: () => power("shutdown") }, "Shut down"),
          h("button.btn", { type: "button", onclick: resetSettings }, "Reset settings")),
        h(".hint", null, "Before cutting power it is better to press \"Shut down\" so the filesystem closes cleanly (recording survives power loss anyway).")),
    ),
  );
  return () => { stopped = true; stopStatus(); offSettings(); clearInterval(logTimer); document.removeEventListener("visibilitychange", onVisible); };
}
