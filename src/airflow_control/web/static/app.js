// airflow_control GUI: one card per MFC, continuous + puff side by side, live plot.
"use strict";

const WINDOW_S = 30;
const DUR_STEPS = ["0.05", "0.1", "0.5", "1"]; // s
let shuttingDown = false;
const cards = new Map(); // name -> { root, el, hist: [{t, flow, setpoint}], lastT }

const $ = (root, sel) => root.querySelector(sel);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
// Flow steps scale with the device's full scale: FS 2 -> 0.01 … 0.5, FS 100 -> 1 … 50.
const clean = (x) => String(Number(x.toPrecision(3)));
const flowSteps = (fs) => {
  const scale = 10 ** Math.floor(Math.log10(fs));
  return [1, 5, 10, 20, 50].map((k) => clean((k * scale) / 100));
};
// "nice" grid spacing giving about 4 intervals up to max
const niceStep = (max) => {
  const raw = max / 4, mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw);
};
const fmt = (v, d = 3) => (v == null || Number.isNaN(v) ? "–" : Number(v).toFixed(d));

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (e) { return null; }
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

// --- cards ------------------------------------------------------------------

function buildCard(ch) {
  const root = document.getElementById("channel-tpl").content.firstElementChild.cloneNode(true);
  const el = {
    label: $(root, ".ch-label"), meta: $(root, ".ch-meta"),
    flow: $(root, ".ch-flow"), set: $(root, ".ch-set"), valve: $(root, ".ch-valve"),
    error: $(root, ".ch-error"),
    contForm: $(root, ".ctl-cont"), range: $(root, ".cont-range"), contVal: $(root, ".cont-val"),
    contOff: $(root, ".cont-off"), contState: $(root, ".cont-state"),
    puffForm: $(root, ".ctl-puff"), puffVal: $(root, ".puff-val"), puffDur: $(root, ".puff-dur"),
    contStep: $(root, ".cont-step"), puffStep: $(root, ".puff-step"), durStep: $(root, ".dur-step"),
    puffState: $(root, ".puff-state"), bar: $(root, ".progress-bar"),
    canvas: $(root, "canvas"), tooltip: $(root, ".tooltip"),
  };
  const card = { root, el, hist: [], lastT: 0, ch, hover: null, localError: null };
  const key = (k) => `airflow-${ch.name}-${k}`;

  for (const input of [el.range, el.contVal, el.puffVal]) input.max = ch.cap;
  for (const span of root.querySelectorAll(".unit")) span.textContent = ch.info.unit;
  const steps = flowSteps(ch.info.full_scale);
  el.puffDur.max = ch.max_puff_s;

  // step selectors drive the spinner arrows / arrow keys (and the slider)
  const bindStep = (select, inputs, k, options, fallback) => {
    const saved = store(key(k));
    for (const v of options) select.add(new Option(v, v));
    select.value = options.includes(saved) ? saved : fallback;
    const apply = () => {
      for (const input of inputs) input.step = select.value;
      store(key(k), select.value);
    };
    select.addEventListener("change", apply);
    apply();
  };
  bindStep(el.contStep, [el.range, el.contVal], "cont-step", steps, steps[1]);
  bindStep(el.puffStep, [el.puffVal], "puff-step", steps, steps[1]);
  bindStep(el.durStep, [el.puffDur], "dur-step", DUR_STEPS, "0.1");
  // after the steps: a range input snaps its value to its step when assigned
  const start = clean(Math.round((0.6 * ch.cap) / Number(steps[1])) * Number(steps[1]));
  el.contVal.value = el.range.value = store(key("cont")) ?? start;
  el.puffVal.value = store(key("puff")) ?? start;
  el.puffDur.value = store(key("dur")) ?? "0.5";

  // Range checks only: a typed value off the step grid (e.g. 1.23 with step 0.1)
  // is fine, so the forms are novalidate and step mismatches are ignored.
  const ok = (input) => {
    const v = input.validity;
    const good = !(v.valueMissing || v.badInput || v.rangeOverflow || v.rangeUnderflow) &&
      !(input === el.puffDur && Number(input.value) <= 0);
    input.classList.toggle("bad", !good);
    return good;
  };
  for (const input of [el.contVal, el.puffVal, el.puffDur]) {
    input.addEventListener("input", () => ok(input));
  }

  el.range.addEventListener("input", () => { el.contVal.value = el.range.value; ok(el.contVal); });
  el.contVal.addEventListener("input", () => { el.range.value = el.contVal.value; });

  const act = async (fn) => {
    try {
      const status = await fn();
      card.localError = null;
      render(status);
    } catch (e) {
      card.localError = e.message;
      showError(card, e.message);
    }
  };

  const flowMsg = `flow must be 0–${ch.cap} ${ch.info.unit}`;
  el.contForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!ok(el.contVal)) return showError(card, (card.localError = flowMsg));
    store(key("cont"), el.contVal.value);
    act(() => post(`/api/${ch.name}/continuous`, { setpoint: Number(el.contVal.value) }));
  });
  el.contOff.addEventListener("click", () =>
    act(() => post(`/api/${ch.name}/continuous`, { setpoint: 0 })));
  el.puffForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!ok(el.puffVal)) return showError(card, (card.localError = flowMsg));
    if (!ok(el.puffDur)) {
      return showError(card, (card.localError = `duration must be in (0, ${ch.max_puff_s}] s`));
    }
    store(key("puff"), el.puffVal.value);
    store(key("dur"), el.puffDur.value);
    act(() => post(`/api/${ch.name}/puff`, {
      setpoint: Number(el.puffVal.value), duration_s: Number(el.puffDur.value),
    }));
  });

  el.canvas.addEventListener("mousemove", (ev) => {
    const r = el.canvas.getBoundingClientRect();
    card.hover = (ev.clientX - r.left) / r.width;
    draw(card);
  });
  el.canvas.addEventListener("mouseleave", () => { card.hover = null; draw(card); });

  document.getElementById("channels").appendChild(root);
  return card;
}

