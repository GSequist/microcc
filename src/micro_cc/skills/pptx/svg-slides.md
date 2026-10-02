# Stage 2 — High-Fidelity SVG Slides

Turn each block in `brief.md` into one standalone `.svg` file. Each file is a
complete, self-contained slide — no external stylesheets, no `@import`, no JS,
no linked assets. Everything inline.

Write files as `slides/01-<slug>.svg`, `slides/02-<slug>.svg`, ... — the
leading number controls slide order in Stage 3.

## Canvas

```xml
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720" width="1280" height="720">
```

1280x720 at the standard 96dpi maps exactly to a 13.333in x 7.5in 16:9 slide —
this is why the canvas size is fixed. Never change the viewBox per slide.

## Register: consulting deck, not poster

This is McKinsey/BCG output, not art. That means:

- Generous whitespace over density. If it's cramped, cut content, don't shrink
  the font.
- One or two accent colors used with restraint — most of the slide is ink text
  on white/off-white, plus thin hairline rules for structure.
- Left-aligned body copy and titles. Center only single short callout numbers.
- Every slide's headline sits in the same position across the deck (top strip),
  so the deck reads as one document, not a slide-by-slide art project.
- Real data, real labels — never "Lorem ipsum" or placeholder numbers.

## Fonts

Use fonts guaranteed to be present wherever the deck gets opened. Do **not**
`@font-face` or reference a webfont — it will not travel with the embedded SVG.

```
font-family="Arial, Helvetica, sans-serif"          /* body, titles */
font-family="'Georgia', 'Times New Roman', serif"   /* if the brand wants serif */
```

Weight via `font-weight` (400/600/700), not by picking exotic family names.

## Text wrapping — the one thing that will bite you

SVG `<text>` does **not** auto-wrap. You must hand-break every line with
`<tspan>`, and you must estimate whether a line fits before committing to it.

```xml
<text font-family="Arial" font-size="40" font-weight="700" fill="#1A1A1A">
  <tspan x="80" y="120">Churn concentrates in the</tspan>
  <tspan x="80" y="168">first 90 days, not evenly</tspan>
</text>
```

**Every `<tspan>` gets an absolute `x` and `y`. Never use `dx`/`dy`** — not even
`dy="0"`. The slide renders fine either way, but PowerPoint's "Convert to Shape"
drops relative offsets, so `dy`-stacked lines collapse onto each other the moment
the user ungroups the slide. Compute each line's `y` yourself: first baseline +
n × line-height (≈1.2 × font-size). Same for anything else positioned relatively —
horizontal runs of mixed styling get their own absolute `x`.

Rough sizing budget (Arial, regular weight, in SVG units where 1 unit ≈ 1px at
96dpi): a line's pixel width is roughly `0.55 * font-size * character-count`.
Before finalizing a line, check it against the box width; if it's close, break
earlier rather than trust it'll fit. For anything data-driven or user-supplied
(headlines, source quotes), compute the estimate rather than eyeballing it.

Leave real margin — if a text block's right edge lands within ~40px of the
column boundary, break the line earlier.

## Layout patterns

Pick one per slide from `brief.md`'s `Layout:` field. All patterns share:
top margin ~60px for the headline strip, side margins ~80px, bottom ~40px for
an optional footer/source line.

- **stat-callout** — one huge number (120-160pt) as the thesis, small caption
  below it, supporting detail in a column beside it.
- **2-column** — even split, a vertical hairline divider at x=640.
- **3-up cards** — three equal rounded-rect cards in a row, each with an icon,
  a bold mini-header, and 1-2 lines of body.
- **comparison** — two or three columns with a shared header row (e.g.
  before/after, option A/B/C), thin row dividers.
- **timeline** — a horizontal line with 3-6 numbered nodes, label above or
  below alternating to avoid crowding.
- **icon-row** — a vertical stack of icon-in-circle + bold label + one-line
  description, repeated 3-5 times.
