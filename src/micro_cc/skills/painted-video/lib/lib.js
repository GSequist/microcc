// Vendored, unmodified, from github.com/ledbetterljoshua/functional-emotions-video
// (MIT — see ../LICENSE-lib). See ../RENDERER.md for what's generic vs. example dressing.
//
// Generic and reusable as-is: time/rng/noise helpers, palette shape, drawing helpers,
// the figure rig (figure()), camera/keyframe/shot system (cam, kf, shot, chapter),
// screen effects (flash/blackout/iris), light occlusion.
//
// Song-specific EXAMPLE dressing — copy the pattern, don't assume the content fits a
// new project: `researcher()` and `ember()` are this song's characters; `POSE` presets
// and `groove()` styles were tuned for them. A new project writes its own creature/
// character functions the same way (draw silhouette on `s`, glow on `f`, occlude if
// something should block a light behind it) rather than reusing these verbatim.
//
// Time, audio, rng, drawing helpers, the figure rig, shot registry.
const DUR = 372.7;
const TAU = Math.PI * 2;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lerp = (a, b, t) => a + (b - a) * t;
const inv = (a, b, x) => clamp((x - a) / (b - a));
const smooth = (x) => { x = clamp(x); return x * x * (3 - 2 * x); };
const ease = {
  out: (x) => 1 - Math.pow(1 - clamp(x), 3),
  in: (x) => Math.pow(clamp(x), 3),
  inOut: (x) => { x = clamp(x); return x < .5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; },
  back: (x) => { x = clamp(x); if (x <= 0) return 0; const c = 1.7; return 1 + (c + 1) * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); },
  sine: (x) => .5 - .5 * Math.cos(Math.PI * clamp(x)),
};
function hash(n) { n = (n ^ 61) ^ (n >>> 16); n = n + (n << 3); n = n ^ (n >>> 4); n = Math.imul(n, 0x27d4eb2d); n = n ^ (n >>> 15); return (n >>> 0) / 4294967296; }
function rng(seed) { let a = seed >>> 0; return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
function noise1(x, seed = 0) { const i = Math.floor(x), f = x - i; const a = hash(i * 7919 + seed * 104729), b = hash((i + 1) * 7919 + seed * 104729); const u = f * f * (3 - 2 * f); return a + (b - a) * u; }
const snoise = (x, seed = 0) => noise1(x, seed) * 2 - 1;

// ---- audio ----
function F(name, t, sm = 0) {
  const a = FEAT[name]; const f = t * FEAT.fps;
  if (!sm) { const i = Math.floor(f), u = f - i; return lerp(a[clamp(i, 0, a.length - 1)] || 0, a[clamp(i + 1, 0, a.length - 1)] || 0, u); }
  let s = 0, n = 0; const c = Math.round(f); for (let k = -sm; k <= sm; k++) { const v = a[c + k]; if (v !== undefined) { s += v; n++; } } return n ? s / n : 0;
}
const BEAT = 0.7, B0 = 1.7; // 85.7 bpm grid
function lastBeat(t) { const b = FEAT.beats; if (t < b[0]) return -1; let lo = 0, hi = b.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (b[m] <= t) lo = m; else hi = m - 1; } return lo; }
function beatPulse(t, decay = 6) { const i = lastBeat(t); return i < 0 ? 0 : Math.exp(-decay * (t - FEAT.beats[i])); }
const beatPhase = (t) => (((t - B0) / BEAT) % 1 + 1) % 1;
// lazy swing: bodies sway across two beats with the backbeat dragged late
function sway(t) { const ph = (((t - B0) / (BEAT * 2)) % 1 + 1) % 1; const sw = ph < .5 ? ph * 1.16 : .58 + (ph - .5) * .84; return Math.sin(sw * TAU); }
function bob(t) { const ph = beatPhase(t); return Math.pow(Math.sin(ph * Math.PI), 2); } // dips each beat

