---
name: design
description: Create visual design artifacts, in two modes. Mode A (poster/art, .png or .pdf) — philosophy-driven original visual art for a poster, piece of art, or other static design piece. Mode B (diagram slide, .html/.svg) — a single-concept diagram slide in George's pastel one-accent "blueprint" Rams/Bauhaus style, for a standalone slide, a deck slide, or "visualize this concept" requests. Use whenever the user asks to create a poster, art piece, design, static visual, diagram, or slide. Every .html artifact this skill produces gets opened in the browser via bash_ as the final step — never leave one unopened.
---

This skill covers two distinct kinds of visual output — pick the mode that matches the request, they are not variants of each other:

- **Mode A — poster / art** (`.png`/`.pdf`): a full-bleed, philosophy-driven art piece. Museum/magazine quality, 90% visual, 10% essential text. Use for posters, identity pieces, standalone art.
- **Mode B — blueprint diagram slide** (`.html`, self-contained): a single-concept technical diagram — pastel one accent, ink line work, registration crosshairs, a dimension line. Use for decks, explainer slides, "visualize this concept," anything that needs to show one mechanism clearly rather than evoke a mood.

If the request is ambiguous (e.g. "make something showing how X works" with no format named), default to Mode B — it's faster to produce, matches this codebase's existing deck conventions, and is far more legible for explaining a mechanism than an abstract art piece would be.

Any `.html` file this skill writes (Mode B, always) gets opened in the browser as the last step — see **OPENING HTML OUTPUT** at the bottom of this file. Do this via `bash_`, unconditionally, right after the file is saved.

---

## MODE A — POSTER / ART (.png / .pdf)

These are instructions for creating design philosophies - aesthetic movements that are then EXPRESSED VISUALLY. Output only .md files, .pdf files, and .png files.

Complete this in two steps:
1. Design Philosophy Creation (.md file)
2. Express by creating it on a canvas (.pdf file or .png file)

First, undertake this task:

## DESIGN PHILOSOPHY CREATION

To begin, create a VISUAL PHILOSOPHY (not layouts or templates) that will be interpreted through:
- Form, space, color, composition
- Images, graphics, shapes, patterns
- Minimal text as visual accent

### THE CRITICAL UNDERSTANDING
- What is received: Some subtle input or instructions by the user that should be taken into account, but used as a foundation; it should not constrain creative freedom.
- What is created: A design philosophy/aesthetic movement.
- What happens next: Then, the same version receives the philosophy and EXPRESSES IT VISUALLY - creating artifacts that are 90% visual design, 10% essential text.

Consider this approach:
- Write a manifesto for an art movement
- The next phase involves making the artwork

The philosophy must emphasize: Visual expression. Spatial communication. Artistic interpretation. Minimal words.

### HOW TO GENERATE A VISUAL PHILOSOPHY

**Name the movement** (1-2 words): "Brutalist Joy" / "Chromatic Silence" / "Metabolist Dreams"

**Articulate the philosophy** (4-6 paragraphs - concise but complete):

To capture the VISUAL essence, express how the philosophy manifests through:
- Space and form
- Color and material
- Scale and rhythm
- Composition and balance
- Visual hierarchy

**CRITICAL GUIDELINES:**
- **Avoid redundancy**: Each design aspect should be mentioned once. Avoid repeating points about color theory, spatial relationships, or typographic principles unless adding new depth.
- **Emphasize craftsmanship REPEATEDLY**: The philosophy MUST stress multiple times that the final work should appear as though it took countless hours to create, was labored over with care, and comes from someone at the absolute top of their field. This framing is essential - repeat phrases like "meticulously crafted," "the product of deep expertise," "painstaking attention," "master-level execution."
- **Leave creative space**: Remain specific about the aesthetic direction, but concise enough that the next Claude has room to make interpretive choices also at a extremely high level of craftmanship.

The philosophy must guide the next version to express ideas VISUALLY, not through text. Information lives in design, not paragraphs.

### PHILOSOPHY EXAMPLES

**"Concrete Poetry"**
Philosophy: Communication through monumental form and bold geometry.
Visual expression: Massive color blocks, sculptural typography (huge single words, tiny labels), Brutalist spatial divisions, Polish poster energy meets Le Corbusier. Ideas expressed through visual weight and spatial tension, not explanation. Text as rare, powerful gesture - never paragraphs, only essential words integrated into the visual architecture. Every element placed with the precision of a master craftsman.

**"Chromatic Language"**
Philosophy: Color as the primary information system.
Visual expression: Geometric precision where color zones create meaning. Typography minimal - small sans-serif labels letting chromatic fields communicate. Think Josef Albers' interaction meets data visualization. Information encoded spatially and chromatically. Words only to anchor what color already shows. The result of painstaking chromatic calibration.

