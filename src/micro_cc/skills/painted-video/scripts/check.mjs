// Vendored, unmodified, from github.com/ledbetterljoshua/functional-emotions-video
// (MIT — see ../LICENSE-lib). Already project-generic: no song-specific paths.
//
// node check.mjs <out.jpg> t1 t2 t3 ...   renders those times into a 3-column contact sheet (640x360 tiles), prints ms/frame + errors
// node check.mjs --full <dir> t1 t2 ...   writes full-res stills instead
import { chromium } from 'playwright';
import fs from 'fs'; import path from 'path'; import { execSync } from 'child_process';
const args = process.argv.slice(2); const full = args[0] === '--full'; if (full) args.shift();
const out = args.shift(); const times = args.map(Number);
const tmp = full ? out : fs.mkdtempSync('/tmp/chk_'); fs.mkdirSync(tmp, { recursive: true });
const b = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-angle=metal', '--ignore-gpu-blocklist', '--enable-gpu'] });
const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
const errs = []; p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); }); p.on('pageerror', e => errs.push(String(e)));
await p.goto('file://' + process.cwd() + '/index.html?offline=1'); await p.evaluate(() => window.__ready);
const files = []; let ms = 0;
for (const t of times) {
  const t0 = Date.now(); const url = await p.evaluate((t) => window.__frameJPEG(t, .9), t); ms += Date.now() - t0;
  const f = path.join(tmp, `t_${t.toFixed(2).padStart(6, '0')}.jpg`); fs.writeFileSync(f, Buffer.from(url.split(',')[1], 'base64')); files.push([t, f]);
}
await b.close();
console.log(`${times.length} frames, ${(ms / times.length).toFixed(0)} ms/frame`);
if (!full) {
  const n = files.length, cols = Math.min(3, n), rows = Math.ceil(n / cols), pad = rows * cols - n;
  let inputs = files.map(([, f]) => `-i "${f}"`).join(' '); let filt = files.map(([t], i) => `[${i}:v]scale=640:360,drawtext=text='${t}':x=8:y=8:fontsize=20:fontcolor=white:box=1:boxcolor=black@0.6[v${i}];`).join('');
  for (let k = 0; k < pad; k++) { inputs += ` -f lavfi -i color=black:s=640x360:d=1`; filt += `[${n + k}:v]null[v${n + k}];`; }
  const ins = [...Array(n + pad).keys()].map(i => `[v${i}]`).join('');
  fs.mkdirSync(path.dirname(out) || '.', { recursive: true });
  execSync(`ffmpeg -y -loglevel error ${inputs} -filter_complex "${filt}${ins}xstack=inputs=${n + pad}:grid=${cols}x${rows}[o]" -map "[o]" -frames:v 1 -q:v 3 "${out}"`);
  console.log('sheet', out);
} else console.log('stills in', out);
if (errs.length) console.log('ERRORS:\n' + [...new Set(errs)].slice(0, 20).join('\n'));
