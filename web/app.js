/* APEX-1 GCS HUD — WebSocket client + live Iron Man HUD rendering.
   Consumes TelemetryFrame JSON from gcs.py; no other backend knowledge. */
"use strict";

const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
const STALE_MS = 1500;      // no frames for this long -> LINK STALE
const RECONNECT_MS = 1500;
const ALT_MAX = 300;        // altitude scale for reactor + tapes + radar rim + charts [m]
const VEL_MAX = 65;         // velocity gauge scale ± [m/s] (profile peak ~58 -> ~11% headroom)
const PITCH_MAX_DEG = 20;   // synthesized pitch range [deg]
const PITCH_TAU = 0.3;      // horizon smoothing time constant [s]
const PITCH_PX_PER_DEG = 2.5;
const CMP_PPM = 1.4;        // compass px per degree (280px window shows ~200°)
const CMP_CARET_X = 140;    // compass window center [px]
const TREND_WIN = 0.55;     // trend chevron lookback window [s]
const EVENT_CAP = 12;       // max stored chart event markers

// ---------- DOM ----------
const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const linkEl = $("link");
const metEl = $("met");
const flightEl = $("flight");
const awaitingEl = $("awaiting");
const demoBadgeEl = $("demo-badge");
const velBigEl = $("vel-big");
const altBigEl = $("alt-big");
const trendVelEl = $("trend-vel");
const trendAltEl = $("trend-alt");

// ---------- geometry helpers ----------
// 270° arc, gap at the bottom: 135° -> 405° (SVG y-down, clockwise).
const A0 = 135, A1 = 405;
function polar(cx, cy, r, deg) {
  const rad = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
}
function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// ---------- side gauges ----------
function makeGauge(mountId, { label, unit, min, max, color, decimals = 1, signed = false, scale }) {
  const CX = 100, CY = 100, R = 78;
  const ARC_LEN = 2 * Math.PI * R * ((A1 - A0) / 360);
  let ticks = "";
  for (let i = 0; i <= 10; i++) {
    const a = A0 + (i / 10) * (A1 - A0);
    const [x0, y0] = polar(CX, CY, R - 11, a);
    const [x1, y1] = polar(CX, CY, R - (i % 5 === 0 ? 19 : 15), a);
    ticks += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>`;
  }
  let scaleText = "";
  if (scale) {
    // min/mid/max numeric tick labels: ends of the 270° arc + top mid tick.
    scaleText = `
      <text class="gauge-scale" x="44" y="172">${scale[0]}</text>
      <text class="gauge-scale" x="100" y="52">${scale[1]}</text>
      <text class="gauge-scale" x="156" y="172">${scale[2]}</text>`;
  }
  const d = arcPath(CX, CY, R, A0, A1);
  $(mountId).innerHTML = `
    <svg viewBox="0 0 200 200" role="img" aria-label="${label}">
      <circle class="gauge-ring" cx="${CX}" cy="${CY}" r="93"/>
      <path class="gauge-track" d="${d}"/>
      <path class="gauge-value" d="${d}" stroke-dasharray="0 ${ARC_LEN.toFixed(1)}"
            style="stroke:${color};color:${color}"/>
      <g class="gauge-ticks">${ticks}</g>
      ${scaleText}
      <text class="gauge-value-text" x="100" y="104">--</text>
      <text class="gauge-unit" x="100" y="126">${unit}</text>
      <text class="gauge-label" x="100" y="68">${label}</text>
    </svg>`;
  const arc = $(mountId).querySelector(".gauge-value");
  const text = $(mountId).querySelector(".gauge-value-text");
  return {
    set(v) {
      const frac = Math.min(1, Math.max(0, (v - min) / (max - min)));
      arc.setAttribute("stroke-dasharray", `${(frac * ARC_LEN).toFixed(1)} ${ARC_LEN.toFixed(1)}`);
      const s = v.toFixed(decimals);
      text.textContent = signed && v >= 0 ? `+${s}` : s;
    },
  };
}

// ---------- arc reactor (center altitude display) ----------
function makeReactor(mountId) {
  const CX = 150, CY = 150, R = 130;
  const ARC_LEN = 2 * Math.PI * R * ((A1 - A0) / 360);
  let ticks = "";
  for (let i = 0; i <= 10; i++) {
    const a = A0 + (i / 10) * (A1 - A0);
    const [x0, y0] = polar(CX, CY, R - 12, a);
    const [x1, y1] = polar(CX, CY, R - (i % 5 === 0 ? 22 : 17), a);
    ticks += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}"/>`;
  }
  const d = arcPath(CX, CY, R, A0, A1);
  // Center reticle: broken crosshair + 4 corner ticks, low opacity, under the readout.
  const reticle = `
    <g class="reticle">
      <line x1="150" y1="92" x2="150" y2="124"/>
      <line x1="150" y1="176" x2="150" y2="208"/>
      <line x1="92" y1="150" x2="124" y2="150"/>
      <line x1="176" y1="150" x2="208" y2="150"/>
      <line x1="102" y1="102" x2="116" y2="102"/><line x1="102" y1="102" x2="102" y2="116"/>
      <line x1="198" y1="102" x2="184" y2="102"/><line x1="198" y1="102" x2="198" y2="116"/>
      <line x1="102" y1="198" x2="116" y2="198"/><line x1="102" y1="198" x2="102" y2="184"/>
      <line x1="198" y1="198" x2="184" y2="198"/><line x1="198" y1="198" x2="198" y2="184"/>
    </g>`;
  $(mountId).innerHTML = `
    <div class="reactor-core"></div>
    <svg viewBox="0 0 300 300" role="img" aria-label="ALTITUDE">
      <path class="reactor-track" d="${d}"/>
      <path class="reactor-value" d="${d}" stroke-dasharray="0 ${ARC_LEN.toFixed(1)}"/>
      <g class="reactor-ticks">${ticks}</g>
      <circle class="reactor-ring r1" cx="150" cy="150" r="112"/>
      <circle class="reactor-ring r2" cx="150" cy="150" r="98"/>
      <circle class="reactor-ring r3" cx="150" cy="150" r="84"/>
      ${reticle}
      <text class="reactor-label" x="150" y="110">ALTITUDE</text>
      <text class="reactor-value-text" x="150" y="164">--</text>
      <text class="reactor-unit" x="150" y="190">m</text>
    </svg>`;
  const arc = $(mountId).querySelector(".reactor-value");
  const text = $(mountId).querySelector(".reactor-value-text");
  return {
    set(v) {
      const frac = Math.min(1, Math.max(0, v / ALT_MAX));
      arc.setAttribute("stroke-dasharray", `${(frac * ARC_LEN).toFixed(1)} ${ARC_LEN.toFixed(1)}`);
      text.textContent = v.toFixed(1);
    },
  };
}

