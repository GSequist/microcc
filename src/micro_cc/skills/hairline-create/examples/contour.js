/**
 * Contour: nine nested contour lines of a hill, drawn as smooth curves with no
 * camera at all. The pointer sets where the summit is; each ring leans toward
 * it in proportion to its height, so the hill tilts like terrain under a
 * hand. At rest the summit sits off-centre and its ring is the bright one.
 * The slider is how far the summit may pull the rings.
 *
 * The pattern: free-form. FR curves and noise, a spring per coordinate, and a
 * hit test on the fixed viewBox (the pointer is read against the frame, never
 * against the lines that move).
 */
const { clamp, lerp, mk, pointer, register, disposer, spring, stepS } = HL;
const { blob, curve, layer, path } = FR;

const L = 9, CX = 200, CY = 166, REST = [CX + 34, CY - 16];

function mount({ stage, svg, read }, value) {
  const bag = disposer();
  let lean = value, drawn = "";
  const px = spring(REST[0]), py = spring(REST[1]);
  const g = layer(svg);
  const rings = Array.from({ length: L }, (_, k) => {
    const cls = k === L - 1 ? "nf hi" : k < 3 ? "nf lo" : "nf sil";
    return { k, base: blob(0, 0, 138 - k * 14, 7, 0.1 + k * 0.012, 56), el: path(g, "", cls) };
  });

  function draw() {
    const key = [px.x, py.x, lean].map((n) => n.toFixed(2)).join();
    if (key === drawn) return;
    drawn = key;
    for (const r of rings) {
      const t = (r.k / (L - 1)) ** 1.5 * lean;
      const cx = lerp(CX, px.x, t), cy = lerp(CY, py.x, t);
      r.el.setAttribute("d", curve(r.base.map(([x, y]) => [cx + x, cy + y * 0.8]), true));
    }
  }

  const B = register(stage, (dt) => {
    const m = stepS(px, dt) | stepS(py, dt);
    draw();
    return !!m;
  });
  bag.add(B.unregister);

  bag.add(pointer(stage, {
    move: (p) => {
      px.t = clamp(p[0], CX - 70, CX + 70);
      py.t = clamp(p[1], CY - 50, CY + 50);
      read.textContent = `summit ${Math.round((px.t - CX) / 10)}·${Math.round((py.t - CY) / 10)}`;
      B.wake();
    },
    leave: () => { px.t = REST[0]; py.t = REST[1]; read.textContent = "rest"; B.wake(); },
  }));
  bag.add(() => svg.replaceChildren());

  return {
    set: (v) => { lean = v; draw(); B.wake(); },
    destroy: bag.dispose,
  };
}

hairline({
  name: "contour",
  means: "Nine contour lines of a hill lean toward the pointer, which sets where the summit is.",
  rules: [1, 4, 5, 7, 8],
  range: [0.25, 0.5, 0.8],
  mount,
});
