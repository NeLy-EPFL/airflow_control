// Ball delivery plugin panel (airflow_control.ball_delivery): live camera view with the tracker's detections, calibration
// tools, closed-loop state/settings and a plot of the ball's distance to contact.
"use strict";

(() => {
  const API = "/api/plugins/ball_delivery";
  const root = document.getElementById("loop");
  const q = (sel) => root.querySelector(sel);
  const el = {
    source: q(".loop-source"), state: q(".loop-state"), arm: q(".loop-arm"), error: q(".loop-error"),
    canvas: q(".cam-canvas"), empty: q(".cam-empty"), help: q(".cam-help"),
    tools: [...root.querySelectorAll(".tool")], contact: q(".loop-contact"), clear: q(".loop-clear"),
    vBall: q(".v-ball"), vDist: q(".v-dist"), vFlow: q(".v-flow"), vCycles: q(".v-cycles"),
    vLast: q(".v-lastflow"), vPerf: q(".v-perf"),
    form: q(".loop-params"), params: q(".params"), paramsState: q(".params-state"),
    paramsSummary: q(".params-summary"),
    dist: q(".dist-canvas"), tooltip: q(".tooltip"),
  };
  // Overlay colours sit on a grayscale camera image, not on the page theme.
  const C = { ball: "#3ddc84", miss: "#ff5f5f", platform: "#4aa8ff", contact: "#f0b429", drag: "#ffffff" };
  const HELP = {
    ball: "Drag a tight box around the whole ball, anywhere on the track. " +
      "Its width sets the ball diameter. This replaces the ball templates; they are kept from " +
      "animal to animal and day to day, so this is needed only once per setup.",
    ball_extra: "Drag a box around the ball where it waits partly out of view. " +
      "The box may extend into the grey margin past the image edge.",
    platform: "Drag a box over a part of the platform the ball and the fly's legs don't cover " +
      "(e.g. its lower-left block). If the platform is partly out of view, the box may extend " +
      "into the grey margin. This cuts a new platform template; if the platform only moved, " +
      "drag its blue box onto it instead.",
  };
  const PARAMS = [ // [group label, [[key, label before, unit after], …]]
    ["Ramp", [["start_flow", "start", "U"], ["step_flow", "+", "U"], ["step_interval_s", "every", "s"]]],
    ["", [["max_flow", "up to", "U"]]],
    ["No contact", [["max_at_max_s", "after", "s at max:"], ["rest_s", "air off for", "s, then again"]]],
    ["Contact", [["contact_dist", "within", "Ø"], ["contact_hold_s", "for", "s"]]],
    ["Pushed away", [["release_dist", "beyond", "Ø"], ["release_hold_s", "for", "s"]]],
    ["Ball vanished", [["missing_hold_s", "off the track: wait", "s, then carry on"]]],
    ["", [["occluded_hold_s", "at the fly (air off): wait", "s"]]],
    ["Detection", [["threshold", "score ≥", ""], ["frame_timeout_s", "pause after", "s w/o frames"]]],
  ];
  const WINDOW = 30;

  let st = null; // latest tracking status
  let unit = "";
  let built = false;
  let tool = null;
  let drag = null; // {x0, y0, x1, y1} in image px
  let move = null; // platform box or contact circle being dragged: {what, cx, cy, dx, dy, …}
  let frame = null; // {bitmap, det, w, h}
  let hist = []; // {t, d, visible}
  let hover = null;
  let paramsDirty = false;

  const fmtv = (v, d = 2) => (v == null ? "–" : Number(v).toFixed(d));
  const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

  async function call(url, body) {
    try {
      const status = await post(url, body);
      showErr(null);
      render(status);
      return true;
    } catch (e) {
      showErr(e.message);
      return false;
    }
  }
  let localErr = null;
  function showErr(msg) {
    localErr = msg;
    const m = msg || st?.error || st?.source?.error;
    el.error.hidden = !m;
    el.error.textContent = m || "";
  }

  // --- build once --------------------------------------------------------------
  function build() {
    built = true;
    root.hidden = false;
    for (const [group, fields] of PARAMS) {
      const row = document.createElement("div");
      row.className = "row";
      const g = document.createElement("span");
      g.className = "grp";
      g.textContent = group;
      row.appendChild(g);
      for (const [key, label, u] of fields) {
        const lab = document.createElement("label");
        lab.append(label + " ");
        const input = document.createElement("input");
        Object.assign(input, { className: "num", type: "number", min: 0, step: "any", name: key });
        input.addEventListener("input", () => { paramsDirty = true; el.paramsState.textContent = "unsaved"; });
        lab.appendChild(input);
        const us = document.createElement("span");
        us.className = "dim";
        us.dataset.u = u;
        lab.append(" ", us);
        row.appendChild(lab);
      }
      el.params.appendChild(row);
    }
    el.form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const body = {};
      for (const input of el.params.querySelectorAll("input")) {
        if (input.value === "" || Number.isNaN(Number(input.value))) {
          return showErr(`${input.name}: enter a number`);
        }
        body[input.name] = Number(input.value);
      }
      if (await call(`${API}/params`, body)) {
        paramsDirty = false;
        el.paramsState.textContent = "saved";
      }
    });

    el.arm.addEventListener("click", () =>
      call(st?.loop.armed ? `${API}/disarm` : `${API}/arm`));
    el.contact.addEventListener("click", () => call(`${API}/contact`));
    el.clear.addEventListener("click", () => {
      if (confirm("Forget the ball templates, platform and contact position?")) call(`${API}/clear`);
    });
    for (const b of el.tools) {
      b.addEventListener("click", () => setTool(tool === b.dataset.kind ? null : b.dataset.kind));
    }

    el.canvas.addEventListener("pointerdown", (ev) => {
      if (!frame) return;
      const [x, y] = toImage(ev);
      if (!tool) { // drag the platform box or the contact circle
        const hit = hitTest(x, y);
        if (!hit) return;
        move = { ...hit, x0: x, y0: y, dx: 0, dy: 0 };
        el.canvas.setPointerCapture(ev.pointerId);
        return;
      }
      drag = { x0: x, y0: y, x1: x, y1: y };
      el.canvas.setPointerCapture(ev.pointerId);
    });
    el.canvas.addEventListener("pointermove", (ev) => {
      if (!frame) return;
      const [x, y] = toImage(ev);
      if (move) {
        [move.dx, move.dy] = [x - move.x0, y - move.y0];
        if (move.what === "platform") { // it may go past the edge, its center stays in view
          move.dx = Math.max(-move.cx, Math.min(frame.w - move.cx, move.dx));
          move.dy = Math.max(-move.cy, Math.min(frame.h - move.cy, move.dy));
        }
        drawCam();
        return;
      }
      if (!drag) {
        el.canvas.classList.toggle("movable", !tool && !!hitTest(x, y));
        return;
      }
      [drag.x1, drag.y1] = [x, y];
      drawCam();
    });
    el.canvas.addEventListener("pointerup", async () => {
      if (move) {
        const m = move;
        move = null;
        if (Math.hypot(m.dx, m.dy) < 1) return drawCam();
        const [x, y] = [m.cx + m.dx, m.cy + m.dy];
        await call(m.what === "platform" ? `${API}/platform` : `${API}/contact`, { x, y });
        return;
      }
      if (!drag) return;
      const d = drag, kind = tool;
      drag = null;
      setTool(null);
      const box = [d.x0, d.x1, d.y0, d.y1].map(Math.round);
      if (Math.abs(box[1] - box[0]) < 8 || Math.abs(box[3] - box[2]) < 8) return drawCam();
      await call(`${API}/box`, { kind, box });
    });

    el.dist.addEventListener("mousemove", (ev) => {
      const r = el.dist.getBoundingClientRect();
      hover = (ev.clientX - r.left) / r.width;
      drawDist();
    });
    el.dist.addEventListener("mouseleave", () => { hover = null; drawDist(); });

    fetch(`${API}/history`).then((r) => r.json()).then((rows) => {
      hist = rows.map(([t, d, score, visible]) => ({ t, d, visible })).concat(hist);
      hist.sort((a, b) => a.t - b.t);
      drawDist();
    }).catch(() => {});
    frameLoop();
  }

  function setTool(kind) {
    tool = kind;
    drag = null;
    for (const b of el.tools) b.classList.toggle("active", b.dataset.kind === kind);
    el.canvas.classList.toggle("drawing", !!kind);
    updateHelp();
    drawCam();
  }

  function updateHelp() {
    if (tool) { el.help.textContent = HELP[tool]; return; }
    if (st?.loop.armed) {
      el.help.textContent = "Armed. The platform box and the dashed contact circle can still be " +
        "dragged; disarm to re-mark templates. Any manual action on this MFC (setpoint, puff, " +
        "STOP) disarms the loop" + (st.source.recording != null
          ? ", and so does the end of the recording if it was armed during one." : ".");
      return;
    }
    const missing = st?.calibration.missing || [];
    if (!missing.length) {
      el.help.textContent = "Calibrated. For a new animal, drag the blue platform box onto the " +
        "platform, then the dashed circle to where the ball touches it. Then Arm.";
    } else if (missing.length === 1 && missing[0] === "contact position") {
      el.help.textContent = "Press Set contact with the ball touching the platform, or with no " +
        "ball in view to get a dashed circle next to the platform; drag it into place.";
    } else {
      el.help.textContent = `To do: mark the ${missing.join(", ")}.`;
    }
  }

  // --- camera view -------------------------------------------------------------
  const margin = () => (frame ? [Math.round(frame.w * 0.12), Math.round(frame.h * 0.12)] : [0, 0]);
  function toImage(ev) {
    const r = el.canvas.getBoundingClientRect();
    const [mx, my] = margin();
    const s = (frame.w + 2 * mx) / r.width;
    return [(ev.clientX - r.left) * s - mx, (ev.clientY - r.top) * s - my];
  }

  async function frameLoop() {
    for (;;) {
      if (shuttingDown) return;
      if (document.hidden || !st) { await sleep(300); continue; }
      const t0 = performance.now();
      try {
        const res = await fetch(`${API}/frame`, { cache: "no-store" });
        if (res.ok) {
          const det = JSON.parse(res.headers.get("X-Detection") || "null");
          const bitmap = await createImageBitmap(await res.blob());
          frame?.bitmap.close?.();
          frame = { bitmap, det, w: bitmap.width, h: bitmap.height };
          el.empty.hidden = true;
          drawCam();
        } else {
          el.empty.hidden = false;
          await sleep(400);
        }
      } catch (e) {
        await sleep(1000);
      }
      await sleep(Math.max(0, 33 - (performance.now() - t0)));
    }
  }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // text in screen pixels at image position (x, y) = its bottom-left corner
  let view = { scale: 1, mx: 0, my: 0, dpr: 1 };
  function label(ctx, text, x, y, color, align = "left") {
    const { scale, mx, my, dpr } = view;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    let sx = ((x + mx) * scale) / dpr;
    const sy = ((y + my) * scale) / dpr;
    ctx.font = "12px ui-monospace, monospace";
    const w = ctx.measureText(text).width + 8;
    if (align === "right") sx -= w;
    ctx.fillStyle = "rgba(0,0,0,0.65)";
    ctx.fillRect(sx, sy - 18, w, 18);
    ctx.fillStyle = color;
    ctx.textBaseline = "middle";
    ctx.fillText(text, sx + 4, sy - 9);
    ctx.restore();
  }

  // where the ball is, as the loop sees it (same thresholds as closedloop.py)
  function ballZone(det) {
    const p = st?.loop.params;
    if (!p || det.distance == null) return "";
    if (det.distance <= p.contact_dist) return "at contact";
    if (det.distance > p.release_dist) return "away";
    return "near contact";
  }

  // what the loop is doing and what it is waiting for
  function loopBanner() {
    const l = st?.loop;
    if (!l) return null;
    const p = l.params;
    let text = STATE_TEXT[l.state](l);
    if (l.state === "ramp" && l.contact_s != null) {
      text += ` · contact ${fmtv(l.contact_s, 1)}/${p.contact_hold_s} s`;
    } else if (l.state === "contact" && l.away_s != null) {
      text += ` · pushed away ${fmtv(l.away_s, 1)}/${p.release_hold_s} s`;
    }
    if (l.missing_s != null && l.armed) {
      text += ` · ball missing ${fmtv(l.missing_s, 1)}` +
        `/${l.missing_limit_s ?? p.missing_hold_s} s`;
    }
    const color = { off: "#cfd3da", ramp: C.platform, contact: C.ball, paused: C.contact, rest: C.contact }[l.state] || C.miss;
    return { text, color };
  }

  // the platform box or contact circle under (x, y), when they can be moved
  function hitTest(x, y) {
    const det = frame?.det, cal = st?.calibration;
    if (!det || !cal) return null;
    if (det.contact_x != null && cal.ball_diameter &&
        Math.hypot(x - det.contact_x, y - det.contact_y) <= cal.ball_diameter / 2) {
      return { what: "contact", cx: det.contact_x, cy: det.contact_y };
    }
    if (det.platform_x != null && cal.platform_box) {
      const [x0, x1, y0, y1] = cal.platform_box, w = x1 - x0, h = y1 - y0;
      const bx0 = det.platform_x - w / 2, by0 = det.platform_y - h / 2;
      if (x >= bx0 && x <= bx0 + w && y >= by0 && y <= by0 + h) {
        return { what: "platform", cx: det.platform_x, cy: det.platform_y, box: [bx0, bx0 + w, by0, by0 + h] };
      }
    }
    return null;
  }

  function drawCam() {
    if (!frame) return;
    const cv = el.canvas, ctx = cv.getContext("2d");
    const [mx, my] = margin();
    const W = frame.w + 2 * mx, H = frame.h + 2 * my;
    const dpr = window.devicePixelRatio || 1;
    const scale = Math.max(1, ((cv.clientWidth || W) * dpr) / W);
    if (cv.width !== Math.round(W * scale)) { cv.width = Math.round(W * scale); cv.height = Math.round(H * scale); }
    view = { scale, mx, my, dpr };
    ctx.setTransform(scale, 0, 0, scale, mx * scale, my * scale); // image px from here on
    ctx.clearRect(-mx, -my, W, H);
    ctx.fillStyle = cssv("--panel-2");
    ctx.fillRect(-mx, -my, W, H);
    ctx.drawImage(frame.bitmap, 0, 0);
    ctx.lineWidth = (2 * dpr) / scale; // 2 screen px

    const cal = st?.calibration, det = frame.det;
    // while dragging: the platform carries the contact along
    const [pdx, pdy] = move?.what === "platform" ? [move.dx, move.dy] : [0, 0];
    const [cdx, cdy] = move ? [move.dx, move.dy] : [0, 0];
    // platform
    if (det?.platform_x != null && cal?.platform_box) {
      const [x0, x1, y0, y1] = cal.platform_box, w = x1 - x0, h = y1 - y0;
      const px = det.platform_x + pdx, py = det.platform_y + pdy;
      // not found: the box stays where the platform was last seen
      const lostPlatform = !move && det.platform_score != null && det.platform_score < st.platform_threshold;
      ctx.strokeStyle = lostPlatform ? C.miss : C.platform;
      if (lostPlatform) ctx.setLineDash([(6 * dpr) / scale, (4 * dpr) / scale]);
      ctx.strokeRect(px - w / 2, py - h / 2, w, h);
      ctx.setLineDash([]);
      if (lostPlatform) {
        label(ctx, `platform not found (score ${fmtv(det.platform_score)}): drag the box onto it`,
          frame.w + margin()[0], py - h / 2, C.miss, "right");
      } else {
        label(ctx, `platform ${fmtv(det.platform_score)}`, px - w / 2, py - h / 2, C.platform);
      }
    }
    // contact: a ghost ball where the ball touches the platform
    if (det?.contact_x != null && cal?.ball_diameter) {
      ctx.strokeStyle = C.contact;
      ctx.setLineDash([(6 * dpr) / scale, (4 * dpr) / scale]);
      ctx.beginPath();
      ctx.arc(det.contact_x + cdx, det.contact_y + cdy, cal.ball_diameter / 2, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    // ball
    if (det && cal?.ball_boxes?.length) {
      if (det.visible) {
        const [x0, x1, y0, y1] = cal.ball_boxes[det.ball_template] || cal.ball_boxes[0];
        const w = x1 - x0, h = y1 - y0;
        ctx.strokeStyle = C.ball;
        ctx.strokeRect(det.ball_x - w / 2, det.ball_y - h / 2, w, h);
        ctx.beginPath();
        ctx.moveTo(det.ball_x - 8, det.ball_y); ctx.lineTo(det.ball_x + 8, det.ball_y);
        ctx.moveTo(det.ball_x, det.ball_y - 8); ctx.lineTo(det.ball_x, det.ball_y + 8);
        ctx.stroke();
        const parts = ["ball", ballZone(det), det.distance == null ? "" : `${fmtv(det.distance)} Ø`,
          det.at_entry_edge ? "at entry edge" : "", `score ${fmtv(det.ball_score)}`];
        label(ctx, parts.filter(Boolean).join(" · "), Math.max(-mx, det.ball_x - w / 2), det.ball_y - h / 2, C.ball);
      } else {
        // left down the track (normal) or vanished elsewhere (fell off? covered?)
        const seenEdge = st?.loop.armed ? st.loop.last_seen_edge : null;
        const gone = seenEdge === true ? " · left via entry edge"
          : seenEdge === false ? " · vanished on the track" : "";
        label(ctx, `no ball${gone} · score ${fmtv(det.ball_score)}`, 0, 0, C.miss);
      }
    }
    const banner = loopBanner();
    if (banner) label(ctx, banner.text, frame.w + mx, -my + 22 * view.dpr / view.scale, banner.color, "right");
    // box being drawn
    if (drag) {
      ctx.strokeStyle = C.drag;
      ctx.setLineDash([(4 * dpr) / scale, (3 * dpr) / scale]);
      ctx.strokeRect(Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1),
        Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0));
      ctx.setLineDash([]);
    }
  }

  // --- distance plot -----------------------------------------------------------
  function drawDist() {
    const cv = el.dist, ctx = cv.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (!W) return;
    if (cv.width !== W * dpr || cv.height !== H * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const pad = { l: 34, r: 8, t: 6, b: 18 };
    const yMax = 1.5;
    const tNow = hist.length ? hist[hist.length - 1].t : Date.now() / 1000;
    const x = (t) => pad.l + ((t - (tNow - WINDOW)) / WINDOW) * (W - pad.l - pad.r);
    const y = (v) => pad.t + (1 - Math.min(v, yMax) / yMax) * (H - pad.t - pad.b);

    ctx.font = "11px ui-monospace, monospace";
    ctx.fillStyle = cssv("--text-dim");
    ctx.strokeStyle = cssv("--grid");
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let v = 0; v <= yMax + 1e-9; v += 0.5) {
      ctx.beginPath();
      ctx.moveTo(pad.l, Math.round(y(v)) + 0.5);
      ctx.lineTo(W - pad.r, Math.round(y(v)) + 0.5);
      ctx.stroke();
      ctx.fillText(v.toFixed(1), pad.l - 6, y(v));
    }
    ctx.textBaseline = "top";
    for (let s = 0; s <= WINDOW; s += 10) {
      ctx.textAlign = s === 0 ? "right" : s === WINDOW ? "left" : "center";
      ctx.fillText(s === 0 ? "now" : `-${s}s`, x(tNow - s), H - pad.b + 4);
    }
    // thresholds: dashed, labelled directly
    const p = st?.loop.params;
    if (p) {
      ctx.strokeStyle = cssv("--series-set");
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.5;
      // contact labelled below its line, pushed away above: they are close together
      for (const [v, name, base] of [[p.contact_dist, "contact", "top"], [p.release_dist, "pushed away", "bottom"]]) {
        ctx.beginPath();
        ctx.moveTo(pad.l, y(v));
        ctx.lineTo(W - pad.r, y(v));
        ctx.stroke();
        ctx.fillStyle = cssv("--text-dim");
        ctx.textAlign = "left";
        ctx.textBaseline = base;
        ctx.fillText(name, pad.l + 4, y(v) + (base === "top" ? 2 : -2));
      }
      ctx.setLineDash([]);
    }
    // distance: gaps where the ball isn't seen; values above the axis pinned to the top
    const pts = hist.filter((h) => h.t >= tNow - WINDOW - 1);
    ctx.save();
    ctx.beginPath();
    ctx.rect(pad.l, 0, W - pad.l - pad.r, H - pad.b);
    ctx.clip();
    ctx.strokeStyle = cssv("--series-flow");
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let pen = false;
    for (const h of pts) {
      if (h.d == null) { pen = false; continue; }
      if (pen) ctx.lineTo(x(h.t), y(h.d)); else ctx.moveTo(x(h.t), y(h.d));
      pen = true;
    }
    ctx.stroke();
    ctx.restore();

    if (hover != null && pts.length) {
      const tH = tNow - WINDOW + ((hover * W - pad.l) / (W - pad.l - pad.r)) * WINDOW;
      let best = pts[0];
      for (const h of pts) if (Math.abs(h.t - tH) < Math.abs(best.t - tH)) best = h;
      const px = x(best.t);
      ctx.strokeStyle = cssv("--text-dim");
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px + 0.5, pad.t);
      ctx.lineTo(px + 0.5, H - pad.b);
      ctx.stroke();
      if (best.d != null) {
        ctx.fillStyle = cssv("--series-flow");
        ctx.strokeStyle = cssv("--panel");
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(px, y(best.d), 4, 0, 2 * Math.PI);
        ctx.fill();
        ctx.stroke();
      }
      el.tooltip.hidden = false;
      el.tooltip.textContent = `${(best.t - tNow).toFixed(1)} s · ` +
        (best.d == null ? "ball not seen" : `${fmtv(best.d)} Ø`);
      el.tooltip.style.left = `${Math.min(Math.max(px + 10, 0), W - el.tooltip.offsetWidth)}px`;
    } else {
      el.tooltip.hidden = true;
    }
  }

  // --- status ------------------------------------------------------------------
  const STATE_TEXT = {
    off: (l) => (l.reason === "recording ended" ? "OFF · recording ended" : "OFF"),
    ramp: (l) => `RAMP ${fmtv(l.flow)} ${unit}`,
    contact: () => "CONTACT · air off",
    paused: () => "PAUSED · no frames",
    rest: (l) => `REST · air off · ramp again in ${Math.max(0, l.params.rest_s - l.in_state_s).toFixed(0)} s`,
  };

  function render(status) {
    const tr = status.plugins?.ball_delivery;
    if (!tr) { root.hidden = true; return; }
    st = tr;
    if (!built) build();
    const ch = status.channels.find((c) => c.name === tr.loop.channel);
    unit = ch?.info.unit || "";
    const l = tr.loop, s = tr.source;

    el.source.textContent = `${s.source} → ${ch?.label || l.channel}`;
    el.state.textContent = STATE_TEXT[l.state](l);
    el.state.className = `badge loop-state st-${l.state}`;
    el.state.title = l.reason || "";
    el.arm.textContent = l.armed ? "Disarm" : "Arm";
    el.arm.classList.toggle("btn-primary", !l.armed);
    for (const b of [...el.tools, el.clear]) b.disabled = l.armed; // templates: disarmed only
    showErr(localErr);

    const d = tr.detection;
    el.vBall.textContent = d ? `${d.visible ? "seen" : "not seen"} · score ${fmtv(d.ball_score)}` : "–";
    el.vDist.textContent = d?.distance == null ? "–" : `${fmtv(d.distance)} ball Ø`;
    el.vFlow.textContent = `${fmtv(l.flow)} ${unit} (max ${fmtv(l.max_flow)})` +
      (l.missing_s == null ? "" : ` · ball missing ${l.missing_s} s`);
    el.vCycles.textContent = String(l.cycles) + (l.vanished ? ` · ball vanished ${l.vanished}×` : "");
    el.vLast.textContent = l.last_contact_flow == null ? "–" : `${fmtv(l.last_contact_flow)} ${unit}`;
    const rec = s.recording ? " · recording" : "";
    el.vPerf.textContent = s.connected
      ? `${s.fps ?? "–"} fps · tracking ${tr.track_ms ?? "–"} ms${rec}`
      : "not connected";

    for (const span of el.params.querySelectorAll("[data-u]")) {
      span.textContent = span.dataset.u === "U" ? unit : span.dataset.u;
    }
    if (!paramsDirty) {
      for (const input of el.params.querySelectorAll("input")) {
        if (document.activeElement !== input) input.value = l.params[input.name];
      }
    }
    // The settings stay folded: say what they are set to.
    const pp = l.params;
    el.paramsSummary.textContent =
      `ramp ${pp.start_flow} \u2192 ${pp.max_flow} ${unit}, +${pp.step_flow} every ` +
      `${pp.step_interval_s} s \u00b7 contact within ${pp.contact_dist} \u00d8`;

    if (d && (!hist.length || d.t > hist[hist.length - 1].t)) {
      hist.push({ t: d.t, d: d.distance, visible: d.visible });
      while (hist.length && hist[0].t < d.t - WINDOW - 5) hist.shift();
    }
    updateHelp();
    drawDist();
  }

  window.airflowPlugin("ball_delivery", { render, redraw: () => { drawCam(); drawDist(); } });
  window.addEventListener("resize", () => { drawCam(); drawDist(); });
})();