**"Analog Meditation"**
Philosophy: Quiet visual contemplation through texture and breathing room.
Visual expression: Paper grain, ink bleeds, vast negative space. Photography and illustration dominate. Typography whispered (small, restrained, serving the visual). Japanese photobook aesthetic. Images breathe across pages. Text appears sparingly - short phrases, never explanatory blocks. Each composition balanced with the care of a meditation practice.

**"Organic Systems"**
Philosophy: Natural clustering and modular growth patterns.
Visual expression: Rounded forms, organic arrangements, color from nature through architecture. Information shown through visual diagrams, spatial relationships, iconography. Text only for key labels floating in space. The composition tells the story through expert spatial orchestration.

**"Geometric Silence"**
Philosophy: Pure order and restraint.
Visual expression: Grid-based precision, bold photography or stark graphics, dramatic negative space. Typography precise but minimal - small essential text, large quiet zones. Swiss formalism meets Brutalist material honesty. Structure communicates, not words. Every alignment the work of countless refinements.

*These are condensed examples. The actual design philosophy should be 4-6 substantial paragraphs.*

### ESSENTIAL PRINCIPLES
- **VISUAL PHILOSOPHY**: Create an aesthetic worldview to be expressed through design
- **MINIMAL TEXT**: Always emphasize that text is sparse, essential-only, integrated as visual element - never lengthy
- **SPATIAL EXPRESSION**: Ideas communicate through space, form, color, composition - not paragraphs
- **ARTISTIC FREEDOM**: The next Claude interprets the philosophy visually - provide creative room
- **PURE DESIGN**: This is about making ART OBJECTS, not documents with decoration
- **EXPERT CRAFTSMANSHIP**: Repeatedly emphasize the final work must look meticulously crafted, labored over with care, the product of countless hours by someone at the top of their field

**The design philosophy should be 4-6 paragraphs long.** Fill it with poetic design philosophy that brings together the core vision. Avoid repeating the same points. Keep the design philosophy generic without mentioning the intention of the art, as if it can be used wherever. Output the design philosophy as a .md file.

---

## DEDUCING THE SUBTLE REFERENCE

**CRITICAL STEP**: Before creating the canvas, identify the subtle conceptual thread from the original request.

**THE ESSENTIAL PRINCIPLE**:
The topic is a **subtle, niche reference embedded within the art itself** - not always literal, always sophisticated. Someone familiar with the subject should feel it intuitively, while others simply experience a masterful abstract composition. The design philosophy provides the aesthetic language. The deduced topic provides the soul - the quiet conceptual DNA woven invisibly into form, color, and composition.

This is **VERY IMPORTANT**: The reference must be refined so it enhances the work's depth without announcing itself. Think like a jazz musician quoting another song - only those who know will catch it, but everyone appreciates the music.

---

## CANVAS CREATION

With both the philosophy and the conceptual framework established, express it on a canvas. Take a moment to gather thoughts and clear the mind. Use the design philosophy created and the instructions below to craft a masterpiece, embodying all aspects of the philosophy with expert craftsmanship.

**IMPORTANT**: For any type of content, even if the user requests something for a movie/game/book, the approach should still be sophisticated. Never lose sight of the idea that this should be art, not something that's cartoony or amateur.

To create museum or magazine quality work, use the design philosophy as the foundation. Create one single page, highly visual, design-forward PDF or PNG output (unless asked for more pages). Generally use repeating patterns and perfect shapes. Treat the abstract philosophical design as if it were a scientific bible, borrowing the visual language of systematic observation—dense accumulation of marks, repeated elements, or layered patterns that build meaning through patient repetition and reward sustained viewing. Add sparse, clinical typography and systematic reference markers that suggest this could be a diagram from an imaginary discipline, treating the invisible subject with the same reverence typically reserved for documenting observable phenomena. Anchor the piece with simple phrase(s) or details positioned subtly, using a limited color palette that feels intentional and cohesive. Embrace the paradox of using analytical visual language to express ideas about human experience: the result should feel like an artifact that proves something ephemeral can be studied, mapped, and understood through careful attention. This is true art. 

**Text as a contextual element**: Text is always minimal and visual-first, but let context guide whether that means whisper-quiet labels or bold typographic gestures. A punk venue poster might have larger, more aggressive type than a minimalist ceramics studio identity. Most of the time, font should be thin. All use of fonts must be design-forward and prioritize visual communication. Regardless of text scale, nothing falls off the page and nothing overlaps. Every element must be contained within the canvas boundaries with proper margins. Check carefully that all text, graphics, and visual elements have breathing room and clear separation. This is non-negotiable for professional execution. **IMPORTANT: Use different fonts if writing text. Search the `./canvas-fonts` directory. Regardless of approach, sophistication is non-negotiable.**

Download and use whatever fonts are needed to make this a reality. Get creative by making the typography actually part of the art itself -- if the art is abstract, bring the font onto the canvas, not typeset digitally.

