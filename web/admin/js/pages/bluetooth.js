// Bluetooth: the built-in adapter, scanning, pairing and the speaker the radio plays through
// (docs/contracts/radio.md). State is polled while the page is open (faster during a scan or an action).
import { h, icon, clear } from "../dom.js";
import { api, errText } from "../api.js";
import { store, on, applySettings } from "../store.js";
import { toast, banner, badge, switchEl, confirmDialog } from "../ui.js";
import { rssiBars, btIconName, btName, sortDevices, splitDevices } from "../radio-fmt.js";

const SCAN_S = 20;

function rssiEl(rssi) {
  const n = rssiBars(rssi);
  const el = h(".rssi" + (n === null ? ".none" : ""), { title: n === null ? "Not seen in the last scan" : `${rssi} dBm` });
  for (let i = 1; i <= 4; i++) el.append(h("i", { class: n !== null && i <= n ? "on" : null }));
  return el;
}

export function mount(root) {
  let stopped = false;
  let bt = null; // BtState
  let timer = null;
  let scanEnd = 0;
  let showFolded = false;
  const busy = new Map(); // mac → {what, since}
  const rowErr = new Map(); // mac → text

  const unavail = h("div", { hidden: true });
  const powerSw = switchEl({ checked: false, label: "Bluetooth on", onchange: (v) => setPower(v) });
  const scanBtn = h("button.btn.primary", { type: "button", onclick: () => scan() }, icon("search"), "Scan");
  const listBody = h(".bt-list");
  const spkBox = h(".bt-spk");
  const adapterKv = h(".kv");

  // ── state ──
  async function load() {
    try {
      const s = await api.get("/api/bluetooth/state");
      if (stopped) return;
      bt = s;
      render();
    } catch (e) {
      if (stopped) return;
      bt = null;
      clear(unavail).append(banner("bad", e.status === 404
        ? "Bluetooth is not available on this device yet — update the device software."
        : "Bluetooth: " + errText(e)));
      unavail.hidden = false;
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (stopped) return;
    const fast = busy.size || (bt && bt.discovering) || Date.now() < scanEnd;
    timer = setTimeout(async () => { if (!document.hidden) await load(); schedule(); }, fast ? 1500 : 5000);
  }

  // ── actions ──
  async function setPower(on) {
    try { bt = await api.post("/api/bluetooth/power", { on }); toast("ok", on ? "Bluetooth is on" : "Bluetooth is off"); }
    catch (e) { toast("error", errText(e)); }
    render();
  }

  async function scan() {
    try {
      bt = await api.post("/api/bluetooth/scan", { seconds: SCAN_S });
      scanEnd = Date.now() + SCAN_S * 1000;
      render();
      schedule();
    } catch (e) { toast("error", "Scan: " + errText(e)); }
  }

  async function act(dev, what) {
    const mac = dev.address;
    if (busy.has(mac)) return;
    if (what === "forget") {
      const ok = await confirmDialog({
        title: "Forget this device?",
        text: h("div", null, "“", h("b", null, btName(dev)), "” will be unpaired. To use it again, put it in pairing mode and connect it here."),
        okText: "Forget", danger: true,
      });
      if (!ok) return;
    }
    const label = what === "connect" ? (dev.paired ? "Connecting…" : "Pairing…") : what === "disconnect" ? "Disconnecting…" : "Removing…";
    busy.set(mac, { what: label, since: Date.now() });
    rowErr.delete(mac);
    render();
    schedule();
    const path = "/api/bluetooth/devices/" + encodeURIComponent(mac);
    try {
      const r = what === "forget" ? await api.del(path) : await api.post(`${path}/${what}`, {});
      if (r && Array.isArray(r.devices)) bt = r;
      if (what === "connect") toast("ok", `${btName(dev)} connected` + (dev.audio ? " — the radio plays through it now" : ""));
      else if (what === "forget") toast("ok", `${btName(dev)} forgotten`);
    } catch (e) {
      const msg = errText(e);
      rowErr.set(mac, msg);
      toast("error", `${btName(dev)}: ${msg}`);
    } finally {
      busy.delete(mac);
      if (!stopped) { await load(); schedule(); }
    }
  }

  // ── render ──
  function deviceRow(d) {
    const b = busy.get(d.address);
    const isSpk = bt && bt.audio_device === d.address;
    const badges = [];
    if (d.connected) badges.push(badge("connected", "ok"));
    if (isSpk) badges.push(badge("radio speaker", "warn"));
    if (d.paired && !d.connected) badges.push(badge("paired"));
    if (!d.paired) badges.push(badge("new"));
    const acts = [];
    if (b) {
      acts.push(h("span.bt-busy", null, h("span.spin"), " ", b.what, " ", h("span.mono.dim", null, Math.round((Date.now() - b.since) / 1000) + " s")));
    } else {
      if (d.connected) acts.push(h("button.btn.sm", { type: "button", onclick: () => act(d, "disconnect") }, "Disconnect"));
      else acts.push(h("button.btn.sm" + (d.audio ? ".primary" : ""), { type: "button", disabled: !(bt && bt.powered), onclick: () => act(d, "connect") }, d.paired ? "Connect" : "Pair & connect"));
      if (d.paired) acts.push(h("button.btn.sm.icon.danger", { type: "button", title: "Forget", onclick: () => act(d, "forget") }, icon("trash")));
    }
    const err = rowErr.get(d.address);
    const hintTxt = b && b.what === "Pairing…" ? "Put the speaker in pairing mode (usually: hold the Bluetooth button until it blinks)." : null;
    return h(".bt-row" + (d.connected ? ".conn" : "") + (isSpk ? ".spk" : ""), null,
      h("span.ic", null, icon(btIconName(d))),
      h(".who", null,
        h(".nm", null, btName(d)),
        h(".addr", null, d.address),
        h(".badges", null, badges)),
      rssiEl(d.rssi),
      h(".acts", null, acts),
      err ? h(".msg.red", null, err) : hintTxt ? h(".msg.amber", null, hintTxt) : null,
    );
  }

  function render() {
    if (!bt) return;
    unavail.hidden = bt.available !== false;
    if (bt.available === false) {
      clear(unavail).append(banner("bad", h("div", null,
        h("b", null, "Bluetooth adapter not available. "),
        "The device does not see its Bluetooth controller: check that Bluetooth is enabled in the boot configuration and that bluetoothd is running.")));
    }
    powerSw.input.checked = !!bt.powered;
    powerSw.input.disabled = bt.available === false;

    // scan button with a countdown
    const left = Math.max(0, Math.ceil((scanEnd - Date.now()) / 1000));
    const scanning = bt.discovering || left > 0;
    clear(scanBtn).append(icon("search"), scanning ? `Scanning… ${left ? left + " s" : ""}` : "Scan");
    scanBtn.disabled = scanning || !bt.powered || bt.available === false;

    // adapter
    clear(adapterKv).append(
      h("span.k", null, "Adapter"), h("span.v", null, bt.available === false ? h("span.red", null, "not found") : bt.powered ? h("span.green", null, "on") : h("span.amber", null, "off")),
      h("span.k", null, "Scanning"), h("span.v", null, bt.discovering ? "yes" : "no"),
      h("span.k", null, "Known devices"), h("span.v", null, String((bt.devices || []).filter((d) => d.paired).length)),
    );

    // radio speaker
    clear(spkBox);
    const spk = (bt.devices || []).find((d) => d.address === bt.audio_device);
    if (spk) {
      spkBox.append(h(".bt-spk-row", null,
        h("span.ic" + (spk.connected ? ".green" : ".dim"), null, icon("speaker", 34)),
        h("div", null,
          h(".nm", null, btName(spk)),
          h(".dim", null, spk.connected ? "Connected — the radio plays here" : "Not connected — turn the speaker on and bring it closer; it reconnects by itself"),
        ),
        busy.has(spk.address) ? null : spk.connected
          ? h("button.btn.sm", { type: "button", onclick: () => act(spk, "disconnect") }, "Disconnect")
          : h("button.btn.sm.primary", { type: "button", disabled: !bt.powered, onclick: () => act(spk, "connect") }, "Connect"),
      ));
    } else {
      spkBox.append(h(".dim", null, "No speaker selected yet. Turn your Bluetooth speaker on, put it in pairing mode, press ", h("b", null, "Scan"), " and connect it below — it becomes the radio speaker."));
    }

    // devices
    clear(listBody);
    const { shown, folded } = splitDevices(sortDevices(bt.devices, bt.audio_device));
    if (!shown.length && !folded.length) {
      listBody.append(h(".bt-empty.dim", null, bt.powered ? "No devices yet — press Scan." : "Bluetooth is off."));
    }
    for (const d of shown) listBody.append(deviceRow(d));
    if (folded.length) {
      listBody.append(h("button.bt-fold", { type: "button", onclick: () => { showFolded = !showFolded; render(); } },
        showFolded ? "Hide" : "Show", ` ${folded.length} unnamed device${folded.length === 1 ? "" : "s"}`));
      if (showFolded) for (const d of folded) listBody.append(deviceRow(d));
    }
  }

  // ── settings (immediate) ──
  const setBox = h(".bt-set");
  function renderSettings(s) {
    clear(setBox);
    const add = (sec, key, label, hint) => {
      if (!s || !s[sec] || s[sec][key] === undefined) return;
      setBox.append(h(".bt-set-row", null, switchEl({ checked: !!s[sec][key], label, onchange: async (v) => {
        try { applySettings(await api.patch("/api/settings", { [sec]: { [key]: v } })); toast("ok", "Saved"); }
        catch (e) { toast("error", errText(e)); renderSettings(store.settings); }
      } }), hint ? h(".hint", null, hint) : null));
    };
    add("bluetooth", "auto_connect", "Reconnect the radio speaker automatically", "Every 30 s while it is not connected and Bluetooth is on");
    add("bluetooth", "enabled", "Turn Bluetooth on at startup", null);
  }
  const offSet = on("settings", renderSettings);
  renderSettings(store.settings);

  root.append(
    h(".page-head", null, h("h1", null, "Bluetooth"), powerSw),
    unavail,
    h(".grid.g2", null,
      h(".card", null, h("h2", null, "Radio speaker"), spkBox, setBox),
      h(".card", null, h("h2", null, "Adapter"), adapterKv,
        h(".hint", { style: { marginTop: "10px" } }, "Audio goes to one speaker at a time (A2DP). Connecting another speaker makes it the radio speaker.")),
    ),
    h(".card", null,
      h(".card-head", null, h("h2", null, "Devices"), scanBtn, h("button.btn.sm", { type: "button", title: "Refresh", onclick: () => load() }, icon("refresh"))),
      listBody),
  );

  // seconds counters (busy, scan) without waiting for the next poll
  const tick = setInterval(() => { if (busy.size || Date.now() < scanEnd + 1000) render(); }, 1000);
  load().then(schedule);

  return () => {
    stopped = true;
    clearTimeout(timer);
    clearInterval(tick);
    offSet();
  };
}