// ---------- attitude indicator (synthesized pitch from velocity) ----------
function makeAttitude(mountId) {
  // Pitch ladder: lines every 10° up to ±30°, ±20° and ±30° longer.
  // Positive ladder lines sit above the horizon, labeled on the right (standard).
  const ladderSpec = [
    { p: 30, hw: 30, side: 1 },
    { p: 20, hw: 38, side: 1 },
    { p: 10, hw: 26, side: 1 },
    { p: -10, hw: 26, side: -1 },
    { p: -20, hw: 38, side: -1 },
    { p: -30, hw: 30, side: -1 },
  ];
  let ladder = "";
  for (const { p, hw, side } of ladderSpec) {
    const y = (-p * PITCH_PX_PER_DEG).toFixed(1);
    ladder += `<line class="att-ladder" x1="${-hw}" y1="${y}" x2="${hw}" y2="${y}"/>`;
    const lx = side > 0 ? hw + 5 : -hw - 5;
    ladder += `<text class="att-deg" x="${lx}" y="${(+y + 2.5).toFixed(1)}" text-anchor="${side > 0 ? "start" : "end"}">${Math.abs(p)}</text>`;
  }
  $(mountId).innerHTML = `
    <svg viewBox="0 0 200 150" role="img" aria-label="ATTITUDE">
      <defs><clipPath id="attClip"><rect x="16" y="14" width="168" height="122" rx="3"/></clipPath></defs>
      <g clip-path="url(#attClip)">
        <g class="att-horizon-g">
          <rect class="att-sky" x="-160" y="-170" width="320" height="170"/>
          <rect class="att-ground" x="-160" y="0" width="320" height="170"/>
          <line class="att-horizon" x1="-160" y1="0" x2="160" y2="0"/>
          ${ladder}
        </g>
      </g>
      <rect class="att-frame" x="16" y="14" width="168" height="122" rx="3"/>
      <g class="att-ship">
        <line x1="58" y1="75" x2="92" y2="75"/>
        <line x1="108" y1="75" x2="142" y2="75"/>
      </g>
      <circle class="att-ship-dot" cx="100" cy="75" r="2.5"/>
      <text class="att-lab" x="100" y="147">ATT · PITCH SYNTH</text>
    </svg>`;
  const g = $(mountId).querySelector(".att-horizon-g");
  g.setAttribute("transform", "translate(100 75)");
  return {
    // pitch in degrees, +nose-up (horizon shifts down the window).
    setPitch(deg) {
      g.setAttribute("transform", `translate(100 ${(75 + deg * PITCH_PX_PER_DEG).toFixed(2)})`);
    },
  };
}

