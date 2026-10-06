/* hairline free sha256:84247511ecde01aa61d789e12818c6e3f83fd7ce50b9518fff47fe1f442173fb */
/*
 * FR: free-form drawing on top of HL. Same palette, same clocks, same rules;
 * what changes is that a figure no longer has to be an isometric solid.
 * Use FR next to HL: HL still owns mk, spring, tween, register, pointer, disposer.
 * The viewBox is still 400 × 320, and a figure that does not use Cam may work
 * in plain screen units.
 *
 * Cameras
 *   Plan(S)                    Cam(0, 1, S): looking straight down; x right, y down, z ignored. proj and unproj both work
 *   Cam(0, 0, S)              (HL) an elevation: x right, z up, y ignored. proj works, unproj does not
 * Curves
 *   curve(points, closed, t)   a smooth path through screen points (Catmull-Rom as cubic Béziers); t is the tension, default 0.5
 *   arc(cx, cy, r, a0, a1, n)  n + 1 points on a circle's arc, angles in degrees
 *   ellipse(cx, cy, rx, ry, n) a closed ring of n points
 *   blob(cx, cy, r, seed, amp, n)   an organic closed ring: the circle's radius wobbles by amp (0..1) from smooth noise
 * Points
 *   morph(a, b, t)             point list a toward point list b, same length
 *   resample(points, n)        n points at equal distance along an open polyline
 *   along(points, t)           the point at t (0..1) of the polyline's length
 *   grid(cols, rows, w, h, x, y)   cols × rows points filling a w × h box at (x, y)
 * Chance
 *   rnd(seed)                  a function returning the same sequence in [0, 1) for the same seed
 *   noise(x, y, seed)          smooth value noise in [0, 1), continuous in x and y
 * Drawing
 *   layer(parent)              a group
 *   xf(g, x, y, deg, s)        moves, turns (degrees) and scales a group; the only way a figure transforms
 *   reveal(path, t)            draws a path on: t 0 is nothing, 1 is all of it
 *   cut(path, a, b)            shows only the stretch of a path from a to b (0..1)
 *   path(parent, d, cls)       one path with a class from the palette
 */
var FR = (() => {
  const { Cam, mk, r2, lerp, rad } = HL;
  const Plan = (S) => Cam(0, 1, S);
  const pt = (p) => r2(p[0]) + " " + r2(p[1]);

  function curve(pts, closed = false, t = 0.5) {
    const n = pts.length;
    if (n < 2) return "";
    const at = (i) => closed ? pts[((i % n) + n) % n] : pts[Math.max(0, Math.min(n - 1, i))];
    let d = "M" + pt(pts[0]);
    const last = closed ? n : n - 1;
    for (let i = 0; i < last; i++) {
      const a = at(i - 1), b = at(i), c = at(i + 1), e = at(i + 2), k = t / 3 * 2;
      const c1 = [b[0] + (c[0] - a[0]) * k / 2, b[1] + (c[1] - a[1]) * k / 2];
      const c2 = [c[0] - (e[0] - b[0]) * k / 2, c[1] - (e[1] - b[1]) * k / 2];
      d += "C" + pt(c1) + " " + pt(c2) + " " + pt(c);
    }
    return closed ? d + "Z" : d;
  }
  const arc = (cx, cy, r, a0, a1, n = 24) =>
    Array.from({ length: n + 1 }, (_, k) => {
      const a = rad(lerp(a0, a1, k / n));
      return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
    });
  const ellipse = (cx, cy, rx, ry, n = 48) =>
    Array.from({ length: n }, (_, k) => {
      const a = k / n * Math.PI * 2;
      return [cx + rx * Math.cos(a), cy + ry * Math.sin(a)];
    });

  function rnd(seed) {
    let s = (seed * 2654435761) >>> 0 || 1;
    return () => {
      s ^= s << 13; s >>>= 0;
      s ^= s >>> 17;
      s ^= s << 5; s >>>= 0;
      return s / 4294967296;
    };
  }
  const hash = (i, j, seed) => {
    let h = (i * 374761393 + j * 668265263 + seed * 2246822519) >>> 0;
    h = (h ^ (h >>> 13)) * 1274126177 >>> 0;
    return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
  };
  function noise(x, y, seed = 0) {
    const i = Math.floor(x), j = Math.floor(y), fx = x - i, fy = y - j;
    const u = fx * fx * (3 - 2 * fx), v = fy * fy * (3 - 2 * fy);
    return lerp(lerp(hash(i, j, seed), hash(i + 1, j, seed), u), lerp(hash(i, j + 1, seed), hash(i + 1, j + 1, seed), u), v);
  }
  const blob = (cx, cy, r, seed = 1, amp = 0.2, n = 48) =>
    Array.from({ length: n }, (_, k) => {
      const a = k / n * Math.PI * 2, m = Math.min(amp, 1);
      const q = r * (1 + m * (noise(Math.cos(a) * 1.4 + 5, Math.sin(a) * 1.4 + 5, seed) * 2 - 1));
      return [cx + q * Math.cos(a), cy + q * Math.sin(a)];
    });

  const morph = (a, b, t) => a.map((p, i) => [lerp(p[0], b[i][0], t), lerp(p[1], b[i][1], t)]);
  const lens = (pts) => {
    const out = [0];
    for (let i = 1; i < pts.length; i++) out.push(out[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]));
    return out;
  };
  function along(pts, t) {
    const L = lens(pts), total = L[L.length - 1], want = Math.max(0, Math.min(1, t)) * total;
    let i = 1;
    while (i < L.length - 1 && L[i] < want) i++;
    const span = L[i] - L[i - 1] || 1, f = (want - L[i - 1]) / span;
    return [lerp(pts[i - 1][0], pts[i][0], f), lerp(pts[i - 1][1], pts[i][1], f)];
  }
  const resample = (pts, n) => Array.from({ length: n }, (_, k) => along(pts, n === 1 ? 0 : k / (n - 1)));
  const grid = (cols, rows, w, h, x = 0, y = 0) => {
    const out = [];
    for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++)
      out.push([x + (cols === 1 ? w / 2 : i / (cols - 1) * w), y + (rows === 1 ? h / 2 : j / (rows - 1) * h)]);
    return out;
  };

  const layer = (parent) => mk("g", {}, parent);
  const xf = (g, x = 0, y = 0, deg = 0, s = 1) =>
    g.setAttribute("transform", `translate(${r2(x)} ${r2(y)}) rotate(${r2(deg)}) scale(${r2(s)})`);
  const path = (parent, d, cls) => mk("path", { d, class: cls || "sil nf" }, parent);
  function cut(el, a, b) {
    el.setAttribute("pathLength", "1");
    const lo = Math.max(0, Math.min(1, a)), hi = Math.max(lo, Math.min(1, b));
    el.style.strokeDasharray = `0 ${r2(lo)} ${r2(hi - lo)} 2`;
    el.style.strokeDashoffset = "0";
    el.style.visibility = hi > lo ? "visible" : "hidden";
  }
  const reveal = (el, t) => cut(el, 0, t);

  return { Plan, curve, arc, ellipse, blob, morph, resample, along, grid, rnd, noise, layer, xf, reveal, cut, path };
})();
