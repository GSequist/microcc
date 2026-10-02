---
name: docx
description: "Comprehensive document creation, editing, and analysis with support for tracked changes, comments, formatting preservation, and text extraction. Use when working with professional documents (.docx files) for: (1) Creating new documents, (2) Modifying or editing content, (3) Working with tracked changes, (4) Adding comments, or any other document tasks"
---

# DOCX Creation, Editing, and Analysis

## Overview

A user may ask you to create, edit, or analyze the contents of a .docx file. A .docx file is essentially a ZIP archive containing XML files. You have different tools and workflows available for different tasks.

## Quick Tip: Markdown → DOCX via Pandoc (Preferred for Creation)

The easiest way to create a .docx is to write **Markdown** (which is native to you) and convert with pandoc:

```bash
pandoc input.md -o output.docx
```

This preserves all formatting beautifully — headings, bold, italic, lists, tables, code blocks — with zero python-docx boilerplate. You can then post-hoc enhance the result: fetch a logo from the web, add it via python-docx, adjust styles, etc.

**Use this approach first** unless the user needs fine-grained control (tracked changes, precise styling, brand colors) that requires python-docx or raw XML.

## Workflow Decision Tree

### Reading/Analyzing Content
Use "Text extraction" section below

### Creating New Document
- **Default/fastest**: Write Markdown → `pandoc input.md -o output.docx` (see tip above)
- **Fine-grained control**: Use "Creating a new Word document" workflow with python-docx

### Editing Existing Document
- **Any text edit**: paper-docx span API (`find_one(...).replace(...)`) — keeps run formatting
- **Someone else's document / legal, academic, business docs**: tracked edits (`tracked=True, author=...`)
- **Redline between two versions**: `docx.package.compare`
- **Raw XML** ([ooxml.md](ooxml.md)): last resort only, when paper refuses and the user accepts the risk

`docx` here is **paper-docx** — an import-compatible fork of python-docx (same `import docx`,
all stock APIs work) that adds guarded edit APIs. Every added operation either does exactly
what it claims or raises a typed `docx.errors.PaperRefusal` and leaves the document untouched.
Treat a refusal as information: narrow the target, don't route around it with XML edits.

## Reading and Analyzing Content

### Text Extraction
Convert document to markdown using pandoc:

```bash
# Convert document to markdown with tracked changes
pandoc --track-changes=all path-to-file.docx -o output.md
# Options: --track-changes=accept/reject/all
```

### Raw XML Access
Read-only inspection of embedded media, metadata, or anything `outline` reports as blind — unpack the docx. (For comments and tracked changes use the paper APIs below.)

```python
import zipfile
import defusedxml.minidom
from pathlib import Path

def unpack_docx(input_file, output_dir="unpacked"):
    """Unpack and pretty-print XML contents of Office files."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(input_file).extractall(output_path)

    for xml_file in list(output_path.rglob("*.xml")) + list(output_path.rglob("*.rels")):
        content = xml_file.read_text(encoding="utf-8")
        dom = defusedxml.minidom.parseString(content)
        xml_file.write_bytes(dom.toprettyxml(indent="  ", encoding="ascii"))

    return output_path

unpacked = unpack_docx("document.docx")
```

Key file structures:
- `word/document.xml` - Main document contents
- `word/comments.xml` - Comments
- `word/media/` - Embedded images
- Tracked changes: `<w:ins>` (insertions), `<w:del>` (deletions)

### Perceive Everything
Stock `doc.paragraphs` only sees the body. `outline` walks every story (tracked insertions,
text boxes, content controls, footnotes) and reports what it could not read:

```python
from docx.story import outline
o = outline(doc)
o.blind_region_counts          # {"tracked_insertions": 2, "text_boxes": 1, ...}
for rev in doc.revisions:      # every tracked change across all story parts
    print(rev.revision_type, rev.author, repr(rev.text))
```

## Creating a New Word Document

Use python-docx API (provided by paper-docx) for creating documents:

```python
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# Set page margins
for section in doc.sections:
    section.left_margin = Inches(1)
    section.right_margin = Inches(1)
    section.top_margin = Inches(1)
    section.bottom_margin = Inches(0.75)

# Create centered bold title
title = doc.add_paragraph()
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
run = title.add_run("Document Title")
run.bold = True
run.font.name = "Verdana"
run.font.size = Pt(10)

# Create normal paragraph
para = doc.add_paragraph()
para.paragraph_format.space_after = Pt(12)
para.paragraph_format.line_spacing = 1.15
run = para.add_run("Paragraph text here.")
run.font.name = "Verdana"
run.font.size = Pt(10)

doc.save("output.docx")
```

