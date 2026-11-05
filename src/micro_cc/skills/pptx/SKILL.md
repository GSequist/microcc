---
name: pptx
description: "Use any time a .pptx deck is being created: pitch decks, proposals, consulting-style decks, from scratch OR on a user-supplied template (.pptx/.potx, 'use our template', 'on brand', 'corporate master'). Trigger on 'deck', 'slides', 'presentation' or a .pptx/.potx filename. Also covers post-build edits (notes, reorder, clone, footers, layout rebind) via paper-pptx."
---

# PPTX Skill

One Claude, one deck, start to finish, in a single continuous pass. No subagents.

## Stage 0 — Route

**Did the user give a template or a past deck whose look the new one must carry?**

| Input | Path | Build |
|---|---|---|
| `.potx`, or a `.pptx` "to use / build on / match" | **Template** → read [template.md](template.md) | native placeholders, charts and tables on their layouts; SVG only for diagram areas |
| A deck as a loose visual reference only ("something like this"), or nothing | **From scratch** → Stages 1-3 below | full-slide SVGs |

If they only shared a deck and it's unclear which they mean, ask once. When the deck will go
back into their firm (client work, corporate decks), default to the template path. A
full-slide SVG deck is never "on template", whatever it looks like.

For the from-scratch path, get the style direction first unless it's already given: palette
(hex), font, mood, logo. If the user offers a visual reference deck,
`python scripts/deck_to_images.py ref.pptx ref --outdir renders` and read the images. If
they say "just make it look good", use the default in `svg-slides.md` → Color and say which
one you picked.

## From scratch

| Stage | What happens | Output |
|---|---|---|
| 1 — Brief | Plan every slide on paper: argument and structure | `brief.md` |
| 2 — SVG | One high-fidelity standalone SVG per brief block | `slides/NN-*.svg` |
| 3 — Assemble | One full-bleed SVG picture per slide | `output.pptx` |

### Stage 1 — Paper brief

Read [brief.md](brief.md). Start with the deck's governing thought and narrative arc. Then
write one block per slide: a headline that makes a claim (not a topic label), the content,
and a named layout pattern. Get a quick sign-off if the user is engaged.

### Stage 2 — SVG slides

Read [svg-slides.md](svg-slides.md). Write one `slides/NN-slug.svg` per block with
`viewBox="0 0 1280 720"`, and render each one before moving to the next:

```bash
python scripts/render_svg.py slides/01-title.svg
```

### Stage 3 — Assemble

```bash
python scripts/build_deck.py slides/ output.pptx [--template brand.pptx]
```

Each slide is an SVG picture with a PNG fallback, named after the file, with the headline as
alt text. `--template` places the slides on the template's blank layout, so its background,
logo and footer stay. In that case don't paint a full-slide background rect in the SVGs.

## Post-build edits (both paths)

`pptx` is **paper-pptx** (an import-compatible python-pptx fork). Use it for anything after
the build. Never rebuild just to add notes or reorder, and never edit an SVG picture through
python-pptx (edit the `.svg` and rebuild instead):

```python
from pptx import Presentation
from pptx.package import patch_save

prs = Presentation("output.pptx")
with prs.batch():
    for slide, note in zip(prs.slides, notes):
        slide.notes_slide.notes_text_frame.text = note
    prs.slides.move(4, 1)                                # also: clone, delete, reorder
patch_save("output.pptx", prs, "output_final.pptx")     # untouched parts stay byte-identical
```

A `pptx.errors.PaperRefusal` means the edit was not applied and the deck is unchanged.
Report it. Don't work around it with raw XML.

## Final QA (both paths)

```bash
python scripts/deck_to_images.py output.pptx final --outdir renders
```

Read every `renders/final-N.jpg`. This catches font substitution, bounds mapping, leftover
placeholder prompts and clashes with master graphics, none of which the per-slide previews
can show.

## Handoff

- **From scratch:** each slide is a full vector drawing. In Microsoft 365, right-click →
  **Convert to Shape** (older builds: **Ungroup** twice) turns it into native, editable
  shapes and text. Say this every time; it's why the deck is built this way.
- **Template:** built on their layouts. Titles and body text are real placeholders, and
  charts are native (right-click → Edit Data). Mention Convert to Shape only for the SVG
  diagram areas.

## Dependencies

- `pip uninstall -y python-pptx && pip install paper-pptx` (provides `import pptx`; never
  install both)
- `pip install cairosvg`: SVG preview and the PNG fallback. On a system or Homebrew Python
  this may refuse with "externally-managed-environment"; use a venv or
  `pipx install --include-deps cairosvg`, not `--break-system-packages`.
- LibreOffice (`soffice`) and Poppler (`pdftoppm`): template renders and final QA
  (`scripts/deck_to_images.py`; `scripts/office/soffice.py` configures the soffice call
  automatically)