// ---- lyrics (timing only; almost never drawn) ----
const LINES = LYRICS;
const norm = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, '');
function when(word, after = 0) { const w = norm(word); for (const l of LINES) for (const x of l.words) if (x.s >= after - .01 && norm(x.t) === w) return x.s; throw new Error('no word ' + word + ' after ' + after); }

// ---- palette ----
const P = {
  night0: '#070b1f', night1: '#0e1a45', night2: '#233a7a', dusk: '#4b3f7e', haze: '#6d5a8e',
  ink: '#0a0d1c', hill: '#10183a', hill2: '#1a2552', city: '#0d1330',
  ember: '#ff5a1f', amber: '#ffb347', gold: '#ffd98a', cream: '#f3e6c9', coat: '#d8d4c6', coatShade: '#8f91a0',
  red: '#e0301e', sea: '#0f2a4a', sea2: '#1f4a6e', skin: '#e8b98a',
};

// ---- drawing helpers (s = src ctx, f = light ctx), all in 1920x1080 coords ----
let s, f;
function bindCtx() { s = Paint.sctx; f = Paint.fctx; }
function fill(g, c) { g.fillStyle = c; g.fillRect(-50, -50, W + 100, H + 100); }
function vgrad(g, y0, y1, stops) { const gr = g.createLinearGradient(0, y0, 0, y1); stops.forEach(([o, c]) => gr.addColorStop(o, c)); return gr; }
function rgrad(g, x, y, r0, r1, stops) { const gr = g.createRadialGradient(x, y, r0, x, y, r1); stops.forEach(([o, c]) => gr.addColorStop(o, c)); return gr; }
function glow(x, y, r, color, a = 1) { // soft light on the light layer
  if (!(r > .5)) return;
  f.save(); f.globalCompositeOperation = 'lighter'; f.globalAlpha = a;
  f.fillStyle = rgrad(f, x, y, 0, r, [[0, color], [1, 'rgba(0,0,0,0)']]); f.fillRect(x - r, y - r, r * 2, r * 2); f.restore();
}
function dot(g, x, y, r, c) { g.fillStyle = c; g.beginPath(); g.arc(x, y, Math.max(.1, r), 0, TAU); g.fill(); }
function poly(g, pts, c, close = true) { g.beginPath(); pts.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y)); if (close) { g.closePath(); g.fillStyle = c; g.fill(); } else { g.strokeStyle = c; g.stroke(); } }
function ridge(g, y, amp, freq, seed, c, x0 = -60, x1 = W + 60, bottom = H + 60) { // a hill line filled below
  g.beginPath(); g.moveTo(x0, bottom);
  for (let x = x0; x <= x1; x += 16) g.lineTo(x, y + (noise1(x * freq, seed) - .5) * amp + (noise1(x * freq * 3.1, seed + 9) - .5) * amp * .3);
  g.lineTo(x1, bottom); g.closePath(); g.fillStyle = c; g.fill();
}
function stroke(g, pts, c, w, cap = 'round') { g.lineCap = cap; g.lineJoin = 'round'; g.lineWidth = w; g.strokeStyle = c; g.beginPath(); pts.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y)); g.stroke(); }
function bez(p0, p1, p2, p3, n = 30) { const o = []; for (let i = 0; i <= n; i++) { const u = i / n, v = 1 - u; o.push([v * v * v * p0[0] + 3 * v * v * u * p1[0] + 3 * v * u * u * p2[0] + u * u * u * p3[0], v * v * v * p0[1] + 3 * v * v * u * p1[1] + 3 * v * u * u * p2[1] + u * u * u * p3[1]]); } return o; }
function at(pts, u) { const i = clamp(u) * (pts.length - 1); const k = Math.floor(i), r = i - k; const a = pts[k], b = pts[Math.min(k + 1, pts.length - 1)]; return [lerp(a[0], b[0], r), lerp(a[1], b[1], r)]; }