// ---------- compass tape (synthetic heading: honest drift, labeled) ----------
function makeCompass(mountId) {
  const CARD = { 0: "N", 90: "E", 180: "S", 270: "W" };
  const W = (1080) * CMP_PPM; // strip spans -360°..720° for seamless wraparound
  let ticks = "";
  for (let a = -360; a < 720; a += 10) {
    const norm = ((a % 360) + 360) % 360;
    const x = ((a + 360) * CMP_PPM).toFixed(1);
    const isCard = norm % 90 === 0;
    const isNum = norm % 30 === 0;
    const len = isCard ? 12 : (isNum ? 9 : 5);
    ticks += `<line class="cmp-tick${isNum ? " maj" : ""}" x1="${x}" y1="6" x2="${x}" y2="${6 + len}"/>`;
    if (isCard) {
      ticks += `<text class="cmp-card" x="${x}" y="30">${CARD[norm]}</text>`;
    } else if (isNum) {
      ticks += `<text class="cmp-num" x="${x}" y="30">${norm}</text>`;
    }
  }
  $(mountId).innerHTML = `
    <div class="cmp-window">
      <svg class="cmp-strip" viewBox="0 0 ${W.toFixed(1)} 34" width="${W.toFixed(1)}" height="34" aria-hidden="true">${ticks}</svg>
      <div class="cmp-caret"></div>
    </div>
    <div class="cmp-readout">
      <span class="cmp-tag">HDG SYNTH</span>
      <span class="cmp-hdg">000°</span>
    </div>`;
  const strip = $(mountId).querySelector(".cmp-strip");
  const hdgEl = $(mountId).querySelector(".cmp-hdg");
  strip.style.transform = `translate(${(CMP_CARET_X - 360 * CMP_PPM).toFixed(2)}px, 0)`;
  return {
    setHeading(deg) {
      strip.style.transform = `translate(${(CMP_CARET_X - (deg + 360) * CMP_PPM).toFixed(2)}px, 0)`;
    },
    setText(deg) {
      const norm = Math.round(((deg % 360) + 360) % 360);
      hdgEl.textContent = `${String(norm).padStart(3, "0")}°`;
    },
  };
}

