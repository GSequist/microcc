#!/usr/bin/env python3
"""Scaffold a new painted-video project from this skill's vendored renderer.

Copies js/paint.js + js/lib.js (patching lib.js's DUR/BEAT/B0 constants to the
given song), writes index.html/main.js/data.js/package.json, and stub chapter
files so the project boots and plays a blank scene before any chapter is written.

Usage:
  python scaffold.py <project_dir> --audio song.mp3 --duration 180 --bpm 120 \
      [--first-beat 0.4] [--chapters 3] [--title "My Video"]
"""
import argparse
import re
import shutil
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

MAIN_JS = """// Frame assembly + live player + offline hooks.
function renderFrame(t) {
  bindCtx();
  for (const g of [s, f]) { g.setTransform(.5, 0, 0, .5, 0, 0); g.globalAlpha = 1; g.globalCompositeOperation = 'source-over'; g.filter = 'none'; }
  fill(s, P.night0); fill(f, '#000');
  let paint = {}; PAINT_FRAME = {};
  for (let k = 0; k < SHOTS.length; k++) {
    const sh = SHOTS[k], nx = SHOTS[k + 1];
    const end = sh.b + (nx && nx.o.xin && Math.abs(nx.a - sh.b) < .01 ? nx.o.xin : 0);
    if (t < sh.a || t >= end) continue;
    const a = sh.o.xin ? smooth((t - sh.a) / sh.o.xin) : 1;
    for (const g of [s, f]) { g.save(); g.globalAlpha = a; }
    try { sh.fn(t, clamp((t - sh.a) / (sh.b - sh.a)), t - sh.a); } catch (e) { console.error(sh.name, e); }
    for (const g of [s, f]) g.restore();
    const pc = typeof sh.o.paint === 'function' ? sh.o.paint(t) : (sh.o.paint || {});
    paint = a >= 1 ? pc : { ...paint, ...Object.fromEntries(Object.entries(pc).map(([key, v]) => [key, typeof v === 'number' ? lerp(paint[key] ?? v, v, a) : v])) };
  }
  Paint.present(t, { ...paint, ...PAINT_FRAME });
}

const canvas = document.getElementById('c'); canvas.width = W; canvas.height = H;
Paint.init(canvas);
const params = new URLSearchParams(location.search);
window.__frameJPEG = (t, q = .93) => { renderFrame(t); return canvas.toDataURL('image/jpeg', q); };
window.__ready = Promise.resolve(true);

if (!params.has('offline')) {
  const audio = document.getElementById('a');
  const bar = document.getElementById('bar'), fillEl = document.getElementById('fill'), tl = document.getElementById('time'), play = document.getElementById('play');
  let scrubT = params.has('t') ? parseFloat(params.get('t')) : 0; audio.currentTime = scrubT;
  const fmt = (x) => `${Math.floor(x / 60)}:${String(Math.floor(x % 60)).padStart(2, '0')}`;
  (function loop() { const t = audio.paused ? (audio.currentTime || scrubT) : audio.currentTime; renderFrame(Math.min(t, DUR - .01)); fillEl.style.width = (t / DUR * 100) + '%'; tl.textContent = `${fmt(t)} / ${fmt(DUR)}`; requestAnimationFrame(loop); })();
  const toggle = () => { if (audio.paused) { audio.play(); document.body.classList.add('playing'); } else { audio.pause(); document.body.classList.remove('playing'); } };
  play.onclick = toggle; canvas.onclick = toggle; audio.onended = () => document.body.classList.remove('playing');
  const seek = (e) => { const r = bar.getBoundingClientRect(); audio.currentTime = scrubT = clamp((e.clientX - r.left) / r.width) * DUR; };
  bar.addEventListener('pointerdown', (e) => { seek(e); const mv = (ev) => seek(ev); window.addEventListener('pointermove', mv); window.addEventListener('pointerup', () => window.removeEventListener('pointermove', mv), { once: true }); });
  window.addEventListener('keydown', (e) => {
    if (e.code === 'Space') { e.preventDefault(); toggle(); }
    if (e.code === 'ArrowRight') audio.currentTime = scrubT = Math.min(DUR, audio.currentTime + 5);
    if (e.code === 'ArrowLeft') audio.currentTime = scrubT = Math.max(0, audio.currentTime - 5);
    if (e.code === 'KeyF') (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen());
  });
  let idle; const wake = () => { document.body.classList.add('awake'); clearTimeout(idle); idle = setTimeout(() => document.body.classList.remove('awake'), 2200); };
  window.addEventListener('pointermove', wake); window.addEventListener('touchstart', wake); wake();
}
"""

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{title}</title>
<style>
  :root {{ --bg: #070608; --fg: #f2e9d6; --accent: #ff4a1c; }}
  html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--fg); overflow: hidden; }}
  body {{ display: grid; place-items: center; font-family: "IBM Plex Mono", monospace; }}
  canvas {{ width: 100vw; height: 100vh; object-fit: contain; display: block; cursor: pointer; }}
  #ui {{ position: fixed; left: 0; right: 0; bottom: 0; padding: 18px 16px calc(16px + env(safe-area-inset-bottom)); display: flex; align-items: center; gap: 14px;
        background: linear-gradient(transparent, rgba(0,0,0,.7)); opacity: 0; transition: opacity .5s; }}
  body.awake #ui, body:not(.playing) #ui {{ opacity: 1; }}
  #play {{ all: unset; cursor: pointer; width: 40px; height: 40px; border-radius: 50%; border: 1px solid rgba(242,233,214,.5); display: grid; place-items: center; flex: none; }}
  #play::before {{ content: ""; border-left: 12px solid var(--fg); border-top: 7px solid transparent; border-bottom: 7px solid transparent; margin-left: 3px; }}
  body.playing #play::before {{ border: none; width: 10px; height: 14px; margin: 0; background: linear-gradient(90deg, var(--fg) 35%, transparent 35% 65%, var(--fg) 65%); }}
  #bar {{ flex: 1; height: 22px; display: flex; align-items: center; cursor: pointer; touch-action: none; }}
  #bar > div {{ width: 100%; height: 2px; background: rgba(242,233,214,.2); position: relative; }}
  #fill {{ position: absolute; left: 0; top: 0; bottom: 0; background: var(--accent); }}
  #time {{ font-size: 12px; letter-spacing: .08em; opacity: .7; flex: none; font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="ui"><button id="play" aria-label="Play / pause"></button><div id="bar"><div><div id="fill"></div></div></div><span id="time">0:00</span></div>