// ---- the figure rig ----
// pose angles are radians from straight down (limbs) / straight up (torso, head); + is toward screen-right
const POSE = {
  stand: { lean: 0, head: 0, aL: [.12, .05], aR: [-.12, -.05], lL: [.06, 0], lR: [-.06, 0] },
  rise: { lean: 0, head: -.1, aL: [.5, .9], aR: [-.5, -.9], lL: [0, 0], lR: [0, 0] },
  reach: { lean: .1, head: .15, aL: [.3, .2], aR: [-2.2, -2.5], lL: [.08, 0], lR: [-.1, .1] },
  write: { lean: .45, head: .5, aL: [-.8, -1.6], aR: [-.6, -1.4], lL: [-1.3, 0], lR: [-1.4, .05] },
  sit: { lean: .05, head: .1, aL: [-.5, -1.2], aR: [-.4, -1.1], lL: [-1.5, 0], lR: [-1.55, .05] },
  hold: { lean: 0, head: .05, aL: [.1, .1], aR: [-.9, -1.6], lL: [.06, 0], lR: [-.06, 0] },
  lean: { lean: -.15, head: -.2, aL: [.2, .3], aR: [-.35, -.2], lL: [.1, .02], lR: [-.02, 0] },
  bow: { lean: .9, head: 1.1, aL: [.2, .1], aR: [-.2, -.1], lL: [.1, 0], lR: [-.1, 0] },
  armsUp: { lean: 0, head: -.3, aL: [2.6, 2.9], aR: [-2.6, -2.9], lL: [.12, 0], lR: [-.12, 0] },
  dance1: { lean: .15, head: .3, aL: [1.2, 2.2], aR: [-.5, -1.5], lL: [.35, -.3], lR: [-.15, .1] },
  dance2: { lean: -.15, head: -.3, aL: [.5, 1.5], aR: [-1.2, -2.2], lL: [.15, -.1], lR: [-.35, .3] },
};
function mixPose(a, b, k) {
  const o = {}; for (const key of Object.keys(a)) o[key] = Array.isArray(a[key]) ? a[key].map((v, i) => lerp(v, b[key][i], k)) : lerp(a[key], b[key], k); return o;
}
// tapered limb segment
function taper(g, a, b, w1, w2, c) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy) || 1, nx = -dy / L, ny = dx / L;
  g.fillStyle = c; g.beginPath();
  g.moveTo(a[0] + nx * w1, a[1] + ny * w1); g.lineTo(b[0] + nx * w2, b[1] + ny * w2); g.lineTo(b[0] - nx * w2, b[1] - ny * w2); g.lineTo(a[0] - nx * w1, a[1] - ny * w1); g.fill();
  dot(g, a[0], a[1], w1, c); dot(g, b[0], b[1], w2, c);
}
// draws a mannequin-like figure; returns joints. o: x,y = hip point, sc = px per unit (figure ~1.9 units tall)
function figure(g, pose, o) {
  const { x, y, sc = 100, color = P.ink, coat = null, headC = null, width = 1, limbC = null } = o;
  const seg = (p, ang, len, up = false) => [p[0] + Math.sin(ang) * len * sc, p[1] + (up ? -1 : 1) * Math.cos(ang) * len * sc];
  const hip = [x, y];
  const neck = seg(hip, pose.lean, .6, true);
  const head = seg(neck, pose.lean + pose.head, .19, true);
  const cl = Math.cos(pose.lean), sl = Math.sin(pose.lean);
  const shL = [neck[0] - .17 * sc * cl + .03 * sc * sl, neck[1] + .17 * sc * sl * 0 + .04 * sc], shR = [neck[0] + .17 * sc * cl + .03 * sc * sl, neck[1] + .04 * sc];
  const hpL = [hip[0] - .12 * sc * cl, hip[1] + .12 * sc * sl * .3], hpR = [hip[0] + .12 * sc * cl, hip[1] - .12 * sc * sl * .3];
  const elL = seg(shL, pose.aL[0], .3), hdL = seg(elL, pose.aL[1], .29);
  const elR = seg(shR, pose.aR[0], .3), hdR = seg(elR, pose.aR[1], .29);
  const knL = seg(hpL, pose.lL[0], .44), ftL = seg(knL, pose.lL[1], .45);
  const knR = seg(hpR, pose.lR[0], .44), ftR = seg(knR, pose.lR[1], .45);
  const W_ = width * sc, lc = limbC || color;
  // legs
  taper(g, hpL, knL, .085 * W_, .06 * W_, lc); taper(g, knL, ftL, .06 * W_, .04 * W_, lc);
  taper(g, hpR, knR, .085 * W_, .06 * W_, lc); taper(g, knR, ftR, .06 * W_, .04 * W_, lc);
  // torso: shoulders -> waist -> hips
  const waist = [lerp(hip[0], neck[0], .42), lerp(hip[1], neck[1], .42)];
  g.fillStyle = coat || color; g.beginPath();
  g.moveTo(shL[0], shL[1] - .02 * sc); g.quadraticCurveTo(neck[0], neck[1] - .03 * sc, shR[0], shR[1] - .02 * sc);
  g.quadraticCurveTo(waist[0] + .15 * W_ * cl, waist[1], hpR[0] + .03 * sc, hpR[1] + .04 * sc);
  if (coat) { const kn = [(knL[0] + knR[0]) / 2, (knL[1] + knR[1]) / 2]; g.lineTo(kn[0] + .22 * W_, kn[1] + .05 * sc); g.lineTo(kn[0] - .22 * W_, kn[1] + .05 * sc); }
  g.lineTo(hpL[0] - .03 * sc, hpL[1] + .04 * sc); g.quadraticCurveTo(waist[0] - .15 * W_ * cl, waist[1], shL[0], shL[1] - .02 * sc); g.fill();
  if (coat && coat[0] === '#') { // lab coat detail: shaded side, lapels, button line, pocket
    const kn = [(knL[0] + knR[0]) / 2, (knL[1] + knR[1]) / 2], hem = [kn[0], kn[1] + .05 * sc];
    g.save(); g.clip();
    g.fillStyle = 'rgba(10,12,30,.28)'; g.beginPath(); g.moveTo(neck[0], neck[1] - .1 * sc); g.lineTo(hem[0] - .02 * sc, hem[1]); g.lineTo(hem[0] - .5 * sc, hem[1]); g.lineTo(shL[0] - .2 * sc, shL[1] - .1 * sc); g.fill();
    g.fillStyle = 'rgba(10,12,30,.45)'; g.beginPath(); g.moveTo(neck[0] - .07 * sc * cl, neck[1] - .01 * sc); g.lineTo(waist[0], waist[1] + .02 * sc); g.lineTo(neck[0] + .07 * sc * cl, neck[1] - .01 * sc); g.lineTo(neck[0], neck[1] + .06 * sc); g.fill();
    g.strokeStyle = 'rgba(10,12,30,.35)'; g.lineWidth = Math.max(1, .012 * sc); g.beginPath(); g.moveTo(waist[0], waist[1] + .02 * sc); g.lineTo(hem[0], hem[1]); g.stroke();
    g.fillStyle = 'rgba(10,12,30,.22)'; g.fillRect(waist[0] + .05 * sc * cl, waist[1] + .1 * sc, .09 * sc, .07 * sc);
    g.restore();
  }
  // arms
  const ac = coat || lc;
  taper(g, shL, elL, .065 * W_, .05 * W_, ac); taper(g, elL, hdL, .05 * W_, .035 * W_, coat ? lc : ac);
  taper(g, shR, elR, .065 * W_, .05 * W_, ac); taper(g, elR, hdR, .05 * W_, .035 * W_, coat ? lc : ac);
  // neck + head (an egg, tilted with the head angle)
  taper(g, neck, seg(neck, pose.lean + pose.head, .08, true), .045 * sc, .04 * sc, headC || color);
  g.save(); g.translate(head[0], head[1]); g.rotate(pose.lean + pose.head); g.fillStyle = headC || color;
  g.beginPath(); g.ellipse(0, -.01 * sc, .085 * sc, .11 * sc, 0, 0, TAU); g.fill(); g.restore();
  return { hip, neck, head, hdL, hdR, elL, elR, knL, knR, ftL, ftR, shL, shR, waist };
}
// a researcher: coat, faceless head, optional lantern (light layer)
function researcher(pose, x, y, sc, { lantern = 0, t = 0, hand = 'R', rim = 1, rimC = 'rgba(255,150,80,.9)', coat = '#2b2d44', lit = false, face = 0, glint = .6 } = {}) {
  // backlit by default: a warm rim on the right edge, dark body; lit = pale coat facing the light
  if (rim && !lit) figure(s, pose, { x: x + 4 * rim * sc / 200, y: y - 1, sc, color: rimC, coat: rimC, headC: rimC });
  const J = figure(s, pose, { x, y, sc, color: '#0b0c16', coat: lit ? P.coat : coat, headC: lit ? '#3a3a48' : '#12131f' });
  // round glasses: two small glints. face: -1 looking left, 0 at camera, 1 right; face = null hides them (seen from behind)
  if (face !== null && glint > 0) for (const d of [-1, 1]) { const gx = J.head[0] + face * .045 * sc + d * .035 * sc * (1 - .5 * Math.abs(face)), gy = J.head[1] - .005 * sc; dot(f, gx, gy, .018 * sc, `rgba(230,240,255,${glint})`); }
  if (lantern > 0) {
    const h = hand === 'R' ? J.hdR : J.hdL; const lx = h[0], ly = h[1] + .1 * sc;
    stroke(s, [h, [lx, ly]], '#111', 2);
    dot(s, lx, ly + .04 * sc, .045 * sc, P.gold);
    glow(lx, ly + .04 * sc, .7 * sc, `rgba(255,180,100,${.22 * lantern})`); glow(lx, ly + .04 * sc, .1 * sc, `rgba(255,235,190,${.9 * lantern})`);
  }
  return J;
}