function showError(card, msg) {
  card.el.error.hidden = !msg;
  card.el.error.textContent = msg || "";
}

function updateCard(card, ch) {
  const { el } = card;
  card.ch = ch;
  const i = ch.info;
  el.label.textContent = ch.label;
  el.meta.textContent =
    `${ch.name} · addr ${i.address} · SN ${i.serial_number} · ${i.gas} · ` +
    `FS ${i.full_scale} ${i.unit} · cap ${ch.cap}`;

  const r = ch.reading;
  el.flow.textContent = fmt(r?.flow);
  el.set.textContent = fmt(r?.setpoint);
  el.valve.textContent = fmt(r?.valve, 1);
  showError(card, ch.error || card.localError);

  const unit = ch.info.unit;
  el.contState.textContent = ch.continuous > 0 ? `holding ${clean(ch.continuous)} ${unit}` : "off";
  el.contState.classList.toggle("on", ch.continuous > 0);

  if (ch.puff) {
    const frac = 1 - ch.puff.remaining_s / ch.puff.duration_s;
    el.bar.style.width = `${Math.min(100, frac * 100)}%`;
    el.puffState.textContent =
      `${clean(ch.puff.setpoint)} ${unit} · ${fmt(ch.puff.remaining_s, 1)} s left`;
    el.puffState.classList.add("on");
  } else {
    el.bar.style.width = "0";
    el.puffState.textContent = "";
    el.puffState.classList.remove("on");
  }

  if (r && r.t > card.lastT) {
    card.lastT = r.t;
    card.hist.push({ t: r.t, flow: r.flow, setpoint: r.setpoint });
    while (card.hist.length && card.hist[0].t < r.t - WINDOW_S - 1) card.hist.shift();
  }
  draw(card);
}

