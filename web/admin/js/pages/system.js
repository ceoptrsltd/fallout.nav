// System: services, logs, Wi-Fi, password change, reboot / shutdown.
import { h, icon, clear, fmt, isNum, fmtUptime } from "../dom.js";
import { api, errText } from "../api.js";
import { toast, confirmDialog, poll, badge, banner, switchEl } from "../ui.js";
import { applySettings } from "../store.js";

const DEFAULT_UNITS = ["fnav-core", "fnav-camera", "fnav-display", "mediamtx", "gpsd"];
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
          ["Time synchronized", sys.time_synced === true ? "yes" : sys.time_synced === false ? "no" : "—"]]
          .flatMap(([k, v]) => [h("span.k", null, k), h("span.v", null, v)]),
      );
      if (s.mock || (s.version || "").includes("mock")) sysKv.append(h("span.k", null, "Mode"), h("span.v.amber", null, "mock"));
    } catch (e) {
      if (!stopped) clear(svcBody).append(h("tr", null, h("td", { colspan: 3, class: "red" }, errText(e))));
    }
  }
  const stopStatus = poll(loadStatus, 5000);

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
  const autoSw = switchEl({ checked: false, label: "Auto-refresh", onchange: (v) => {
    clearInterval(logTimer);
    if (v) logTimer = setInterval(() => loadLogs(true), 3000);
  } });
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
            try { await api.del("/api/system/wifi/" + encodeURIComponent(w.ssid)); toast("ok", "Network removed"); loadWifi(); } catch (e) { toast("error", errText(e)); }
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
      h(".card", null, h("h2", null, "Device"), sysKv),
    ),
    h(".card", null,
      h(".card-head", null, h("h2", null, "Logs"), unitSel, linesSel, autoSw, h("button.btn.sm", { type: "button", onclick: () => loadLogs() }, icon("refresh"), "Refresh")),
      logEl),
    h(".card", null, h(".card-head", null, h("h2", null, "Wi‑Fi networks"), h("button.btn.sm", { type: "button", onclick: loadWifi }, icon("refresh"))),
      h(".tbl-wrap", null, h("table", null, h("thead", null, h("tr", null, h("th", null, "Network"), h("th.num", null, "Signal"), h("th", null, "Priority"), h("th"))), wifiBody)),
      h("h3", { style: { marginTop: "14px" } }, "Add a network"), wifiForm,
      h(".hint", null, "The device connects to the available network with the highest priority.")),
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
  return () => { stopped = true; stopStatus(); clearInterval(logTimer); };
}