// ---- keyframes, camera, shake ----
// kf(t, [[t0, v0], [t1, v1], ...], easeFn): values may be numbers or arrays
function kf(t, keys, e = ease.inOut) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) if (t < keys[i][0]) { const [a, va] = keys[i - 1], [b, vb] = keys[i], k = e((t - a) / (b - a)); return Array.isArray(va) ? va.map((v, j) => lerp(v, vb[j], k)) : lerp(va, vb, k); }
  return keys[keys.length - 1][1];
}
// put world point (cx, cy) at screen centre, zoomed and rotated; applies to both layers. Call once per shot, before drawing.
function cam(cx = W / 2, cy = H / 2, zoom = 1, rot = 0) { for (const g of [s, f]) { g.translate(W / 2, H / 2); g.rotate(rot); g.scale(zoom, zoom); g.translate(-cx, -cy); } }
// hand-held shake: [dx, dy], re-rolled 24x/s, decaying hits via amt
function shake(t, amt) { const k = Math.floor(t * 24); return [(hash(k * 3 + 1) - .5) * 2 * amt, (hash(k * 3 + 2) - .5) * 2 * amt]; }
// hit envelope: 1 at t0, decays
const hit = (t, t0, decay = 6) => t < t0 ? 0 : Math.exp(-decay * (t - t0));
// beat time by index (FEAT.beats); downbeats fall on indices ≡ 2 (mod 4)
const BT = (n) => FEAT.beats[n];
function beatAfter(t) { const i = lastBeat(t); return FEAT.beats[i + 1] ?? t; }