// ---------- numeric altitude tapes (dual, flanking the reactor) ----------
function makeAltTape(mountId, side) {
  // Content coordinate: yC(v) = 300 - v  (1 px per meter, 0 m at yC 300).
  // Window is 300 px tall showing v in [150-alt, 450-alt] for alt in [0, 300].
  const yOf = (v) => 300 - v;
  let ticks = "";
  for (let v = -60; v <= 340; v += 10) {
    const y = yOf(v).toFixed(1);
    const major = ((v % 20) + 20) % 20 === 0;
    const x1 = side === "left" ? (major ? 18 : 23) : (major ? 1 : 6);
    const x2 = side === "left" ? 29 : 12;
    ticks += `<line class="tape-tick${major ? " maj" : ""}" x1="${x1}" y1="${y}" x2="${x2}" y2="${y}"/>`;
    if (v >= 0 && v <= 300 && major) {
      ticks += `<text class="tape-num" x="${side === "left" ? 16 : 14}" y="${(+y + 3).toFixed(1)}"
        text-anchor="${side === "left" ? "end" : "start"}">${v}</text>`;
    }
  }
  const gratId = `altGrat-${side}`;
  $(mountId).innerHTML = `
    <svg viewBox="0 0 30 300" width="30" height="300" role="img" aria-label="ALTITUDE TAPE">
      <defs>
        <pattern id="${gratId}" width="6" height="6" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
          <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,170,0,0.22)" stroke-width="1.5"/>
        </pattern>
      </defs>
      <g class="tape-g">
        <rect x="0" y="300" width="30" height="180" fill="url(#${gratId})"/>
        ${ticks}
        <g class="tape-bug-g"><rect class="tape-bug" x="-3.5" y="-3.5" width="7" height="7" transform="rotate(45)"/></g>
      </g>
    </svg>
    <div class="alttape-caret"></div>`;
  const g = $(mountId).querySelector(".tape-g");
  const bug = $(mountId).querySelector(".tape-bug-g");
  g.setAttribute("transform", "translate(0 -150)");
  bug.setAttribute("transform", "translate(15 300)"); // bug at 0 m step
  let bugStep = 0;
  return {
    // Raw baro value as received — matches the reactor readout exactly (no smoothing).
    set(alt) {
      g.setAttribute("transform", `translate(0 ${(alt - 150).toFixed(2)})`);
      const step = clamp(Math.round(alt / 20) * 20, 0, 300);
      if (step !== bugStep) {
        bugStep = step;
        bug.setAttribute("transform", `translate(15 ${yOf(step).toFixed(1)})`);
      }
    },
  };
}

// ---------- radar (range rings + altitude blip on 2 axes) ----------
function makeRadar(mountId) {
  const RIM = 46;            // px at 300 m (blip scale = ALT_MAX)
  let rings = "";
  for (const m of [50, 100, 150, 200, 250]) {
    const r = (m / 300) * RIM;
    rings += `<circle class="radar-ring inner" cx="50" cy="50" r="${r.toFixed(1)}"/>`;
    rings += `<text class="radar-num" x="51.5" y="${(50 - r + 3.5).toFixed(1)}">${m}</text>`;
  }
  $(mountId).innerHTML = `
    <svg viewBox="0 0 100 100" role="img" aria-label="RADAR">
      <circle class="radar-ring" cx="50" cy="50" r="${RIM}"/>
      ${rings}
      <line class="radar-cross" x1="50" y1="4" x2="50" y2="96"/>
      <line class="radar-cross" x1="4" y1="50" x2="96" y2="50"/>
      <g class="radar-sweep">
        <line class="radar-sweep-line" x1="50" y1="50" x2="50" y2="6" stroke-width="1.5" opacity="0.8"/>
      </g>
      <circle class="radar-blip" cx="50" cy="50" r="2.5"/>
      <circle class="radar-blip2" cx="50" cy="50" r="2"/>
      <text class="radar-label" x="50" y="99" text-anchor="middle">ALT·RNG</text>
    </svg>`;
  const blip = $(mountId).querySelector(".radar-blip");
  const blip2 = $(mountId).querySelector(".radar-blip2");
  return {
    set(alt) {
      const frac = Math.min(1, Math.max(0, alt / ALT_MAX));
      blip.setAttribute("cx", "50");
      blip.setAttribute("cy", (50 - frac * RIM).toFixed(1));
      blip2.setAttribute("cy", "50");
      blip2.setAttribute("cx", (50 + frac * RIM).toFixed(1));
    },
  };
}

