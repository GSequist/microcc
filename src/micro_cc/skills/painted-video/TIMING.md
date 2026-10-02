# Timing pipeline (optional, heavy — Stage 6)

Only needed for real word-level lyric sync from a source song's actual
vocals. A beat-synced video (Stage 1's `scaffold.py --bpm`) does not need any
of this — most videos don't. Never install this stack as part of Stage 0.

## Why it's separate

This is a genuinely heavy ML dependency chain — vocal source separation and
speech transcription models, gigabytes of weights on first run — for a
preprocessing step you run once per song, offline, by hand. Bundling it into
Stage 0's install-on-demand would defeat the point of keeping this skill thin;
it stays fully opt-in and documented here instead.

## Setup

Use a dedicated virtualenv, not the system/micro-cc Python:

```bash
python3 -m venv .venv-timing && source .venv-timing/bin/activate
pip install -r scripts/analysis/requirements.txt
```

`mlx-whisper` is Apple Silicon only; swap for `openai-whisper` in
`transcribe.py` on other platforms (see the comment at the top of that file).

## Pipeline, in order

Each script in `scripts/analysis/` has a hardcoded path near the top marked
`# EDIT:` — point it at your song/output before running. Run all of these
from the project root (the directory with `assets/` and `js/`), not from
inside `scripts/analysis/`.

1. **Separate vocals** (not a vendored script — a Demucs CLI call):
   ```bash
   demucs --two-stems=vocals assets/<song>.mp3 -o analysis/stems
   ```
2. **`python scripts/analysis/features.py`** — beat grid, tempo, section
   boundaries, and coarse audio-energy bands (rms/low/mid/high/onset/perc/cent)
   via librosa. Writes `analysis/features.json`.
3. **`python scripts/analysis/transcribe.py`** — Whisper over the isolated
   vocal stem in 30s overlapping chunks, word-level timestamps. Writes
   `analysis/words_raw.json`.
4. **`python scripts/analysis/align.py`** — Needleman-Wunsch alignment of the
   (noisy, sometimes hallucinated) Whisper transcript against your real lyrics
   in `analysis/lyrics.txt` — this is what makes the sync robust to Whisper
   mishearing a word. Writes `analysis/lyrics_timed.json`.
5. **`python scripts/analysis/build_data.py`** — bundles both into `js/data.js`
   as `window.FEAT`/`window.LYRICS`, **overwriting** the manual-beat-grid
   `js/data.js` that `scaffold.py` generated.

You need `analysis/lyrics.txt` (plain text, one line per lyric line) before
step 4 — write it by hand from the actual song lyrics.

## After running this

`js/data.js` now has real `FEAT.beats` (from actual audio, not a synthetic
grid) and populated `LYRICS` word timing, so `when(word, after)` in `lib.js`
works. If the real tempo differs from what you scaffolded with, also update
`BEAT`/`B0` in `js/lib.js` (see `RENDERER.md` → Beat/timing globals) — those
two constants aren't derived from `FEAT`, they're separate and used directly
by `sway()`/`bob()`/`beatPhase()`.