<audio id="a" src="{audio}" preload="auto"></audio>
<script src="js/data.js"></script>
<script src="js/paint.js"></script>
<script src="js/lib.js"></script>
{chapter_tags}
<script src="js/main.js"></script>
</body>
</html>
"""

PACKAGE_JSON = """{{
  "name": "{name}",
  "private": true,
  "type": "module",
  "scripts": {{
    "check": "node check.mjs",
    "render": "node render.mjs 0 {duration} out/{name}.mp4 {audio}"
  }},
  "devDependencies": {{ "playwright": "^1.58.0" }}
}}
"""

CHAPTER_STUB = """// chapter {n} — write shots here. chapter(name, tStart, tEnd, [[t0, fn], ...])
// See RENDERER.md for shot()/chapter()/cam()/figure() and drawing helpers (s, f, fill, glow, ...).
chapter('c{n}', {t0}, {t1}, [
  [{t0}, function placeholder(t, lt, dur) {{
    cam();
    // TODO: paint this shot
  }}],
]);
"""


def gen_beats(duration: float, bpm: float, first_beat: float) -> list[float]:
    step = 60.0 / bpm
    n = int((duration - first_beat) / step)  # index-based to avoid float drift
    return [round(first_beat + i * step, 3) for i in range(n)]


def patch_lib_js(text: str, duration: float, bpm: float, first_beat: float) -> str:
    beat = 60.0 / bpm
    text = re.sub(r"const DUR = [^;]+;", f"const DUR = {duration};", text, count=1)
    text = re.sub(
        r"const BEAT = [^,]+, B0 = [^;]+;.*",
        f"const BEAT = {beat}, B0 = {first_beat}; // {bpm} bpm grid",
        text,
        count=1,
    )
    return text


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("project_dir")
    ap.add_argument("--audio", required=True, help="path to the song file, copied into assets/")
    ap.add_argument("--duration", type=float, required=True, help="song length in seconds")
    ap.add_argument("--bpm", type=float, default=120.0)
    ap.add_argument("--first-beat", type=float, default=0.0, help="time of beat 0, seconds")
    ap.add_argument("--chapters", type=int, default=1)
    ap.add_argument("--title", default="Untitled")
    args = ap.parse_args()

    proj = Path(args.project_dir)
    (proj / "js" / "ch").mkdir(parents=True, exist_ok=True)
    (proj / "assets").mkdir(exist_ok=True)
    (proj / "out").mkdir(exist_ok=True)

    audio_src = Path(args.audio)
    audio_dst = proj / "assets" / audio_src.name
    if audio_src.resolve() != audio_dst.resolve():
        shutil.copyfile(audio_src, audio_dst)
    audio_rel = f"assets/{audio_src.name}"

    shutil.copyfile(SKILL_DIR / "lib" / "paint.js", proj / "js" / "paint.js")
    lib_js = (SKILL_DIR / "lib" / "lib.js").read_text()
    lib_js = patch_lib_js(lib_js, args.duration, args.bpm, args.first_beat)
    (proj / "js" / "lib.js").write_text(lib_js)

    shutil.copyfile(SKILL_DIR / "scripts" / "render.mjs", proj / "render.mjs")
    shutil.copyfile(SKILL_DIR / "scripts" / "check.mjs", proj / "check.mjs")

    (proj / "js" / "main.js").write_text(MAIN_JS)

    beats = gen_beats(args.duration, args.bpm, args.first_beat)
    data_js = (
        f"// Manual beat grid ({args.bpm} bpm, first beat {args.first_beat}s). "
        f"Replace with TIMING.md's pipeline output for real vocal/lyric sync.\n"
        f"const FEAT = {{ fps: 30, beats: {beats} }};\n"
        f"const LYRICS = [];\n"
    )
    (proj / "js" / "data.js").write_text(data_js)

    chapter_tags = []
    for n in range(1, args.chapters + 1):
        t0 = round((n - 1) * args.duration / args.chapters, 2)
        t1 = round(n * args.duration / args.chapters, 2)
        (proj / "js" / "ch" / f"c{n}.js").write_text(CHAPTER_STUB.format(n=n, t0=t0, t1=t1))
        chapter_tags.append(f'<script src="js/ch/c{n}.js"></script>')

    (proj / "index.html").write_text(
        INDEX_HTML.format(title=args.title, audio=audio_rel, chapter_tags="\n".join(chapter_tags))
    )

    name = re.sub(r"[^a-z0-9-]+", "-", args.title.lower()).strip("-") or "painted-video"
    (proj / "package.json").write_text(
        PACKAGE_JSON.format(name=name, duration=args.duration, audio=audio_rel)
    )

    print(f"scaffolded {proj}")
    print(f"next: cd {proj} && bash <skill>/scripts/install_stack.sh && open index.html")


if __name__ == "__main__":
    main()
