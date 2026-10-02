---
name: painted-video
description: "Use when the user wants a painted/brushstroke-style animation synced to music: a real music video (not a typographic lyric video), or any WebGL2 canvas animation keyed to song time. Not for generic web animation, slide decks, or editing existing video footage. Renders offline via Playwright-driven Chrome + ffmpeg; system tools are installed on demand (see Install), nothing here touches pyproject.toml."
---

# Painted Video

Every frame is a pure function of song-time `t`. It plays live in a browser and
renders offline by driving real Chrome in parallel and piping frames to ffmpeg.
Renderer, offline pipeline and figure rig are ported from
[functional-emotions-video](https://github.com/ledbetterljoshua/functional-emotions-video)
(MIT — see `LICENSE-lib`); the story, chapters and characters are yours.

This is Node/Chrome/ffmpeg shelled out at run time, not a Python dependency —
adding this skill did not touch `pyproject.toml` and never will. See **Install**.

## Stages

| Stage | What happens | Output |
|---|---|---|
| 0 — Install | Confirm with the user, then install whatever's missing | Node, ffmpeg, npm `playwright` + Chrome |
| 1 — Scaffold | Copy the renderer + generate a starter project | `index.html`, `js/`, `package.json` |
| 2 — Storyboard | Shot-by-shot plan: story beats, timing, cuts | `STORYBOARD.md` |
| 3 — Chapters | Paint each chapter's shots | `js/ch/cN.js` |
| 4 — Preview | Contact-sheet specific times, catch errors before a full render | `out/sheet.jpg` |
| 5 — Render | Full offline render | `out/video.mp4` |
| 6 — Timing (optional) | Only if syncing to real vocals/lyrics you don't already have timing for | `js/data.js` |

## Stage 0 — Install

Check first, install only what's missing:

```bash
node -v; ffmpeg -version; ls node_modules/playwright 2>/dev/null
```

If anything's missing, tell the user what you're about to install (Homebrew/apt
packages, an npm package, a Chrome download via `npx playwright install`) and
confirm before running — these touch the system, not just the project:

```bash
bash <skill>/scripts/install_stack.sh
```

Idempotent — safe to re-run. It never installs anything for Stage 6 (see below).

## Stage 1 — Scaffold

```bash
python <skill>/scripts/scaffold.py <project_dir> --audio song.mp3 --duration 180 \
  --bpm 120 --first-beat 0.4 --chapters 3 --title "My Video"
```

Copies `lib/paint.js` and `lib/lib.js` (patching `lib.js`'s `DUR`/`BEAT`/`B0`
constants to your song — those three are the one place original song tempo was
hardcoded into otherwise generic code), copies `render.mjs`/`check.mjs`,
and writes `index.html`, `js/main.js`, a manual-beat-grid `js/data.js`, stub
`js/ch/c1.js…cN.js`, and `package.json`. The scaffolded project is self-contained
— it doesn't depend on this skill's path afterward.

Get `--bpm`/`--first-beat` by ear or `ffprobe`/a DAW; exact word-level lyric
sync is Stage 6, optional, and not needed for a beat-synced video.

## Stage 2 — Storyboard

Read `<skill>/RENDERER.md` first — the API you're writing against. Write
`STORYBOARD.md`: a shot list with timestamps, one action per shot, camera
always moving, cuts landing on beats (`FEAT.beats` from `js/data.js`), hits
motivated by the music. Short shots read better than long static ones — the
source project's lesson, credited in `RENDERER.md`, was exactly this.

## Stage 3 — Chapters

Each `js/ch/cN.js` calls `chapter(name, tStart, tEnd, [[t0, fn], ...])` — see
`RENDERER.md` for the full shot/camera/figure/drawing API. For a video with
several independent chapters, brief one subagent per chapter from the same
storyboard + a shared style brief, and review each one's contact sheet
(Stage 4) before accepting it — same pattern the source project used for its
7 parallel chapters.

## Stage 4 — Preview

```bash
node check.mjs out/sheet.jpg 12.5 30 47.2      # contact sheet of specific times
node check.mjs --full out/stills 12.5 30       # full-res stills instead
```

Prints ms/frame and any console/page errors from those frames. Fix issues here
— cheap and fast — before a full render.

## Stage 5 — Render

```bash
node render.mjs <t0> <t1> out/video.mp4 assets/song.mp3 [workers] [fps]
```

Drives Chrome on the real GPU (`--use-angle=metal` on macOS). The bundled
headless shell falls back to software GL and is roughly 20x slower — if a
render is unexpectedly slow, that's almost always why; check `channel: 'chrome'`
resolved to a real Chrome install.

## Stage 6 — Timing (optional, heavy — opt in explicitly)

Only if you need real vocal/word-level lyric sync and don't already have a
beat grid or timing data. **Never install this automatically** — it's a
multi-GB ML stack (Demucs, Whisper, torchaudio) for a one-time preprocessing
step, the opposite of this skill's "install what's needed, nothing more"
approach. Read `TIMING.md` before touching it.

## Dependencies

- Node.js + npm — scaffold/render runtime
- npm `playwright` (JS package — distinct from micro-cc's own Python
  `playwright` dependency) + a real Chrome (`npx playwright install chrome`)
- ffmpeg — frame→mp4 encode, contact sheets
- (Stage 6 only, opt-in) a separate Python venv per `scripts/analysis/requirements.txt`
  — never installed as part of Stage 0

## Credits

Renderer, offline pipeline and figure rig by Joshua Ledbetter
([ledbetterljoshua/functional-emotions-video](https://github.com/ledbetterljoshua/functional-emotions-video),
MIT). `lib/paint.js` is vendored unmodified. `lib/lib.js`, `render.mjs` and
`check.mjs` are vendored with only the song-specific hardcoding
(tempo/audio path) turned into parameters. `scripts/analysis/*.py` are
vendored unmodified except a path-to-edit note.