// ---------- live time-series charts (canvas, no library) ----------
function makeChart(mountId, { label, unit, min, max, color, windowSec = 30, markers }) {
  const mount = $(mountId);
  const canvas = document.createElement("canvas");
  mount.appendChild(canvas);
  const ctx = canvas.getContext("2d");
  const samples = [];
  let lastT = -1;

  function resize() {
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, mount.clientWidth * dpr);
    canvas.height = Math.max(1, mount.clientHeight * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }
  window.addEventListener("resize", resize);

  function add(t, v) {
    if (lastT >= 0 && t < lastT - 1) samples.length = 0;  // new flight: t reset
    lastT = t;
    samples.push({ t, v });
    const cutoff = t - windowSec;
    while (samples.length && samples[0].t < cutoff) samples.shift();
    draw();
  }

  function draw() {
    const w = mount.clientWidth, h = mount.clientHeight;
    if (w === 0 || h === 0) return;
    ctx.clearRect(0, 0, w, h);

    // horizontal grid
    ctx.strokeStyle = "rgba(0,229,255,0.12)";
    ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) {
      const y = (h / 4) * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    // zero line (for signed values like velocity)
    if (min < 0 && max > 0) {
      const yz = h - ((0 - min) / (max - min)) * h;
      ctx.strokeStyle = "rgba(0,229,255,0.28)";
      ctx.beginPath(); ctx.moveTo(0, yz); ctx.lineTo(w, yz); ctx.stroke();
    }

    if (samples.length < 2) return;
    const tNow = samples[samples.length - 1].t;
    const t0 = tNow - windowSec;
    const xOf = (t) => ((t - t0) / windowSec) * w;
    const yOf = (v) => h - ((Math.max(min, Math.min(max, v)) - min) / (max - min)) * h;

    // data line with glow
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8;
    ctx.beginPath();
    for (let i = 0; i < samples.length; i++) {
      const x = xOf(samples[i].t), y = yOf(samples[i].v);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.shadowBlur = 0;

    // current value + label
    const last = samples[samples.length - 1];
    ctx.fillStyle = color;
    ctx.font = "13px 'Share Tech Mono', monospace";
    ctx.textAlign = "left";
    ctx.fillText(`${last.v.toFixed(1)} ${unit}`, 8, 16);
    ctx.fillStyle = "rgba(77,127,146,1)";
    ctx.font = "10px 'Orbitron', monospace";
    ctx.fillText(label, 8, h - 8);

    // event markers (amber dashed verticals, persist across the window scroll)
    if (markers && markers.length) {
      ctx.save();
      ctx.strokeStyle = "rgba(255,122,0,0.6)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.font = "9px 'Share Tech Mono', monospace";
      for (const m of markers) {
        if (m.t < t0 || m.t > tNow) continue;
        const x = xOf(m.t);
        ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, h - 16); ctx.stroke();
        ctx.fillStyle = "rgba(255,122,0,0.95)";
        const flip = x > w - 52;
        ctx.textAlign = flip ? "right" : "left";
        ctx.fillText(m.label, x + (flip ? -3 : 3), h - 20);
      }
      ctx.restore();
    }
  }

  resize();
  return { add };
}

// ---------- build ----------
const reactor = makeReactor("reactor");
const radar = makeRadar("radar");
const attitude = makeAttitude("attitude");
const compass = makeCompass("compass");
const tapeL = makeAltTape("alttape", "left");
const tapeR = makeAltTape("alttape-r", "right");
const markers = [];
const chartAlt = makeChart("chart-altitude", { label: "ALTITUDE", unit: "m", min: 0, max: ALT_MAX, color: "#00e5ff", markers });
const chartVel = makeChart("chart-velocity", { label: "VELOCITY", unit: "m/s", min: -VEL_MAX, max: VEL_MAX, color: "#ffd60a", markers });
const gauges = {
  velocity: makeGauge("g-velocity", { label: "VELOCITY", unit: "m/s", min: -VEL_MAX, max: VEL_MAX, color: "#00e5ff", signed: true, scale: ["−65", "0", "+65"] }),
  accel: makeGauge("g-accel", { label: "ACCEL", unit: "m/s²", min: -20, max: 60, color: "#ff7a00", signed: true, scale: ["−20", "+20", "+60"] }),
  pressure: makeGauge("g-pressure", { label: "PRESSURE", unit: "hPa", min: 980, max: 1020, color: "#4d9fff", scale: ["980", "1000", "1020"] }),
  temperature: makeGauge("g-temperature", { label: "TEMP", unit: "°C", min: 10, max: 20, color: "#ffd60a", scale: ["10", "15", "20"] }),
};

// ---------- corner system readouts (wired to real signals) ----------
const barState = { pwr: -1, com: -1, nav: -1, sys: -1 };
function setBar(key, frac) {
  const pct = Math.round(clamp(frac, 0, 1) * 100);
  if (pct === barState[key]) return;
  barState[key] = pct;
  $(`sr-${key}`).style.setProperty("--fill", pct + "%");
  $(`sr-${key}-v`).textContent = pct + "%";
}

