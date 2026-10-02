// Adapted from github.com/ledbetterljoshua/functional-emotions-video (MIT — see ../LICENSE-lib).
// Original hardcoded the audio path; here it's an argv so the skill works per-project.
//
// node render.mjs <t0> <t1> <out.mp4> <audio.mp3> [workers] [fps]
import { chromium } from 'playwright';
import fs from 'fs'; import { execSync } from 'child_process';
const [t0, t1] = [Number(process.argv[2]), Number(process.argv[3])]; const out = process.argv[4];
const audio = process.argv[5];
const WK = Number(process.argv[6] || 6), FPS = Number(process.argv[7] || 30);
if (!out || !audio) { console.error('usage: node render.mjs <t0> <t1> <out.mp4> <audio.mp3> [workers] [fps]'); process.exit(1); }
const dir = `out/frames_${t0}_${t1}`; fs.rmSync(dir, { recursive: true, force: true }); fs.mkdirSync(dir, { recursive: true });
const n = Math.round((t1 - t0) * FPS); let done = 0; const start = Date.now();
const b = await chromium.launch({ channel: 'chrome', headless: true, args: ['--use-angle=metal', '--ignore-gpu-blocklist', '--enable-gpu'] });
await Promise.all([...Array(WK)].map(async (_, w) => {
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  await p.goto('file://' + process.cwd() + '/index.html?offline=1'); await p.evaluate(() => window.__ready);
  for (let i = w; i < n; i += WK) {
    const url = await p.evaluate((t) => window.__frameJPEG(t, .92), t0 + i / FPS);
    fs.writeFileSync(`${dir}/f_${String(i).padStart(6, '0')}.jpg`, Buffer.from(url.split(',')[1], 'base64'));
    if (++done % 300 === 0) console.log(`${done}/${n} ${((Date.now() - start) / 1000).toFixed(0)}s`);
  }
}));
await b.close();
execSync(`ffmpeg -y -loglevel error -framerate ${FPS} -i ${dir}/f_%06d.jpg -ss ${t0} -t ${t1 - t0} -i "${audio}" -c:v libx264 -preset medium -crf 17 -pix_fmt yuv420p -c:a aac -b:a 256k -shortest -movflags +faststart ${out}`);
console.log('wrote', out);
