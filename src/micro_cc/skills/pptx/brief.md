# Stage 1 — The Paper Brief

Before any SVG exists, design every slide on paper first: what it argues and how
it's blocked out. This is a writing task, not a drawing task — get the thinking
right here so Stage 2 is just execution.

Write one file, `brief.md`, with one block per slide, in this shape:

```
## Slide 04

Headline: Churn concentrates in the first 90 days, not evenly across the year
Takeaway: Fix onboarding before touching retention offers elsewhere

Content:
- 62% of annual churn happens in months 1-3 post-signup
- Cohorts that complete onboarding checklist churn 3x less
- Support ticket volume peaks day 4, not day 1

Layout: stat-callout-left
- Left third: big stat "62%" + caption "of annual churn, months 1-3"
- Right two-thirds: 3-row horizontal bar showing churn rate by month bucket
- Footer: source line, small
```

## Rules for the headline

Use Barbara Minto's pyramid principle: the headline is a **complete claim**, not
a topic label.

- Wrong: "Churn Analysis" (a topic)
- Right: "Churn concentrates in the first 90 days" (a claim the slide then proves)

Every visual and every bullet on the slide exists to support that one claim
(MECE — mutually exclusive, collectively exhaustive support, nothing decorative,
nothing that argues a different point).

## Rules for Content

- Terse. This is the data/argument the slide will show, not the exact copy —
  Stage 2 decides exact wording and where line breaks fall.
- 3-5 content points max per slide. If a slide needs more, it's two slides.
- Numbers stay as numbers ("62%"), not vague ("most").

## Rules for Layout

Name a layout pattern (see `svg-slides.md` for the pattern catalog: stat-callout,
2-column, 3-up cards, comparison table, timeline, icon-row, section-divider,
quote, chart-with-takeaway) and list the elements inside it as a bare structural
list — position, role, rough size. No colors, no fonts, no exact coordinates —
that's Stage 2's job.

**Vary layouts across the deck.** A brief where every slide says "2-column" is a
sign the thinking hasn't actually differentiated the slides — go back and ask
what's structurally different about each claim.

## Deck-level opening

Before writing per-slide blocks, write 2-3 lines at the top of `brief.md`:
- The deck's overall governing thought (the one sentence the whole deck argues)
- The narrative arc in order (e.g. "problem -> root cause -> options -> recommendation -> ask")

Then number slides in that order. Section-divider slides get their own block too
(headline = section name, layout = section-divider).

## Sign-off

If the user is present and engaged, show them `brief.md` and get a quick go/no-go
before Stage 2 — cheap to fix a wrong argument in text, expensive to fix after
20 SVGs are drawn. If the user has said "just go" or isn't available to review,
proceed straight to Stage 2.