- **chart-with-takeaway** — a simple bar/line chart (drawn as plain SVG
  `<rect>`/`<path>`, no chart library) on one side, the one-sentence takeaway
  in large type on the other.
- **section-divider** — full-bleed accent-color background, large title
  centered or left-anchored, section number, nothing else.
- **quote** — a large pull-quote, attribution line below in small caps/mono.

## Building blocks

**Headline strip** (every content slide except section-divider):

```xml
<text x="80" y="90" font-family="Arial" font-size="34" font-weight="700" fill="#1A1A1A">Churn concentrates in the first 90 days</text>
<line x1="80" y1="112" x2="1200" y2="112" stroke="#DADADA" stroke-width="1.5"/>
```

**Stat callout:**

```xml
<text x="80" y="380" font-family="Arial" font-size="140" font-weight="700" fill="#0B5FFF">62%</text>
<text x="80" y="420" font-family="Arial" font-size="20" fill="#6B6B6B">of annual churn, months 1-3</text>
```

**Card (for 3-up):**

```xml
<rect x="80" y="180" width="360" height="420" rx="12" fill="#F7F7F8" stroke="#E4E4E6" stroke-width="1"/>
<circle cx="140" cy="240" r="24" fill="#0B5FFF"/>
<!-- icon path or short glyph centered inside the circle, fill="#FFFFFF" -->
<text x="80" y="310" font-family="Arial" font-size="22" font-weight="700" fill="#1A1A1A">Onboarding gap</text>
```

**Simple bar chart** (draw bars as rects, scale height by hand from the data —
no chart library, no JS):

```xml
<!-- baseline -->
<line x1="640" y1="560" x2="1200" y2="560" stroke="#1A1A1A" stroke-width="1.5"/>
<!-- one bar: height = value/maxValue * chart-height -->
<rect x="680" y="380" width="60" height="180" fill="#0B5FFF"/>
<text x="710" y="580" font-family="Arial" font-size="14" fill="#6B6B6B" text-anchor="middle">Mo 1</text>
<text x="710" y="365" font-family="Arial" font-size="16" font-weight="700" fill="#1A1A1A" text-anchor="middle">28%</text>
```

**Icons.** No icon fonts, no external icon libraries — draw simple geometric
glyphs directly as SVG paths/shapes (circle, check, arrow, simple line-art),
or use basic Unicode glyphs only as a last resort (avoid emoji; rendering of
color emoji is inconsistent across PowerPoint versions).

## Color

Take the palette from Stage 0 (user's brand colors, or notes from a converted
reference deck). If none was given, use a restrained default and state the
choice: ink `#1A1A1A` text, `#6B6B6B` muted/caption text, `#E4E4E6` hairlines,
`#F7F7F8` card backgrounds, one accent (e.g. `#0B5FFF`) for the single most
important number or bar per slide — never more than one accent color doing
work on the same slide.

## Effects — keep them conservative

Flat fills and 1-1.5px strokes render identically everywhere. If you want a
drop shadow on a card, `feDropShadow` is fine in small doses:

```xml
<filter id="cardShadow" x="-20%" y="-20%" width="140%" height="140%">
  <feDropShadow dx="0" dy="2" stdDeviation="6" flood-color="#000000" flood-opacity="0.08"/>
</filter>
```

Avoid gradients, complex filter chains, and animations — they either don't
survive the PPTX embed cleanly or don't match once opened in PowerPoint.

## Check each slide before moving on

```bash
python scripts/render_svg.py slides/01-title.svg
```

Read the resulting PNG. Look for: text overflowing its column, overlapping
elements, a line that's clearly too tight against its neighbor, low contrast,
an accent color used more than once on the slide. Fix in the SVG source, not
by regenerating from scratch. Move to the next slide only once this one is
clean — don't batch all slides then discover the same font-sizing mistake
repeated across twenty files.