// per-frame paint overrides a shot can set (e.g. smear for whip pans)
let PAINT_FRAME = {};
function paintSet(o) { Object.assign(PAINT_FRAME, o); }
function whip(dx, dy) { paintSet({ smear: [dx, dy] }); }

// full-frame effects in screen space (call after cam work is done; they reset the transform)
function screen(fn) { for (const g of [s, f]) { g.save(); g.setTransform(.5, 0, 0, .5, 0, 0); } fn(); for (const g of [s, f]) g.restore(); }
function flash(k, col = '#fff2dc') { if (k > .01) screen(() => { f.save(); f.globalCompositeOperation = 'lighter'; f.globalAlpha = clamp(k); f.fillStyle = col; f.fillRect(-10, -10, W + 20, H + 20); f.restore(); }); }
function blackout(k) { if (k > .01) screen(() => { for (const g of [s, f]) { g.save(); g.globalAlpha = clamp(k); g.fillStyle = '#000'; g.fillRect(-10, -10, W + 20, H + 20); g.restore(); } }); }
// paint everything outside a circle (iris transitions)
function iris(cx, cy, r, col = '#05060d') { screen(() => { for (const g of [s, f]) { g.save(); g.fillStyle = g === f ? '#000' : col; g.beginPath(); g.rect(-10, -10, W + 20, H + 20); g.arc(cx, cy, Math.max(0, r), 0, TAU, true); g.fill('evenodd'); g.restore(); } }); }