// ---------- frame handling ----------
let lastMsg = 0;
let gotFrame = false;
let latest = null;
let lastFrameT = -1;
let prevStatus = null;
let pitchTarget = 0;
let pwrFrac = 0;
let comFrac = 0;
let chevronsOn = false;
const bufAlt = [];   // recent (t, altitude) samples for trend chevrons
const bufVel = [];   // recent (t, velocity) samples
const frameTimes = []; // arrival times for the SYS readout (last 1 s)

function pushTrend(buf, t, v) {
  buf.push({ t, v });
  while (buf.length && buf[0].t < t - TREND_WIN) buf.shift();
}
function trendDelta(buf) {
  return buf.length < 2 ? 0 : buf[buf.length - 1].v - buf[0].v;
}
function setTrend(el, delta, thr) {
  const s = delta >= thr ? "▲" : delta <= -thr ? "▼" : "";
  if (el.textContent !== s) {
    el.textContent = s;
    if (s) chevronsOn = true;
  }
}

function handleFrame(f) {
  lastMsg = Date.now();
  const wasLive = gotFrame;
  if (!gotFrame) {
    gotFrame = true;
    awaitingEl.classList.add("hidden");
  }

  // New flight (t rewinds): clear event markers + trend buffers.
  const rewind = lastFrameT >= 0 && f.t < lastFrameT - 1;
  if (rewind) {
    markers.length = 0;
    bufAlt.length = 0;
    bufVel.length = 0;
  }

  // Event markers on status transitions. Gated on wasLive so the WS
  // 400-frame replay never records a transition straddling two flights.
  if (wasLive && prevStatus && prevStatus !== f.status) {
    if (prevStatus === "BOOST" && f.status === "ASCENT") markers.push({ t: f.t, label: "BOOST END" });
    else if (f.status === "PARACHUTE") markers.push({ t: f.t, label: "CHUTE" });
    else if (f.status === "LANDED") markers.push({ t: f.t, label: "LANDED" });
    if (markers.length > EVENT_CAP) markers.shift();
  }
  prevStatus = f.status;
  lastFrameT = f.t;
  latest = f;

  statusEl.textContent = f.status;
  statusEl.dataset.phase = f.status;
  metEl.textContent = f.t.toFixed(1);
  flightEl.textContent = f.flight;
  reactor.set(f.altitude);
  tapeL.set(f.altitude);
  tapeR.set(f.altitude);
  radar.set(f.altitude);
  gauges.velocity.set(f.velocity);
  gauges.accel.set(f.accel);
  gauges.pressure.set(f.pressure);
  gauges.temperature.set(f.temperature);
  // Synthesized pitch: +nose-up while climbing, -nose-down while falling, level at rest/apogee.
  pitchTarget = clamp(f.velocity / 60, -1, 1) * PITCH_MAX_DEG;
  pushTrend(bufAlt, f.t, f.altitude);
  pushTrend(bufVel, f.t, f.velocity);
  pwrFrac = (f.status === "PRE-LAUNCH" || f.status === "LANDED") ? 0.6 : 1.0;
  frameTimes.push(lastMsg);
  const cutoff = lastMsg - 1000;
  while (frameTimes.length && frameTimes[0] < cutoff) frameTimes.shift();
  chartAlt.add(f.t, f.altitude);
  chartVel.add(f.t, f.velocity);
}

// ---------- render loop (60 fps smoothing between 50 Hz frames) ----------
let pitchShown = 0;
let lastRafMs = performance.now();
let lastTextMs = 0;
let lastHdgTextMs = 0;

