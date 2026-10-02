---
name: pptx
description: "Use this skill any time a .pptx presentation is being created from scratch — pitch decks, business proposals, consulting-style decks. Trigger whenever the user mentions \"deck,\" \"slides,\" \"presentation,\" or references a .pptx filename with intent to create one. Not for template-based editing of an existing .pptx file (there is no dedicated workflow for that here — read the file's XML directly if asked to tweak an existing deck)."
---

# PPTX Skill

Three stages, always in this order, done directly by you in a single
continuous pass. No subagents, no parallel splits — one Claude, one deck,
start to finish.

| Stage | What happens | Output |
|---|---|---|
| 0 — Input | Get style direction; convert a reference deck to images if given one | style notes |
| 1 — Brief | Design every slide on paper: argument + structure, McKinsey-style | `brief.md` |
| 2 — SVG | Turn each brief block into a high-fidelity standalone SVG slide | `slides/NN-*.svg` |
| 3 — Assemble | Paste each SVG into an empty deck, one per slide | `output.pptx` |

## Stage 0 — Style Input

Ask before writing anything, unless the user already gave this:

1. **Style** — palette (hex if they have one), font preference, mood
   (corporate-minimal, bold, dark theme, etc.), logo if any.
2. **Reference deck** — do they have a past `.pptx` to match the look of?
   If yes:
   ```bash
   python scripts/deck_to_images.py reference.pptx ref
   ```
   Read each resulting `ref-NN.jpg` with vision. Write a short style-notes
   block (colors observed, fonts, layout habits, logo placement) — this
   becomes the palette/style input for Stage 2 instead of asking the user.

If the user says "just make it look good" with no reference and no
preference, pick a clean, restrained default (see `svg-slides.md` → Color)
and state the choice so they can redirect.

## Stage 1 — Paper Brief

**Read [brief.md](brief.md) for the full format and rules.**

Write `brief.md`: a deck-level governing thought + narrative arc, then one
block per slide — headline (a claim, not a topic, per Minto's pyramid
principle), the content it needs, and a named layout pattern with its
elements listed structurally. No colors or exact coordinates yet — that's
Stage 2.

Get a quick sign-off on `brief.md` if the user is present and engaged; skip
straight to Stage 2 if they've said to just go ahead.

## Stage 2 — SVG Slides

**Read [svg-slides.md](svg-slides.md) for the full design system, layout
pattern catalog, and building-block snippets.**

For each block in `brief.md`, write one self-contained `slides/NN-slug.svg`,
`viewBox="0 0 1280 720"` (maps exactly to a 13.333in x 7.5in 16:9 slide).
After each file, render and inspect it before moving to the next:

```bash
python scripts/render_svg.py slides/01-title.svg
```

Read the PNG, fix overflow/overlap/contrast issues in the SVG source
immediately — don't discover the same mistake repeated across every slide
because you batched them all first.

## Stage 3 — Assemble

Paste each finished SVG into an otherwise-empty slide deck, full-bleed, one
slide per file:

```bash
python scripts/build_deck.py slides/ output.pptx
```

Each slide gets the SVG (what PowerPoint renders and what "Convert to Shape"
explodes) plus a real rasterized PNG fallback for older viewers.

### Post-assembly edits (optional)

`pptx` here is **paper-pptx** (import-compatible python-pptx fork). Use it for
anything after assembly — never re-run Stage 3 just to add notes or reorder,
and never edit slide SVG pictures via python-pptx (edit the `.svg` and rebuild):

```python
from pptx import Presentation
from pptx.package import patch_save

prs = Presentation("output.pptx")
with prs.batch():
    for slide, note in zip(prs.slides, notes):          # speaker notes from brief.md
        slide.notes_slide.notes_text_frame.text = note
    prs.slides.move(4, 1)                                # also: clone, delete, reorder
patch_save("output.pptx", prs, "output_final.pptx")     # untouched parts stay byte-identical
```

A `pptx.errors.PaperRefusal` means the edit was not applied and the deck is
unchanged — report it, don't work around it with raw XML.

Then do a final visual pass on the real output file:

```bash
python scripts/deck_to_images.py output.pptx final
```

Read each `final-NN.jpg`. This catches embedding-level issues (font
substitution, an SVG that didn't map cleanly to the slide bounds) that the
Stage 2 per-slide preview can't see, since it's rendering the assembled
`.pptx`, not the raw SVG.

### Handoff — tell the user how to unlock it

Don't just hand back `output.pptx` silently. Every time, tell the user: each
slide's picture is secretly a full vector drawing. In PowerPoint (Microsoft
365+), right-click it and choose **"Convert to Shape"** (older builds:
**Ungroup**, twice) to explode it into individual, fully-editable,
high-resolution native shapes and text — not a flattened image. That's the
reason this skill builds decks this way instead of just exporting PNGs, so
say it every time, not only when asked.

## Why SVG-first

Every element on every slide arrives in the final `.pptx` as a real, native
vector object — not a flattened raster image and not an opaque shape tree
built through an API that only exposes some of what's possible. In modern
PowerPoint/Microsoft 365, the user can right-click the embedded picture on
any slide and choose "Convert to Shape" to ungroup it into individual,
fully-editable native shapes and text. That means the deck this skill hands
back isn't a finished-and-frozen artifact — it's a high-resolution starting
point the user can immediately nudge, recolor, and polish by hand in
PowerPoint, the same way they would a deck a designer built natively.

## Dependencies

- `pip uninstall -y python-pptx && pip install paper-pptx` — Stage 3 assembly and
  post-assembly edits (provides `import pptx`; never install both)
- `pip install cairosvg` — Stage 2 per-slide preview and Stage 3 PNG fallback (on a
  system/Homebrew Python this may refuse with "externally-managed-environment";
  use a venv, or `pipx install --include-deps cairosvg`, rather than
  `--break-system-packages`)
- LibreOffice (`soffice`) + Poppler (`pdftoppm`) — Stage 0 reference-deck
  ingestion and Stage 3 final QA render (`scripts/deck_to_images.py` wraps
  both; `scripts/office/soffice.py` auto-configures the LibreOffice call)