// ---- the figure's grooves: beat-synced pose offsets with the song's lazy swing ----
// returns { pose, dx, dy } to apply on top of a base pose; amt scales it
function groove(base, style, t, amt = 1) {
  const ph = beatPhase(t), sw = sway(t), b = bob(t), bp = (t - B0) / BEAT;
  const p = JSON.parse(JSON.stringify(base)); let dx = 0, dy = 0;
  const add = (k, i, v) => { if (i < 0) p[k] += v * amt; else p[k][i] += v * amt; };
  switch (style) {
    case 'idle': add('lean', -1, sw * .03); add('head', -1, sw * .04); dy = -b * 2 * amt; break;
    case 'nod': add('head', -1, .12 * b); add('lean', -1, sw * .04); dy = b * 4 * amt; break;
    case 'sway': dx = sw * 14 * amt; add('lean', -1, -sw * .09); add('head', -1, sw * .12); add('aL', 0, sw * .25); add('aR', 0, sw * .25); dy = b * 5 * amt; break;
    case 'bounce': dy = b * 14 * amt; add('lL', 0, b * .25); add('lL', 1, -b * .45); add('lR', 0, -b * .25); add('lR', 1, b * .45); add('aL', 1, b * .4); add('aR', 1, -b * .4); add('head', -1, b * .15); break;
    case 'funk': dy = b * 12 * amt; add('lean', -1, sw * .12); add('aL', 0, .6 + sw * .5); add('aL', 1, 1.6 + b * .6); add('aR', 0, -.6 + sw * .5); add('aR', 1, -1.6 - b * .6); add('lL', 0, .25 + b * .2); add('lR', 0, -.25 - b * .2); add('head', -1, -sw * .2); break;
    case 'walk': { const w = Math.sin(bp * Math.PI); add('lL', 0, .45 * w); add('lR', 0, -.45 * w); add('lL', 1, .35 * Math.max(0, -w)); add('lR', 1, .35 * Math.max(0, w)); add('aL', 0, -.3 * w); add('aR', 0, .3 * w); dy = -Math.abs(w) * 6 * amt; break; }
    case 'row': { const r = Math.sin(bp * Math.PI * .5); add('lean', -1, .25 * r); add('aL', 0, -1.2 - .5 * r); add('aR', 0, 1.2 + .5 * r); add('aL', 1, -1.6); add('aR', 1, 1.6); break; }
  }
  return { pose: p, dx, dy };
}