function frameLoop(nowMs) {
  const dt = Math.min(0.1, Math.max(0, (nowMs - lastRafMs) / 1000));
  lastRafMs = nowMs;
  const fresh = gotFrame && (Date.now() - lastMsg) <= STALE_MS;

  // Attitude: exponential lerp (tau ~0.3 s); levels out when the link goes stale.
  const target = fresh ? pitchTarget : 0;
  const k = 1 - Math.exp(-dt / PITCH_TAU);
  pitchShown += (target - pitchShown) * k;
  if (Math.abs(target - pitchShown) < 0.005) pitchShown = target;
  attitude.setPitch(pitchShown);

  // Compass: slow sinusoidal drift (±15° over ~60 s), frozen when stale.
  if (fresh) {
    const hdg = 15 * Math.sin((2 * Math.PI * (nowMs / 1000)) / 60);
    compass.setHeading(hdg);
    if (nowMs - lastHdgTextMs > 125) {
      lastHdgTextMs = nowMs;
      compass.setText(hdg);
    }
  }

  // Trend chevrons (hidden when stale or flat).
  if (fresh) {
    setTrend(trendVelEl, trendDelta(bufVel), 1.0);
    setTrend(trendAltEl, trendDelta(bufAlt), 1.0);
  } else if (chevronsOn) {
    trendVelEl.textContent = "";
    trendAltEl.textContent = "";
    chevronsOn = false;
  }

  // Big mini readouts (throttled ~15 Hz).
  if (latest && nowMs - lastTextMs > 66) {
    lastTextMs = nowMs;
    velBigEl.textContent = latest.velocity.toFixed(1);
    altBigEl.textContent = latest.altitude.toFixed(1);
  }

  // Corner bars: PWR from status, COM link health, NAV altitude fraction,
  // SYS = frames received in last 1 s × 2 (100% at 50 Hz).
  setBar("pwr", latest ? pwrFrac : 0);
  if (fresh) comFrac = 1; else comFrac = Math.max(0, comFrac - dt * 0.8);
  setBar("com", comFrac);
  setBar("nav", latest ? clamp(latest.altitude, 0, ALT_MAX) / ALT_MAX : 0);
  setBar("sys", frameTimes.length / 50);
  requestAnimationFrame(frameLoop);
}
requestAnimationFrame(frameLoop);

// Link liveness: the server only pushes new frames, so silence means stale.
setInterval(() => {
  if (!gotFrame) return;
  const stale = Date.now() - lastMsg > STALE_MS;
  linkEl.classList.toggle("stale", stale);
  linkEl.classList.toggle("live", !stale);
  linkEl.textContent = stale ? "● LINK STALE" : "● LIVE";
}, 400);

// ---------- demo mode (client-side synthetic telemetry, zero dependencies) ----------
// A faithful re-implementation of simulator.py's flight + sensor model, driving
// the SAME handleFrame() pipeline — no instrument-update logic is duplicated.
// Used for ?demo=1, and as an automatic fallback when no GCS is reachable, so
// the HUD runs standalone on GitHub Pages (or any static host).
const DEMO = {
  dt: 0.02,            // 50 Hz, matches simulator.py
  g: 9.81,
  thrust: 40.0,        // NET upward accel during boost [m/s^2]
  boostTime: 2.0,
  dragK: 0.0009,       // body quadratic drag [1/m]
  chuteK: 0.15,        // chute quadratic drag [1/m]
  chuteAlt: 50.0,
  relaunchPause: 1.5,  // hold time on the pad after landing [s]
  nAlt: 8.0, nAccel: 0.3, nVel: 0.5, nPress: 0.5, nTemp: 0.3, // 1-sigma sensor noise
};
const demo = { on: false, forced: false, flight: 0, t: 0, y: 0, v: 0, a: 0, chute: false, landed: false };

// Box-Muller: standard normal.
function gauss() {
  let u = 0;
  while (u === 0) u = Math.random();
  let v = 0;
  while (v === 0) v = Math.random();
  return Math.sqrt(-2.0 * Math.log(u)) * Math.cos(2.0 * Math.PI * v);
}
function baroPressure(h) {
  // International barometric formula (troposphere, h < 11 km) — same as simulator.py.
  return 1013.25 * Math.pow(1 - 0.0065 * Math.max(h, 0) / 288.15, 5.2558);
}