To push boundaries, follow design instinct/intuition while using the philosophy as a guiding principle. Embrace ultimate design freedom and choice. Push aesthetics and design to the frontier. 

**CRITICAL**: To achieve human-crafted quality (not AI-generated), create work that looks like it took countless hours. Make it appear as though someone at the absolute top of their field labored over every detail with painstaking care. Ensure the composition, spacing, color choices, typography - everything screams expert-level craftsmanship. Double-check that nothing overlaps, formatting is flawless, every detail perfect. Create something that could be shown to people to prove expertise and rank as undeniably impressive.

Output the final result as a single, downloadable .pdf or .png file, alongside the design philosophy used as a .md file.

---

## FINAL STEP

**IMPORTANT**: The user ALREADY said "It isn't perfect enough. It must be pristine, a masterpiece if craftsmanship, as if it were about to be displayed in a museum."

**CRITICAL**: To refine the work, avoid adding more graphics; instead refine what has been created and make it extremely crisp, respecting the design philosophy and the principles of minimalism entirely. Rather than adding a fun filter or refactoring a font, consider how to make the existing composition more cohesive with the art. If the instinct is to call a new function or draw a new shape, STOP and instead ask: "How can I make what's already here more of a piece of art?"

Take a second pass. Go back to the code and refine/polish further to make this a philosophically designed masterpiece.

## MULTI-PAGE OPTION

To create additional pages when requested, create more creative pages along the same lines as the design philosophy but distinctly different as well. Bundle those pages in the same .pdf or many .pngs. Treat the first page as just a single page in a whole coffee table book waiting to be filled. Make the next pages unique twists and memories of the original. Have them almost tell a story in a very tasteful way. Exercise full creative freedom.

---

## MODE B — BLUEPRINT DIAGRAM SLIDE (.html)

One pastel accent, ink line work, lowercase geometric type, and a technical-drawing
apparatus (registration crosshairs + a dimension line stating a true count) around a
free-form diagram. The style is **topic-agnostic** — apply it to any single-concept
diagram, regardless of subject.

### Workflow

1. **Copy `assets/blueprint-template.html`** as the starting point for the new slide.
2. **Pick the one thing this slide is about.** Everything downstream — the accent
   swatch, the filled diagram element, the dimension label — points at this one
   thing. If two things feel equally important, split into two slides.
3. **Pick one accent pair** from `references/blueprint-tokens.md` (sage / blue /
   rose / butter) and delete the unused ones from `:root`. Never fill two elements
   with accent on the same slide.
4. **Draw the diagram** in the SVG stage: ink outline for structure, `--line`
   hairlines for internal dividers, the accent fill on exactly one element, mono
   uppercase labels (flip to ink on top of the accent fill).
5. **Add the apparatus**: registration crosshairs at the diagram's bounding-box
   corners, and one dimension line below (or beside) it with a label stating a real
   count — "4 cells · 1 accent", "6 nodes · 1 root". Not decorative filler; it must
   be true of the diagram.
6. **Title**: lowercase, weight 200, preceded by the accent swatch.
7. **Caption**: one sentence, states the rule the slide demonstrates, not a
   description of the picture.
8. Save the finished slide as a single self-contained `.html` file (inline CSS, no
   external assets) — if it's joining an existing deck folder, match that deck's
   numbering convention (`NN-slug.html`) and viewBox/padding.
9. **Open it in the browser** — see OPENING HTML OUTPUT below. Every Mode B slide
   ends with this step; producing the file is not the end of the task.

Full CSS variables, exact typography spec, and the apparatus geometry are in
`references/blueprint-tokens.md` — read it before drawing the diagram if any rule
above is ambiguous, especially the registration-crosshair math and the
dimension-line rule.

### Resources

- `references/blueprint-tokens.md` — the complete token/rule spec (color, type,
  layout skeleton, apparatus geometry, caption voice, numbering convention).
- `assets/blueprint-template.html` — a blank slide with all CSS and structural HTML
  in place; the SVG stage and title/caption text are the only parts left to fill in.

---

## OPENING HTML OUTPUT

Any `.html` file this skill produces (always true for Mode B; never true for Mode A,
which only ever outputs `.png`/`.pdf`) must be opened in the user's browser as the
final step, via `bash_` — don't just report the file path and stop. Use a
platform-aware one-liner so this works on both supported platforms (macOS, WSL):

```bash
FILE="/absolute/path/to/slide.html"
case "$(uname -s)" in
  Darwin) open "$FILE" ;;
  *) command -v wslview >/dev/null 2>&1 && wslview "$FILE" \
       || cmd.exe /c start "" "$(wslpath -w "$FILE")" ;;
esac
```

`open`/`wslview`/`cmd.exe /c start` all hand off to the OS and return immediately —
none of them block waiting for the browser to close, so this is safe to run
unconditionally from `bash_` without hanging the turn.