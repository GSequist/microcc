# Renderer API

Read `lib/paint.js` and `lib/lib.js` directly for exact signatures — this is
the conceptual map, not a restatement of the code.

## The look

Every shot draws two Canvas2D layers at half-res (960x540): `s` (the
underpainting — shapes, gradients, silhouettes) and `f` (the light layer —
glows, lanterns, embers, additive only). `Paint.present(t, opts)` then:

1. Uploads both as WebGL2 textures.
2. Redraws the underpainting with ~60,000 instanced, textured brushstrokes in
   three size layers (`LAYERS` in `paint.js`) — each stroke samples its colour
   from the underpainting under it and aligns to the local edge; in flat
   regions it follows a Perlin flow field instead (the swirl you see in skies
   and water).
3. Adds the light layer on top with bloom, then canvas weave + grain + vignette.

Strokes re-roll a few times a second (`opts.boil`, default 8/s) — that's what
reads as hand-painted rather than static. A whip pan calls `whip(dx, dy)` to
smear every stroke along the motion direction for that frame.

**`Paint.present(t, opts)` fields**: `boil` (paint fps), `grain`, `vig`
(vignette strength), `bloom`, `strokeK` (size multiplier), `flowK` (flow-field
strength), `layers` (bool mask to disable a size layer), `smear` (`[dx,dy]`,
usually set via `whip()` rather than directly).

## Shot system

```js
shot(name, tStart, tEnd, fn, opts)
chapter(name, tStart, tEnd, [[t0, fn, shotOpts], ...], opts)
```

`chapter()` is the one you actually call — it turns a list of `[startTime, fn]`
pairs into shots, each running until the next one starts (or `tEnd`). Every
`fn(t, localProgress, localTime)` paints the **entire** frame from scratch —
there's no persistent state between frames, only `t`. `opts.xin` cross-fades
in from the previous shot instead of a hard cut. `main.js` walks `SHOTS` every
frame, calls whichever one is active, and hands the merged result to
`Paint.present`.

## Camera

```js
cam(cx, cy, zoom, rot)   // call once per shot, before drawing, on both s and f
```

World space is always 1920x1080 regardless of zoom/rotation — `cam()` just
transforms the canvas context. `shake(t, amt)` gives hand-held jitter,
`kf(t, [[t0,v0],[t1,v1],...], easeFn)` keyframes any numeric or array value
(position, zoom, whatever) across the shot.

## Drawing helpers (all in 1920x1080 world coords)

`fill`, `vgrad`/`rgrad` (gradients), `glow` (soft additive light on `f`),
`dot`, `poly`, `stroke`, `ridge` (a noisy hill silhouette), `bez`/`at` (bezier
sampling). `bindCtx()` sets the module-level `s`/`f` each frame — already
called by `main.js`, you don't call it yourself inside a shot.

## The figure rig

```js
figure(g, pose, { x, y, sc, color, coat, headC, width, limbC })
```

A procedural mannequin: torso, tapered limbs, an egg head, returns every
joint position (`hip`, `neck`, `head`, `shL/shR`, `elL/elR`, `hdL/hdR`,
`knL/knR`, `ftL/ftR`, `waist`) so you can attach things (a lantern, a held
object) to a hand. `POSE` has named presets (`stand`, `reach`, `write`,
`dance1`, …); `mixPose(a, b, k)` blends two. `groove(basePose, style, t, amt)`
adds beat-synced sway/bounce/walk offsets on top of a base pose, driven by the
song's beat grid (`sway`/`bob`/`beatPhase` in `lib.js`).

`researcher()` and `ember()` in `lib.js` are the **source song's own
characters** — a lab-coated figure and little flame creatures — vendored as a
worked example of the pattern (draw silhouette on `s`, glow on `f`, occlude
where something should block light behind it), not as a generic library. A
new project writes its own character functions the same way; don't assume
`researcher`/`ember` fit an unrelated story.

## Beat/timing globals

`js/data.js` defines `FEAT` (`fps`, `beats: [...]`, optionally `rms`/`low`/
`mid`/`high`/`onset`/`perc`/`cent` arrays) and `LYRICS`. `lastBeat`,
`beatPulse`, `beatAfter`, `BT(n)` all read `FEAT.beats`. `F(name, t, smooth)`
samples a named feature array at time `t`. `when(word, after)` looks up a
lyric word's start time — only works if `LYRICS` has real word timing (Stage
6); a scaffolded manual-beat-grid project has `LYRICS = []` and can't use it.

`BEAT`/`B0` in `lib.js` are the tempo/first-beat constants `sway()`/`bob()`/
`beatPhase()` use — `scaffold.py` patches these from `--bpm`/`--first-beat`.
If you hand-edit `js/data.js` later (e.g. after running Stage 6), update these
two constants too if the tempo changed.

## Occlusion

The light layer is additive and ignores depth — a glow drawn behind a figure
shows through it. To block it, repaint the silhouette on `f` in black *after*
the light that should be hidden, inside `occlude(fn)`.

## Why this design

Every frame is `paint(t)`, nothing else — no simulation state, no
frame-to-frame dependency. That's what makes offline rendering trivially
parallel (`render.mjs` opens N pages and gives each a disjoint slice of `t`)
and makes any single moment reproducible for review (`check.mjs` renders one
frame in isolation, same as the live player would at that `t`).