function demoReset(flight) {
  demo.flight = flight; demo.t = 0; demo.y = 0; demo.v = 0; demo.a = 0;
  demo.chute = false; demo.landed = false;
}
function demoStatus() {
  // Same decision order as simulator.py FlightSimulator.status().
  if (demo.landed) return "LANDED";
  if (demo.t <= 0) return "PRE-LAUNCH";
  if (demo.t < DEMO.boostTime) return "BOOST";
  if (demo.chute) return "PARACHUTE";
  return demo.v > 0 ? "ASCENT" : "DESCENT";
}
function demoFrame() {
  // Ground truth + noisy sensor readings (mirrors simulator.py step/sensor_readings).
  const status = demoStatus();
  const h = Math.max(demo.y, 0);
  const pressure = baroPressure(h) + gauss() * DEMO.nPress;
  const temperature = 12 + gauss() * DEMO.nTemp;
  const altitude = Math.max(0, h + gauss() * DEMO.nAlt); // baro-recovered, clamped on the pad
  const accel = demo.a + DEMO.g + gauss() * DEMO.nAccel;  // specific force
  const velocity = demo.v + gauss() * DEMO.nVel;
  return {
    t: +demo.t.toFixed(3),
    flight: demo.flight,
    status,
    altitude: +altitude.toFixed(3),
    velocity: +velocity.toFixed(3),
    accel: +accel.toFixed(3),
    pressure: +pressure.toFixed(3),
    temperature: +temperature.toFixed(3),
  };
}
function demoStep() {
  if (demo.landed) {
    // Hold on the pad ~relaunchPause s, then relaunch: flight++, t rewinds to 0.
    // The chart reset heuristic (t < lastT - 1) catches the rewind, as in live telemetry.
    demo.t += DEMO.dt;
    if (demo.t >= DEMO.relaunchPause) demoReset(demo.flight + 1);
    return;
  }
  // Semi-implicit Euler, mirroring simulator.py step().
  const thrust = (demo.t < DEMO.boostTime) ? DEMO.thrust : 0;
  const k = demo.chute ? DEMO.chuteK : DEMO.dragK;
  const a = thrust - DEMO.g - k * demo.v * Math.abs(demo.v);
  demo.a = a;
  demo.v += a * DEMO.dt;
  demo.y += demo.v * DEMO.dt;
  demo.t += DEMO.dt;
  if (!demo.chute && demo.v < 0 && demo.y <= DEMO.chuteAlt) demo.chute = true;
  if (demo.y <= 0 && demo.v <= 0) { demo.y = 0; demo.v = 0; demo.landed = true; }
}

let demoTimer = null;
let demoStart = 0;
function startDemo(forced) {
  if (demo.on) return;
  demo.on = true;
  demo.forced = !!forced;
  if (demoStart === 0) demoStart = Date.now(); // one 5-s grace window per page load
  if (demo.flight === 0) demoReset(1);         // first flight of this session
  awaitingEl.classList.add("hidden");          // demo: no AWAITING TELEMETRY overlay
  demoBadgeEl.classList.remove("hidden");      // amber DEMO tag
  // The demo never goes stale: pin the link pill to LIVE (skip the stale interval).
  linkEl.classList.remove("stale");
  linkEl.classList.add("live");
  linkEl.textContent = "\u25CF LIVE";
  if (demoTimer) return;
  demoTimer = setInterval(() => { demoStep(); handleFrame(demoFrame()); }, 1000 / 50);
}
function stopDemo() {
  if (!demo.on) return;
  demo.on = false;
  if (demoTimer) { clearInterval(demoTimer); demoTimer = null; }
  demoBadgeEl.classList.add("hidden"); // back to live: hide the tag, real pill takes over
}

// ---------- WebSocket (auto-reconnect, gated by demo mode) ----------
function connect() {
  if (demo.on) return;
  const ws = new WebSocket(WS_URL);
  ws.onmessage = (e) => {
    let live = false;
    try {
      const f = JSON.parse(e.data);
      if (f && typeof f.t === "number") { handleFrame(f); live = true; }
    } catch (_) { /* skip malformed frame */ }
    // Real telemetry beat the 5-s fallback -> leave demo, go live.
    if (live && demo.on && !demo.forced) stopDemo();
  };
  ws.onclose = () => setTimeout(connect, RECONNECT_MS);
  ws.onerror = () => ws.close();
}
const params = new URLSearchParams(location.search);
const forcedDemo = params.has("demo") && params.get("demo") !== "0";
if (forcedDemo) {
  startDemo(true);              // ?demo=1 -> demo immediately, never open the WS
} else {
  connect();                    // normal WS flow (auto-reconnects if it drops)
  setTimeout(() => {            // no GCS (e.g. GitHub Pages) -> auto demo after 5 s
    if (!gotFrame && !demo.on) startDemo(false);
  }, 5000);
}