### For Complex Documents (JavaScript)
Read [docx-js.md](docx-js.md) for Document, Paragraph, TextRun components.

## Editing an Existing Document

### Text Edits (formatting-safe)
Search sees text across split runs; replace keeps untouched prefix/suffix runs and their
formatting. Ambiguous matches refuse instead of picking the first.

```python
import docx
from docx.search import find_one, find_text, replace_all
from docx.errors import PaperRefusal, AmbiguousTargetError

doc = docx.Document("existing.docx")
find_one(doc, "rate: $75–100/hr").replace("rate: $85–110/hr")
replace_all(doc, "Acme GmbH", "Acme AG")
find_text(doc, "Payment terms", near="Renewal")   # inspect repeated text, ranked by context
# find_one(doc, "the") -> AmbiguousTargetError: make the needle more specific, or pass story=/nth=
```

A successful replace consumes its span — re-find before the next edit on the same text.

### Tracked Changes (Redlining)
**Principle: minimal, precise edits** — replace only the words that change, not the whole
sentence.

```python
find_one(doc, "thirty calendar days").replace("forty-five calendar days", tracked=True, author="Claude")
replace_all(doc, "Licensor", "Provider", tracked=True, author="Claude")

from docx.blocks import insert_section_after, tracked_delete_paragraphs
from docx.story import iter_blocks
anchor = next(b for b in iter_blocks(doc) if b.text == "2. Termination")
insert_section_after(doc, anchor, heading="3. Notice", paragraphs=["Notices must be in writing."],
                     tracked=True, author="Claude")

doc.revisions.accept_all()                    # or reject_all(author="Bob")
```

### Comments
```python
find_one(doc, "30 days").comment("Is 30 days market standard?", author="Claude")
```
`docx.commentops` has `reply`, `resolve`, `delete_comment`, `comment_thread`.

### Redline Two Versions
```python
from docx.package import compare
result = compare("original.docx", "revised.docx", author="Reviewer")
result.document.save("redline.docx")          # native w:ins/w:del, proven by accept/reject
```
`compare` refuses when non-body package parts differ (e.g. `docProps/core.xml` timestamps,
a dropped empty rels file). Pandoc-generated or independently saved files often trip this:
normalize both through paper first — `docx.Document(p).save(p)` — then compare. Changed
table cells may come out as a coarse whole-block replace; for surgical redlines prefer
`replace(..., tracked=True)` on one document.

### Saving an Edit to Someone Else's File
`doc.save()` normalizes the whole package. To keep every untouched part byte-identical:

```python
from docx.package import patch_save
patch_save("existing.docx", doc, "edited.docx")   # returns restored/changed/added/removed parts
```

### Verify
```bash
pandoc --track-changes=all edited.docx -o verification.md
```

### Raw XML (last resort)
Only when a paper API refuses and there is no narrower target: unpack, edit with the patterns
in [ooxml.md](ooxml.md), repack. Tell the user this path has no corruption guard.

```python
import zipfile, shutil
from pathlib import Path
zipfile.ZipFile("document.docx").extractall("unpacked")      # edit unpacked/word/document.xml
shutil.make_archive("edited", "zip", "unpacked"); Path("edited.zip").rename("edited.docx")
```

## Converting Documents to Images

```bash
# DOCX to PDF
soffice --headless --convert-to pdf document.docx

# PDF to JPEG images
pdftoppm -jpeg -r 150 document.pdf page
# Creates page-1.jpg, page-2.jpg, etc.
```

## Code Style Guidelines
- Write concise code
- Avoid verbose variable names
- Avoid unnecessary print statements

## Dependencies

- **pandoc**: `apt-get install pandoc` (text extraction)
- **paper-docx**: `pip uninstall -y python-docx && pip install paper-docx` (creating/editing; provides `import docx` — never install both)
- **LibreOffice**: `apt-get install libreoffice` (PDF conversion)
- **Poppler**: `apt-get install poppler-utils` (PDF to images)
- **defusedxml**: `pip install defusedxml` (secure XML parsing)
