# Blueprint style — tokens and rules

Full spec for the "one-accent Rams blueprint" diagram-slide style used by
Mode B of the `design` skill (see `SKILL.md`). Pastel one-accent Rams/Bauhaus
modernism with technical-drawing apparatus around a free-form diagram.

## Color

```css
--bg: #FFFFFF;
--ink: #17130F;
--muted: #6B6459;
--line: #E4E0D8;

/* accent set — pick exactly ONE per slide */
--sage: #BFCDB6;   --sage-line: #4F5F45;
--blue: #B7CBD9;   --blue-line: #3E5A6C;
--rose: #E3B5AC;   --rose-line: #8C4A3E;
--butter: #E8DBA0; --butter-line: #8A7326;
```

**Rule: one accent, once.** A slide declares only the accent pair it uses (e.g. just
`--sage`/`--sage-line`), not the whole set. That accent fills exactly one element —
the single thing the slide is actually about. Every other shape is ink outline or
`--line` hairline. Restraint is the point, not variety.

If a deck runs multiple slides and needs to visually distinguish categories across
them (not within one slide), a different accent per slide is fine — sage for one
slide's subject, blue for another's. Never two accents filled on the same slide.

## Typography

Two families only, both already system-available (no webfonts, no @font-face needed):

- Body/title: `-apple-system, "Helvetica Neue", "Segoe UI", Arial, sans-serif`
- Mono (labels, eyebrow, dimension text): `ui-monospace, "SF Mono", Menlo, Consolas, monospace`

| Role | Spec |
|---|---|
| Eyebrow (`.num`) | mono, 0.78rem, letter-spacing 0.16em, uppercase, `--muted`. Format: `NN` or `NN · TAG` — a two-digit slide index, optionally a short mono tag. Never a sentence. |
| Title (`.title`) | **lowercase** (not sentence case), weight 200, `clamp(2.5rem, 6vw, 3.75rem)`, letter-spacing 0.01em, `--ink`. |
| Accent swatch | a 10×10px square, fill = accent, 1px border = accent-line, placed immediately before the title in a flex row (`gap: 0.6rem`, `align-items: center`). It is the title's visual footnote — it says "this is the color that matters on this slide" before the reader even reaches the diagram. Always pair the swatch with the title; never use the swatch alone or the title alone. |
| Cell / part labels (in SVG) | mono, 10px, letter-spacing 0.08em, uppercase, `--muted` — except labels sitting on top of the filled accent shape, which flip to `--ink` for contrast. |
| Dimension label (in SVG) | mono, 10px, letter-spacing 0.1em, uppercase, `--muted`, centered. |
| Caption (footer) | body font, weight 300, 0.95rem, line-height 1.5, `--ink`, `max-width: 38ch`, centered, `text-wrap: balance`. |

## Layout skeleton

```
body (flex column, justify-content: space-between, padding: clamp(2rem,4.5vw,4.5rem), gap: 2rem)
├─ header            → eyebrow only
├─ titlewrap         → accent swatch + lowercase title, flex row
├─ .stage (flex: 1, centered)
│   └─ svg viewBox="0 0 760 300" (max-width 760px, overflow visible)
└─ footer            → one centered caption line
```

No slide has a large body-copy block. If the concept needs more than the eyebrow +
title + one-sentence caption, it's two slides, not one dense one.

## The diagram (SVG stage)

The diagram itself is free — a grid, a path, a stack, whatever the concept actually
is. Don't force everything into a 2×2 box; that's specific to whatever content
inspired the first version, not the style itself. What's fixed is the *apparatus*
around it:

1. **Structure is ink, 1.25px stroke, no fill** (`.container-box` style) — outer
   boundary, connecting lines, anything that's "just structure."
2. **Internal dividers are `--line` hairlines**, 1.25px — not full strokes, they're
   quieter than the outer boundary.
3. **Exactly one element gets the accent fill.** This is the same element the title's
   swatch points to. If nothing in the diagram is more important than anything else,
   that's a sign the slide doesn't have a thesis yet — find the one thing.
4. **Registration crosshairs.** Four small `+` ticks, `--muted` stroke, 1px, sitting
   ~14px outside each corner of the diagram's outer bounding box (not touching it).
   Pure technical-drawing signal, no label. Reference implementation:
   ```svg
   <g class="reg" stroke="var(--muted)" stroke-width="1">
     <path d="M{x1-14},{y1-14} v14 M{x1-21},{y1-7} h14" />
     <!-- repeat, mirrored, at all four corners of the bounding box -->
   </g>
   ```
5. **One dimension line**, under (or beside, for a tall composition) the diagram's
   bounding box: an ink 1px line spanning the box's width/height with short
   perpendicular end-ticks, and a centered mono uppercase label below it. That label
   **must be a true count about the diagram** — "4 cells · 1 accent", "3 stages ·
   1 exit", "6 nodes · 1 root" — never decorative filler. It's the spec-sheet
   annotation that makes the diagram read as measured rather than illustrated.

## Caption voice

One sentence. States the *rule* the slide demonstrates, not a description of the
picture — "one cell gets the color, the rest is just lines" rather than "this
diagram shows four cells with one highlighted." Aphoristic, Rams-register: think
product-manual copy, not marketing copy. It's fine to explicitly reference the
principle (e.g. "as little design as possible") when it fits.

## Numbering

Slides within a deck are numbered `01`, `02`, ... in the eyebrow, matching filename
prefixes (`01-*.html`, `02-*.html`, ...). Keep numbering sequential per deck; don't
reuse the `NN · TAG` two-part eyebrow format for style tests in a real deck.
