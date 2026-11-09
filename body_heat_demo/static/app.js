/* Body Heat front end. Shows what the hub validated and the pod decided; never
   computes a temperature decision. Live updates arrive by SSE as card diffs. */
(() => {
  const $ = (s) => document.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const api = async (path, body) => {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw Object.assign(new Error(j.detail || j.error || r.statusText), { status: r.status, body: j });
    return j;
  };
  const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const ago = (s) => s == null ? "no data yet" : s < 2 ? "just now" : s < 60 ? `${s} s ago` : `${Math.floor(s / 60)} min ago`;

  let me = null, S = {}, STATES = {}, tiles = new Map(), es = null, lastMsgAt = 0, screen = null, detailDev = null, detailTimer = null;

  // ---------- theme (light for sun, dark for evening) ----------
  const theme = () => document.documentElement.dataset.theme || "light";
  function setTheme(t) { document.documentElement.dataset.theme = t; try { localStorage.setItem("bh-theme", t); } catch (e) {} $("#theme-btn").textContent = t === "dark" ? "☾ Display" : "☼ Display"; }
  try { setTheme(localStorage.getItem("bh-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")); } catch (e) { setTheme("light"); }
  $("#theme-btn").onclick = () => setTheme(theme() === "dark" ? "light" : "dark");

  function toast(msg, ms = 3500) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms); }

  // ---------- auth ----------
  async function boot() {
    const j = await api("/api/me");
    S = j.strings; j.states.forEach(s => STATES[s.key] = s);
    $("#intended-use").textContent = S.intended_use;
    renderGuide();
    if (j.user) { me = j.user; enter(); return; }
    const ul = $("#account-list"); ul.innerHTML = "";
    j.accounts.filter(a => a.role === "staff" || ["athlete11", "athlete07", "athlete33"].includes(a.username))
      .forEach(a => { const li = el("li"); li.innerHTML = `<code>${a.username}</code> ${a.display}`; ul.appendChild(li); });
  }
  $("#login-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    try { me = (await api("/api/login", { username: f.get("username"), password: f.get("password") })).user; enter(); }
    catch (e) { $("#login-err").textContent = "That username or password did not match. Try again."; }
  });
  $("#signout").onclick = async () => { await api("/api/logout", {}); location.reload(); };

  function enter() {
    $("#screen-login").classList.add("hidden"); $("#hdr").classList.remove("hidden");
    $("#who").textContent = me.display;
    const nav = $("#nav"); nav.innerHTML = "";
    const screens = me.role === "staff" ? [["roster", S.live], ["session", S.setup], ["demo", S.presenter]] : [["athlete", S.my_session]];
    screens.forEach(([id, label]) => { const b = el("button", "", label); b.dataset.screen = id; b.onclick = () => show(id); nav.appendChild(b); });
    show(screens[0][0]);
    connect();
    window.BH = { show, openDetail, closeDetail, api, toast, me: () => me };
    const startTour = () => { $("#welcome").classList.add("hidden"); if (window.BHTour) window.BHTour.start(window.BH, me.role); };
    $("#tour-btn").onclick = startTour;
    $("#welcome-tour").onclick = startTour;
    $("#welcome-skip").onclick = () => { $("#welcome").classList.add("hidden"); try { localStorage.setItem("bh-toured", "1"); } catch (e) {} };
    let toured = false; try { toured = !!localStorage.getItem("bh-toured"); } catch (e) {}
    if (!toured) setTimeout(() => $("#welcome").classList.remove("hidden"), 600);
  }
  function show(id) {
    screen = id;
    document.querySelectorAll(".screen").forEach(s => s.classList.toggle("hidden", s.id !== `screen-${id}`));
    document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.screen === id));
    if (id === "session") loadSession();
    if (id === "demo") loadDemo();
    if (id === "athlete") loadAthlete();
  }

  // ---------- guide ----------
  function renderGuide() {
    const t = $("#guide-table"); t.innerHTML = "<tr><th></th><th>What the card says</th><th>What it means</th><th>What to do</th></tr>";
    Object.values(STATES).forEach(s => {
      const r = el("tr", s.key);
      r.appendChild(el("td", "ico", s.icon)); r.appendChild(el("td", "lab", s.label)); r.appendChild(el("td", "", s.meaning)); r.appendChild(el("td", "todo", s.staff));
      t.appendChild(r);
    });
  }
  const openGuide = () => $("#guide").classList.remove("hidden");
  $("#guide-btn").onclick = openGuide;
  $("#guide-close").onclick = () => $("#guide").classList.add("hidden");
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("#guide").classList.add("hidden"); closeDetail(); } });

  // ---------- live feed ----------
  function connect() {
    if (es) es.close();
    es = new EventSource("/api/events");
    es.onmessage = (ev) => {
      lastMsgAt = Date.now(); setLost(false);
      const m = JSON.parse(ev.data);
      $("#clock").textContent = mmss(m.t);
      if (m.full) tiles.clear();
      (m.tiles || []).forEach(t => tiles.set(t.device_id, t));
      if (m.tiles && me.role === "staff") { renderGrid(); renderSummary(); }
      if (m.strip) renderAttention(m.strip);
      if (m.notices) renderNotices(m.notices);
      if (m.status) renderStatus(m.status);
      if (me.role === "athlete" && m.tiles) renderAthleteStatus();
    };
  }
  setInterval(() => { if (me && lastMsgAt && Date.now() - lastMsgAt > 5000) setLost(true); }, 1000);
  function setLost(lost) {
    const was = document.body.classList.contains("lost");
    document.body.classList.toggle("lost", lost);
    const b = $("#banner");
    if (lost) { b.textContent = S.connection_lost; b.className = "banner lost"; if (!was) setTimeout(() => { if (document.body.classList.contains("lost")) connect(); }, 3000); }
    else if (!b.classList.contains("fault")) b.classList.add("hidden");
  }
  function renderStatus(s) {
    const b = $("#banner");
    if (s.storage_fault && !document.body.classList.contains("lost")) { b.textContent = `${S.storage_fault}: ${s.storage_fault}`; b.className = "banner fault"; }
    else if (!document.body.classList.contains("lost")) { b.className = "banner hidden"; }
    window.__status = s;
    if (screen === "demo") renderCounters(s);
  }

  // ---------- summary: one glance ----------
  function renderSummary() {
    const counts = {};
    tiles.forEach(t => counts[t.state] = (counts[t.state] || 0) + 1);
    const need = (counts.absolute || 0) + (counts.trend || 0);
    const check = (counts.no_signal || 0) + (counts.detached || 0) + (counts.low_battery || 0);
    const sum = $("#summary"); sum.innerHTML = "";
    const head = el("span", need ? "need" : "allok", need ? `${need} ${need === 1 ? S.needs_attention : S.need_attention}` : check ? `No alerts · ${check} pod${check > 1 ? "s" : ""} to check` : `✓ ${S.all_ok}`);
    sum.appendChild(head);
    ["absolute", "trend", "no_signal", "detached", "low_battery", "ok", "not_collecting"].forEach(k => {
      if (!counts[k]) return;
      const p = el("span", `pill ${k}`); p.appendChild(el("span", "", STATES[k].icon)); p.appendChild(el("span", "", `${counts[k]} ${k === "ok" ? "OK" : STATES[k].label}`)); sum.appendChild(p);
    });
  }

  // ---------- cards ----------
  function tileNode(t, big) {
    const d = el(big ? "div" : "button", `tile ${t.state}${t.greyed ? " greyed" : ""}${big ? " big" : ""}`);
    d.dataset.dev = t.device_id; d.setAttribute("role", "listitem");
    const top = el("div", "top"); top.appendChild(el("span", "jersey", `#${t.jersey}`)); top.appendChild(el("span", "name", t.name)); d.appendChild(top);
    const temp = el("div", "temp");
    if (t.skin_c == null) { temp.classList.add("hidden-temp"); temp.textContent = t.alerts_only ? "Alerts only" : "No reading"; }
    else { temp.textContent = `${t.skin_c.toFixed(1)}°`; temp.appendChild(el("span", "arrow", t.arrow)); }
    d.appendChild(temp);
    if (t.hub_analysis) d.appendChild(el("b", "pod-source", "Pod-reported status"));
    const lab = el("div", "label"); lab.appendChild(el("span", "ico", t.icon)); lab.appendChild(el("span", "", t.label)); d.appendChild(lab);
    if (t.hub_analysis) {
      const a = t.hub_analysis, box = el("div", "hub-analysis");
      box.appendChild(el("b", "", a.label));
      if (a.flag) box.appendChild(el("div", "", `Latest flag (t=${a.flag.t}): ${a.flag.kind}: ${a.flag.message}`));
      else box.appendChild(el("div", "", a.status));
      if (a.notice) box.appendChild(el("div", "analysis-notice", `Latest notice (t=${a.notice.t}): ${a.notice.kind}: ${a.notice.message}`));
      d.appendChild(box);
    }
    const foot = el("div", "foot");
    const badges = el("div", "badges"); t.badge_labels.forEach(b => badges.appendChild(el("span", "badge", b))); foot.appendChild(badges);
    foot.appendChild(el("span", "age mono", t.state === "no_signal" ? `last ${ago(t.age_s)}` : ago(t.measurement_age_s ?? t.age_s))); d.appendChild(foot);
    d.title = `${t.name}: ${t.label}. ${STATES[t.state].staff}`;
    if (!big) d.onclick = () => openDetail(t.device_id);
    return d;
  }
  function renderGrid() {
    const g = $("#grid");
    const order = [...tiles.values()].sort((a, b) => a.jersey - b.jersey);
    if (g.childElementCount !== order.length) { g.innerHTML = ""; order.forEach(t => g.appendChild(tileNode(t))); return; }
    order.forEach((t, i) => { const old = g.children[i]; const n = tileNode(t); if (old.outerHTML !== n.outerHTML) g.replaceChild(n, old); });
  }
  function renderAttention(rows) {
    const a = $("#attention"); a.innerHTML = "";
    rows.forEach(r => {
      const d = el("div", `alert-row ${r.state}${r.acknowledged ? " acked" : ""}`);
      d.appendChild(el("span", "ico", r.icon));
      const ww = el("div", "who-what");
      ww.appendChild(el("span", "what", `#${r.jersey} ${r.name} — ${r.label}`));
      ww.appendChild(el("span", "todo", r.acknowledged ? `Noted by ${r.ack_by} at ${mmss(r.ack_t)}. Stays until the pod clears it.` : STATES[r.state].staff));
      d.appendChild(ww);
      d.appendChild(el("span", "since", `for ${mmss(r.since_s)}`));
      if (!r.acknowledged) { const b = el("button", "btn", `✓ ${S.got_it}`); b.title = S.ack_note; b.onclick = (e) => { e.stopPropagation(); api("/api/ack", { device_id: r.device_id }); toast(`Noted. #${r.jersey} stays listed until the pod clears it.`); }; d.appendChild(b); }
      else d.appendChild(el("span"));
      d.onclick = () => openDetail(r.device_id);
      a.appendChild(d);
    });
    const h = rows.length ? Math.min(128, rows.length * 66) + 6 : 0;
    $("#screen-roster").style.setProperty("--att-h", `${h}px`);
  }
  function renderNotices(ns) {
    const n = $("#notices"); n.innerHTML = "";
    ns.forEach(x => n.appendChild(el("span", `notice ${x.kind}`, `#${x.jersey} ${x.name}: ${x.text}`)));
  }

  // ---------- person detail ----------
  async function openDetail(dev) {
    detailDev = dev; $("#modal").classList.remove("hidden");
    await refreshDetail();
    clearInterval(detailTimer); detailTimer = setInterval(refreshDetail, 2000);
    $("#detail-close").focus();
  }
  function closeDetail() { $("#modal").classList.add("hidden"); detailDev = null; clearInterval(detailTimer); }
  $("#detail-close").onclick = closeDetail;
  $("#modal").onclick = (e) => { if (e.target.id === "modal") closeDetail(); };
  async function refreshDetail() {
    if (!detailDev) return;
    const d = await api(`/api/wearer/${detailDev}`);
    $("#detail-title").textContent = `#${d.tile.jersey} ${d.tile.name}`;
    const tt = $("#detail-tile"); tt.innerHTML = ""; tt.appendChild(tileNode(d.tile, true));
    const g = $("#detail-guidance"); g.innerHTML = ""; g.appendChild(el("b", "", "What to do")); g.appendChild(el("span", "", STATES[d.tile.state].staff));
    const acts = $("#detail-actions"); acts.innerHTML = "";
    if (me.role === "staff") {
      if (["absolute", "trend"].includes(d.tile.state) && !d.tile.badges.includes("acknowledged")) {
        const b = el("button", "btn primary", `✓ ${S.got_it}`); b.title = S.ack_note; b.onclick = async () => { await api("/api/ack", { device_id: detailDev }); refreshDetail(); }; acts.appendChild(b);
        acts.appendChild(el("span", "tiny muted", S.ack_note));
      }
      acts.appendChild(switchBtn(`Alerts only (hide temperature on the live screen)`, d.alerts_only, async (on) => { await api("/api/alerts_only", { device_id: detailDev, on }); refreshDetail(); }));
      acts.appendChild(switchBtn(d.consented ? "Consent recorded — recording this pod" : "No consent — not recording", d.consented, async (on) => { await api("/api/consent", { device_id: detailDev, consented: on }); refreshDetail(); if (!on) toast(S.consent_withdrawn_prompt, 6000); }));
    }
    const ev = $("#detail-events"); ev.innerHTML = d.events.length ? "" : '<div class="muted">Nothing yet</div>';
    d.events.forEach(e => { const r = el("div"); r.appendChild(el("span", "t mono", mmss(e.t))); r.appendChild(el("span", "k", friendlyKind(e))); r.appendChild(el("span", "", `${friendlyDetail(e)}${e.user ? " · " + e.user : ""}`)); ev.appendChild(r); });
    $("#detail-chart").innerHTML = ""; $("#detail-chart").appendChild(chart(d, 820, 460));
  }
  const friendlyKind = (e) => ({ alert: "Alert", alert_clear: "Cleared", ack: "Noted", no_signal: "Quiet", signal_back: "Back", contact: "Contact", consent: "Consent", battery: "Battery" })[e.kind] || e.kind;
  const friendlyDetail = (e) => e.kind === "alert" ? STATES[e.detail]?.label || e.detail : e.detail || "";
  function switchBtn(label, on, onChange) {
    const b = el("button", `switch${on ? " on" : ""}`); b.setAttribute("role", "switch"); b.setAttribute("aria-checked", String(!!on));
    b.appendChild(el("i")); b.appendChild(el("span", "", label)); b.onclick = () => onChange(!on); return b;
  }

  // ---------- chart (bundled, no CDN) ----------
  function chart(d, W, H) {
    const ns = "http://www.w3.org/2000/svg", mk = (tag, attrs) => { const e = document.createElementNS(ns, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
    const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": "Session chart" });
    const s = d.series, skin = s.skin_c || [], air = s.air_c || [], hum = s.humidity_pct || [], mot = s.motion || [];
    const all = [...skin, ...air, ...hum, ...mot];
    const wrap = el("div"); wrap.appendChild(svg);
    if (!all.length) { const t = mk("text", { x: 20, y: 30 }); t.textContent = "No stored readings yet"; svg.appendChild(t); return wrap; }
    const x0 = Math.min(...all.map(p => p[0])), x1 = Math.max(d.t, ...all.map(p => p[0]));
    const L = 50, R = 14, panels = [
      { title: "Skin and air temperature (°C)", h: 0.55, series: [["skin", skin], ["air", air]], limit: 38.5 },
      { title: "Humidity (%RH)", h: 0.2, series: [["hum", hum]] },
      { title: "Movement", h: 0.25, series: [["mot", mot]] },
    ];
    const X = (t) => L + (t - x0) / Math.max(1, x1 - x0) * (W - L - R);
    let top = 10;
    panels.forEach(p => {
      const ph = (H - 40) * p.h, bottom = top + ph - 18;
      const vals = p.series.flatMap(([, pts]) => pts.map(q => q[1])).concat(p.limit ? [p.limit] : []);
      let lo = Math.min(...vals), hi = Math.max(...vals); if (hi - lo < 0.5) { hi += 0.25; lo -= 0.25; } const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const Y = (v) => bottom - (v - lo) / (hi - lo) * (bottom - top - 14);
      svg.appendChild(mk("line", { x1: L, x2: W - R, y1: bottom, y2: bottom, class: "axis" }));
      svg.appendChild(mk("line", { x1: L, x2: L, y1: top + 14, y2: bottom, class: "axis" }));
      const title = mk("text", { x: L + 4, y: top + 10 }); title.textContent = p.title; svg.appendChild(title);
      [lo + pad, hi - pad].forEach(v => { const t = mk("text", { x: 4, y: Y(v) + 4 }); t.textContent = v.toFixed(1); svg.appendChild(t); });
      if (p.limit) { svg.appendChild(mk("line", { x1: L, x2: W - R, y1: Y(p.limit), y2: Y(p.limit), class: "limit" })); const t = mk("text", { x: W - R - 150, y: Y(p.limit) - 4 }); t.textContent = "team setting 38.5"; svg.appendChild(t); }
      p.series.forEach(([cls, pts]) => { if (pts.length) svg.appendChild(mk("path", { class: cls, d: pts.map((q, i) => `${i ? "L" : "M"}${X(q[0]).toFixed(1)},${Y(q[1]).toFixed(1)}`).join(" ") })); });
      d.events.forEach(e => {
        const cls = e.kind === "alert" ? (e.detail === "absolute" ? "mark-absolute" : "mark-alert") : e.kind === "alert_clear" ? "mark-clear" : e.kind === "ack" ? "mark-ack" : e.kind === "no_signal" ? "mark-no_signal" : null;
        if (cls && e.t >= x0) svg.appendChild(mk("line", { x1: X(e.t), x2: X(e.t), y1: top + 14, y2: bottom, class: cls }));
      });
      top += ph;
    });
    [x0, x1].forEach(t => { const tx = mk("text", { x: X(t) - (t === x1 ? 40 : 0), y: H - 6 }); tx.textContent = t < 0 ? `${mmss(-t)} before` : mmss(t); svg.appendChild(tx); });
    const lg = el("div", "legend"); lg.innerHTML = `<span><i style="border-color:var(--accent)"></i>skin</span><span><i style="border-color:#2a9d8f"></i>air</span><span><i style="border-color:var(--det)"></i>humidity</span><span><i style="border-color:var(--ok)"></i>movement</span><span><i style="border-color:var(--trend)"></i>rising alert</span><span><i style="border-color:var(--abs)"></i>above setting</span><span><i style="border-color:var(--ok);border-style:dashed"></i>pod cleared</span><span><i style="border-color:var(--fg);border-style:dotted"></i>noted by staff</span>`;
    wrap.appendChild(lg);
    return wrap;
  }

  // ---------- setup screen ----------
  async function loadSession() {
    const s = await api("/api/session");
    const pods = s.wearers.filter(w => w.kind === "pod").length;
    $("#session-info").innerHTML = `<table class="kv"><tr><td>Session</td><td>${s.session_id}</td></tr><tr><td>Started</td><td>${s.started.replace("T", " ")}</td></tr><tr><td>People</td><td>${s.wearers.length} (${pods} pods, ${s.wearers.length - pods} emulated for the demo)</td></tr><tr><td>Running for</td><td class="mono">${mmss(s.t)}</td></tr></table>`;
    const names = { trend_limit_c_per_5min: "Rising limit (°C per 5 min)", trend_window_s: "Rising window (s, shortened for the demo)", absolute_limit_c: "Temperature limit (°C)", recovery_period_s: "Show \"back below\" notice for (s)", low_battery_pct: "Low battery below (%)", no_signal_after_s: "No signal after (s)" };
    $("#settings").innerHTML = Object.entries(s.settings).map(([k, v]) => `<tr><td>${names[k] || k}</td><td class="mono">${v}</td></tr>`).join("");
    const w = $("#wearers");
    w.innerHTML = `<tr><th>#</th><th>Name</th><th>Pod</th><th>Recording (consent)</th><th>Alerts only</th></tr>`;
    s.wearers.forEach(x => {
      const tr = el("tr");
      tr.innerHTML = `<td><b>${x.jersey}</b></td><td>${x.full_name}</td><td><span class="pill ${x.kind}">${x.device_id} · ${x.kind}</span></td>`;
      const c = el("td"); c.appendChild(switchBtn(x.consented ? "On" : "Off", x.consented, async (on) => { await api("/api/consent", { device_id: x.device_id, consented: on }); if (!on) toast(S.consent_withdrawn_prompt, 6000); loadSession(); })); tr.appendChild(c);
      const a = el("td"); a.appendChild(switchBtn(x.alerts_only ? "On" : "Off", x.alerts_only, async (on) => { await api("/api/alerts_only", { device_id: x.device_id, on }); loadSession(); })); tr.appendChild(a);
      w.appendChild(tr);
    });
    const log = $("#event-log"); log.innerHTML = "";
    s.events.forEach(e => { const r = el("div"); r.appendChild(el("span", "t mono", mmss(Math.max(0, e.t)))); r.appendChild(el("span", "k", friendlyKind(e))); r.appendChild(el("span", "", `${e.device_id ? e.device_id + " " : ""}${friendlyDetail(e)}${e.user ? " · " + e.user : ""}`)); log.appendChild(r); });
    const den = $("#denied-log"); den.innerHTML = s.denied.length ? "" : '<div class="muted">None so far</div>';
    s.denied.forEach(([t, u, what]) => { const r = el("div"); r.appendChild(el("span", "t mono", mmss(t))); r.appendChild(el("span", "k", u)); r.appendChild(el("span", "", what)); den.appendChild(r); });
    $("#autoplay-btn").textContent = `Scripted scenario: ${s.autoplay ? "on" : "off"}`;
  }

  // ---------- presenter tools ----------
  async function loadDemo() {
    const s = await api("/api/session");
    const sel = $("#demo-jersey");
    if (!sel.childElementCount) s.wearers.forEach(w => { const o = el("option", "", `#${w.jersey} ${w.full_name}`); o.value = w.jersey; sel.appendChild(o); });
    $("#autoplay-btn").textContent = `Scripted scenario: ${s.autoplay ? "on" : "off"}`;
    const l = $("#demo-log"); l.innerHTML = "";
    s.demo_log.forEach(x => { const r = el("div"); r.appendChild(el("span", "t mono", mmss(x.t))); r.appendChild(el("span", "k", x.jersey ? `#${x.jersey}` : "")); r.appendChild(el("span", "", `${x.action}${x.user ? " · " + x.user : ""}`)); l.appendChild(r); });
    if (window.__status) renderCounters(window.__status);
  }
  document.querySelectorAll("[data-ctl]").forEach(b => b.onclick = async () => {
    await api(`/api/demo/${b.dataset.ctl}`, { jersey: Number($("#demo-jersey").value) });
    toast(`${b.textContent} → sent`); setTimeout(loadDemo, 300);
  });
  function renderCounters(s) {
    const rej = Object.entries(s.rejections).map(([k, v]) => `<tr><td>Rejected: ${k.replace("_", " ")}</td><td class="mono">${v}</td></tr>`).join("");
    $("#counters").innerHTML = `<tr><td>Accepted packets</td><td class="mono">${s.accepted}</td></tr>${rej}<tr><td>Stored measurements</td><td class="mono">${s.stored_measurements}</td></tr><tr><td>Repeated IDs ignored</td><td class="mono">${s.ignored_duplicates}</td></tr><tr><td>Storage</td><td>${s.storage_fault ? s.storage_fault : "OK"}</td></tr>`;
  }

  // ---------- athlete / wearer view ----------
  async function loadAthlete() { $("#athlete-hello").textContent = `Hi ${me.display.split(" ")[0]}`; renderAthleteStatus(); await renderAthleteChart(); }
  function renderAthleteStatus() {
    const t = tiles.get(me.device_id); if (!t) return;
    const c = $("#athlete-status"); c.className = `status-card ${t.state}`; c.innerHTML = "";
    c.appendChild(el("div", "ico", t.icon));
    c.appendChild(el("div", "headline", t.label));
    c.appendChild(el("div", "detail", STATES[t.state].wearer));
    c.appendChild(el("div", "temp", t.skin_c == null ? "" : `Skin ${t.skin_c.toFixed(1)} °C ${t.arrow} · ${ago(t.measurement_age_s ?? t.age_s)}`));
  }
  async function renderAthleteChart() {
    const d = await api(`/api/wearer/${me.device_id}`);
    $("#athlete-chart").innerHTML = ""; $("#athlete-chart").appendChild(chart(d, 760, 400));
    const c = $("#athlete-consent"); c.innerHTML = "";
    c.appendChild(el("p", "", d.consented ? "You opted in. Your pod's readings are recorded for this session and shown only to you and team staff." : "You have not opted in, so nothing is recorded from your pod."));
    if (d.consented) { const b = el("button", "btn danger", S.withdraw); b.onclick = async () => { await api("/api/consent", { device_id: me.device_id, consented: false }); toast("Done. The hub stopped recording your pod. Staff have been asked to switch it off."); renderAthleteChart(); }; c.appendChild(b); }
  }
  $("#try-peer").onclick = async () => {
    try { await api("/api/wearer/pod-07"); $("#peer-result").textContent = "Unexpected: allowed"; }
    catch (e) { $("#peer-result").textContent = `The hub said no (${e.status}: ${e.message}) and wrote it in the log.`; }
  };
  setInterval(() => { if (screen === "athlete") renderAthleteChart(); }, 5000);

  boot();
})();