// --- chart ------------------------------------------------------------------

function draw(card) {
  const { canvas, tooltip } = card.el;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = canvas.clientHeight;
  if (canvas.width !== W * dpr || canvas.height !== H * dpr) {
    canvas.width = W * dpr;
    canvas.height = H * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const pad = { l: 34, r: 8, t: 6, b: 18 };
  const yMax = card.ch.cap;
  const tNow = card.lastT || Date.now() / 1000;
  const x = (t) => pad.l + ((t - (tNow - WINDOW_S)) / WINDOW_S) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - v / yMax) * (H - pad.t - pad.b);

  // recessive grid + axis labels
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillStyle = css("--text-dim");
  ctx.strokeStyle = css("--grid");
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  const gridStep = niceStep(yMax);
  const decimals = (clean(gridStep).split(".")[1] || "").length;
  for (let v = 0; v <= yMax + 1e-9; v += gridStep) {
    ctx.beginPath();
    ctx.moveTo(pad.l, Math.round(y(v)) + 0.5);
    ctx.lineTo(W - pad.r, Math.round(y(v)) + 0.5);
    ctx.stroke();
    ctx.fillText(v.toFixed(decimals), pad.l - 6, y(v));
  }
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let s = 0; s <= WINDOW_S; s += 10) {
    ctx.textAlign = s === 0 ? "right" : s === WINDOW_S ? "left" : "center";
    ctx.fillText(s === 0 ? "now" : `-${s}s`, x(tNow - s), H - pad.b + 4);
  }

  // only the visible window (the server's history is longer), clipped to the plot area
  // so neither series runs over the y-axis labels
  const pts = card.hist.filter((p) => p.t >= tNow - WINDOW_S - 1);
  if (pts.length > 1) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(pad.l, 0, W - pad.l - pad.r, H - pad.b);
    ctx.clip();
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    // setpoint: stepped, dashed (secondary encoding besides colour)
    ctx.strokeStyle = css("--series-set");
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    pts.forEach((p, i) => {
      if (i === 0) ctx.moveTo(x(p.t), y(p.setpoint));
      else { ctx.lineTo(x(p.t), y(pts[i - 1].setpoint)); ctx.lineTo(x(p.t), y(p.setpoint)); }
    });
    ctx.stroke();
    ctx.setLineDash([]);
    // measured: solid
    ctx.strokeStyle = css("--series-flow");
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.flow)) : ctx.moveTo(x(p.t), y(p.flow))));
    ctx.stroke();
    ctx.restore();
  }

  // hover: crosshair + tooltip on the nearest sample
  if (card.hover != null && pts.length) {
    const tHover = tNow - WINDOW_S + ((card.hover * W - pad.l) / (W - pad.l - pad.r)) * WINDOW_S;
    const visible = pts.filter((p) => p.t >= tNow - WINDOW_S);
    let best = visible[0] ?? pts[pts.length - 1];
    for (const p of visible) if (Math.abs(p.t - tHover) < Math.abs(best.t - tHover)) best = p;
    const px = x(best.t);
    ctx.strokeStyle = css("--text-dim");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(px + 0.5, pad.t);
    ctx.lineTo(px + 0.5, H - pad.b);
    ctx.stroke();
    for (const [v, c] of [[best.flow, "--series-flow"], [best.setpoint, "--series-set"]]) {
      ctx.fillStyle = c && css(c);
      ctx.strokeStyle = css("--panel");
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(px, y(v), 4, 0, 2 * Math.PI);
      ctx.fill();
      ctx.stroke();
    }
    tooltip.hidden = false;
    tooltip.textContent =
      `${(best.t - tNow).toFixed(1)} s · flow ${fmt(best.flow)} · set ${fmt(best.setpoint)}`;
    const tw = tooltip.offsetWidth;
    tooltip.style.left = `${Math.min(Math.max(px + 10, 0), W - tw)}px`;
  } else {
    tooltip.hidden = true;
  }
}