// ---- the embers: the 171, as little flame creatures ----
// x,y = base of the flame; sz = height px; o: { eyes (0..1), look [-1..1], mood: 'calm'|'scared'|'happy'|'fierce', hue 0..1 (0 = gold, 1 = red), seed, lit (0..1) }
function ember(t, x, y, sz, o = {}) {
  if (!(sz > .5)) return;
  const { eyes = 1, look = 0, mood = 'calm', hue = .3, seed = 0, lit = 1, lean = 0 } = o;
  const fl = Math.sin(t * 9 + seed * 7) * .08 + Math.sin(t * 5.3 + seed) * .06;
  const r = lerp(255, 235, hue), g = lerp(200, 70, hue), bb = lerp(90, 30, hue);
  const tip = [x + (lean + fl) * sz, y - sz * (1 + fl * .5)];
  s.fillStyle = `rgb(${r * .85 | 0},${g * .7 | 0},${bb * .6 | 0})`;
  s.beginPath(); s.moveTo(tip[0], tip[1]); s.bezierCurveTo(x + sz * .55, y - sz * .45, x + sz * .42, y, x, y); s.bezierCurveTo(x - sz * .42, y, x - sz * .55, y - sz * .45, tip[0], tip[1]); s.fill();
  f.save(); f.globalCompositeOperation = 'lighter';
  f.fillStyle = `rgba(${r},${g},${bb},${(eyes > 0 && sz > 10 ? .22 : .55) * lit})`; f.beginPath(); f.moveTo(tip[0], tip[1]); f.bezierCurveTo(x + sz * .45, y - sz * .4, x + sz * .32, y, x, y); f.bezierCurveTo(x - sz * .32, y, x - sz * .45, y - sz * .4, tip[0], tip[1]); f.fill();
  f.restore();
  glow(x, y - sz * .35, sz * 1.1, `rgba(${r},${g},${bb},${.28 * lit})`);
  if (eyes > 0 && sz > 10) {
    const ey = y - sz * .32, ex = sz * .13, lx = look * sz * .06;
    const h2 = mood === 'scared' ? .09 : mood === 'happy' ? .03 : mood === 'fierce' ? .04 : .06;
    for (const d of [-1, 1]) { s.fillStyle = '#2a0c04'; s.beginPath(); s.ellipse(x + d * ex + lx, ey, sz * .05, sz * h2 * eyes, 0, 0, TAU); s.fill(); }
    if (mood === 'fierce') stroke(s, [[x - ex * 1.8 + lx, ey - sz * .12], [x + lx, ey - sz * .05], [x + ex * 1.8 + lx, ey - sz * .12]], '#2a0c04', sz * .04);
  }
}

// ---- chapters and shots ----
// chapter(name, start, end, [[t0, fn], [t1, fn], ...]); fn(t, lt, dur) paints the entire frame. Cuts land on each t0.
const SHOTS = [];
function shot(name, a, b, fn, o = {}) { SHOTS.push({ name, a, b, fn, o }); }
function chapter(name, a, b, list, o = {}) {
  list.forEach(([t0, fn, so = {}], i) => { const t1 = i + 1 < list.length ? list[i + 1][0] : b; shot(`${name}:${fn.name || i}`, t0, t1, (t, p, lt) => fn(t, lt, t1 - t0), { paint: so.paint || o.paint, xin: so.xin }); });
}

// ---- light occlusion ----
// The light layer is additive and ignores depth, so a glow drawn behind a body shows through it.
// To block it, repaint the silhouette on f in black AFTER the light it should hide:
//   occlude(() => figure(f, pose, { x, y, sc, color: '#000', coat: coat ? '#000' : null, headC: '#000' }))
// Inside the callback draw on `f` with '#000' fills (poly(f, pts, '#000'), dot(f, …, '#000'), etc.).
function occlude(fn) { f.save(); f.globalCompositeOperation = 'source-over'; f.globalAlpha = 1; fn(); f.restore(); }
