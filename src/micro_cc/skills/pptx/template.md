# Template Path — build on the user's .pptx / .potx

The deliverable must look like it came out of their template: their masters, theme fonts and
colours, real title/body placeholders (Outline view, accessibility, "Reset slide" and theme
switches all work). Native objects first; SVG only for the bespoke-diagram area of a slide.

All helpers live in `scripts/template_ops.py`; `pptx` is paper-pptx.

## 1. Map the template

```bash
python scripts/inspect_template.py brand.potx --render renders/ > template_map.json
```

- `template_map.json`: slide size, theme colours (`accent1`…) and heading/body fonts, every
  layout with its placeholders (`idx`, `type`, px bbox), and the template's own sample slides
  with their shapes and text.
- `renders/layout-NN.jpg` (NN = layout `index`): each layout with its placeholders labelled
  `idx N · TYPE`. `renders/sample-NN.jpg`: the template's own slides.

Look at every render. Write short template notes into `brief.md`: which layouts exist for
title / section / content / two-column / chart, which sample slides are good composed starting
points (agenda, KPI rows, team slides), logo and footer furniture, accent colours.

## 2. Brief

Same as [brief.md](brief.md), except `Layout:` names a **template layout by exact name** (or
`clone sample N`) and maps the content onto placeholder `idx`s:

```
Layout: "Title and Content"
- idx 0: headline
- idx 1: 3 bullets (level 0), one sub-bullet (level 1)
```

If no layout fits a slide, choose the closest one and put an SVG or chart into its content
placeholder. Don't draw the whole slide as an SVG.

## 3. Build

Write one `build_deck_script.py` for the deck, run it, and fix what's wrong:

```python
import sys; sys.path.insert(0, "<skill>/scripts")
from template_ops import open_deck, layout, ph, fill, place_chart, place_table, place_svg, \
    drop_empty_placeholders, delete_slides, overflow_report
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE

prs = open_deck("brand.potx")
samples = list(range(len(prs.slides)))          # template's own slides, deleted at the end

with prs.batch():
    s = prs.slides.add_slide(layout(prs, "Title and Content"))
    fill(ph(s, 0), ["Churn concentrates in the first 90 days"])
    fill(ph(s, 1), ["62% of annual churn in months 1-3", (1, "cohort data 2023-25"),
                    "Onboarding completers churn 3x less"])

    s = prs.slides.add_slide(layout(prs, "Two Content"))
    fill(ph(s, 0), ["Month 1 carries the loss"])
    cd = CategoryChartData(); cd.categories = ["M1", "M2", "M3"]; cd.add_series("Churn %", (28, 20, 14))
    place_chart(s, ph(s, 1), XL_CHART_TYPE.COLUMN_CLUSTERED, cd)
    place_svg(s, ph(s, 2), open("diagrams/funnel.svg", "rb").read(), name="funnel", descr="Onboarding funnel")

    agenda = prs.slides.clone(samples[0], after=len(prs.slides) - 1)   # reuse a composed sample slide
    fill(agenda.shapes.title, ["Agenda"])

    for sl in prs.slides:
        drop_empty_placeholders(sl)
    delete_slides(prs, samples)

for issue in overflow_report(prs):
    print(issue)
prs.save("output.pptx")
```

Rules:
- **Never set fonts, sizes or colours on placeholder text.** They come from the master. `fill`
  copies the formatting of whatever paragraph already sits at that position, so on cloned
  sample slides the local styling carries over too.
- **Charts and tables are native** (`place_chart`, `place_table`). They pick up the theme
  colours and the client can edit the data. For a series that needs emphasis, set
  `chart.plots[0].series[i].format.fill` with `MSO_THEME_COLOR.ACCENT_1`, not a hex value.
- **SVG only for diagrams** (process flows, frameworks, custom visuals). Author the SVG
  `viewBox` at the placeholder's px `w h` from the map, use the theme hex colours and the
  theme font name, and follow the text rules in [svg-slides.md](svg-slides.md) (absolute
  `tspan` x/y, hand-wrapped lines). `place_svg` fits the picture into the placeholder and
  keeps the aspect ratio.
- `clone(src)` inserts directly after `src`. Pass `after=` (or call `prs.slides.move`) to
  control the order. `clone` refuses slides that contain SmartArt, OLE objects or comments.
  Use `add_slide` on that slide's layout instead.
- Speaker notes: `slide.notes_slide.notes_text_frame.text = ...`.
- Footers and slide numbers: `prs.apply_footers(footer="…", slide_number=True)` only if the
  template's samples use them.
- Moving an existing slide to another layout: `slide.rebind_layout(layout(prs, "…"))`. Read
  the returned `RebindReport`. The default `orphan_policy="refuse"` raises an error instead
  of silently dropping a placeholder that has nowhere to go.
- A `pptx.errors.PaperRefusal` (or another typed refusal) means the edit was not applied.
  Report it. Don't route around it with raw XML.

## 4. QA

1. Check `overflow_report` (it estimates from the resolved font size). Fix every hit by
   shortening the text or splitting the slide. Don't shrink the font: the master defines it.
2. `python scripts/deck_to_images.py output.pptx final --outdir renders` and read every image.
   Check that no "Click to add" prompts remain, that the logo and footer furniture are
   intact, that nothing overlaps master graphics, and that SVG areas sit inside their
   placeholders.
3. LibreOffice substitutes the brand font if it isn't installed. If the theme font is
   missing locally, say so: line breaks in PowerPoint may differ slightly from the render.

## Handoff

Tell the user the deck is built on their template layouts. Titles and body text are real
placeholders, and charts are native and editable (right-click → Edit Data). Mention
"Convert to Shape" only for slides that contain SVG diagrams.