// --- status / websocket -----------------------------------------------------

function renderEvents(events) {
  const tbody = document.getElementById("events");
  tbody.replaceChildren(...events.slice().reverse().map((e) => {
    const tr = document.createElement("tr");
    const extra = Object.entries(e)
      .filter(([k]) => !["t", "mono", "channel", "event", "setpoint"].includes(k))
      .map(([k, v]) => `${k}=${v}`).join(" ");
    for (const v of [e.t.slice(11, 23), e.channel, e.event, fmt(e.setpoint, 2), extra]) {
      const td = document.createElement("td");
      td.textContent = v;
      tr.appendChild(td);
    }
    return tr;
  }));
}

// --- plugins: optional panels above the MFCs (see airflow_control/plugin.py) ---
const plugins = new Map(); // name -> {render(status), redraw()} once its script registered
const pluginsLoading = new Set();
window.airflowPlugin = (name, hooks) => plugins.set(name, hooks);

async function loadPlugin(name) {
  pluginsLoading.add(name);
  try {
    const res = await fetch(`/plugins/${name}/panel.html`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const box = document.createElement("div");
    box.innerHTML = await res.text();
    document.getElementById("plugins").append(...box.childNodes);
    const script = document.createElement("script");
    script.src = `/plugins/${name}/panel.js`;
    document.body.append(script);
  } catch (e) {
    console.error(`plugin ${name}: ${e.message}`);
  }
}

function render(status) {
  for (const ch of status.channels) {
    if (!cards.has(ch.name)) cards.set(ch.name, buildCard(ch));
    updateCard(cards.get(ch.name), ch);
  }
  renderEvents(status.events);
  for (const name of Object.keys(status.plugins || {})) {
    if (!pluginsLoading.has(name)) loadPlugin(name);
    plugins.get(name)?.render(status);
  }
  document.getElementById("logpath").textContent = status.log_path ? `log: ${status.log_path}` : "";
}

function connect() {
  const conn = document.getElementById("conn");
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = async () => {
    conn.textContent = "connected";
    conn.className = "pill pill-ok";
    try {
      const hist = await (await fetch("/api/history")).json();
      for (const [name, samples] of Object.entries(hist)) {
        const card = cards.get(name);
        if (!card) continue;
        const known = new Set(card.hist.map((p) => p.t));
        for (const [t, flow, setpoint] of samples) {
          if (!known.has(t)) card.hist.push({ t, flow, setpoint });
        }
        card.hist.sort((a, b) => a.t - b.t);
        draw(card);
      }
    } catch (e) {}
  };
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => {
    conn.textContent = shuttingDown ? "shut down" : "disconnected";
    conn.className = "pill pill-bad";
    if (!shuttingDown) setTimeout(connect, 1000);
  };
}

async function stopAll() {
  try { render(await post("/api/stop")); } catch (e) { alert(`STOP failed: ${e.message}`); }
}

async function quit() {
  if (!confirm("Set every MFC to 0 and shut down airflow_control?")) return;
  try {
    await post("/api/shutdown");
  } catch (e) {
    alert(`Shutdown failed: ${e.message}`);
    return;
  }
  shuttingDown = true;
  document.getElementById("channels").hidden = true;
  document.getElementById("plugins").hidden = true;
  document.querySelector(".events").hidden = true;
  document.getElementById("bye").hidden = false;
  for (const id of ["stop-all", "quit"]) document.getElementById(id).disabled = true;
}

document.getElementById("stop-all").addEventListener("click", stopAll);
document.getElementById("quit").addEventListener("click", quit);
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") stopAll(); });
document.getElementById("theme").addEventListener("click", () => {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
  store("airflow-theme", root.dataset.theme);
  for (const c of cards.values()) draw(c);
  for (const p of plugins.values()) p.redraw?.();
});
window.addEventListener("resize", () => { for (const c of cards.values()) draw(c); });

connect();
