# Office Open XML Technical Reference

XML patterns for working with .docx files directly. Use this when implementing tracked changes (redlining) or advanced document manipulation.

## Technical Guidelines

### Schema Compliance
- **Element ordering in `<w:pPr>`**: `<w:pStyle>`, `<w:numPr>`, `<w:spacing>`, `<w:ind>`, `<w:jc>`
- **Whitespace**: Add `xml:space='preserve'` to `<w:t>` elements with leading/trailing spaces
- **Unicode**: Escape characters in ASCII content: `"` becomes `&#8220;`
  - **Character encoding reference**: Curly quotes `""` become `&#8220;&#8221;`, apostrophe `'` becomes `&#8217;`, em-dash `—` becomes `&#8212;`
- **Tracked changes**: Use `<w:del>` and `<w:ins>` tags with `w:author="Claude"` outside `<w:r>` elements
  - **Critical**: `<w:ins>` closes with `</w:ins>`, `<w:del>` closes with `</w:del>` - never mix
  - **RSIDs must be 8-digit hex**: Use values like `00AB1234` (only 0-9, A-F characters)
  - **trackRevisions placement**: Add `<w:trackRevisions/>` after `<w:proofState>` in settings.xml
- **Images**: Add to `word/media/`, reference in `document.xml`, set dimensions to prevent overflow

## Document Content Patterns

### Basic Structure
```xml
<w:p>
  <w:r><w:t>Text content</w:t></w:r>
</w:p>
```

### Headings and Styles
```xml
<w:p>
  <w:pPr>
    <w:pStyle w:val="Title"/>
    <w:jc w:val="center"/>
  </w:pPr>
  <w:r><w:t>Document Title</w:t></w:r>
</w:p>

<w:p>
  <w:pPr><w:pStyle w:val="Heading2"/></w:pPr>
  <w:r><w:t>Section Heading</w:t></w:r>
</w:p>
```

### Text Formatting
```xml
<!-- Bold -->
<w:r><w:rPr><w:b/><w:bCs/></w:rPr><w:t>Bold</w:t></w:r>
<!-- Italic -->
<w:r><w:rPr><w:i/><w:iCs/></w:rPr><w:t>Italic</w:t></w:r>
<!-- Underline -->
<w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>Underlined</w:t></w:r>
<!-- Highlight -->
<w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr><w:t>Highlighted</w:t></w:r>
```

### Lists
```xml
<!-- Numbered list -->
<w:p>
  <w:pPr>
    <w:pStyle w:val="ListParagraph"/>
    <w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>
    <w:spacing w:before="240"/>
  </w:pPr>
  <w:r><w:t>First item</w:t></w:r>
</w:p>

<!-- Restart numbered list at 1 - use different numId -->
<w:p>
  <w:pPr>
    <w:pStyle w:val="ListParagraph"/>
    <w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>
  </w:pPr>
  <w:r><w:t>New list item 1</w:t></w:r>
</w:p>

<!-- Bullet list (level 2) -->
<w:p>
  <w:pPr>
    <w:pStyle w:val="ListParagraph"/>
    <w:numPr><w:ilvl w:val="1"/><w:numId w:val="1"/></w:numPr>
    <w:ind w:left="900"/>
  </w:pPr>
  <w:r><w:t>Bullet item</w:t></w:r>
</w:p>
```

### Tables
```xml
<w:tbl>
  <w:tblPr>
    <w:tblStyle w:val="TableGrid"/>
    <w:tblW w:w="0" w:type="auto"/>
  </w:tblPr>
  <w:tblGrid>
    <w:gridCol w:w="4675"/><w:gridCol w:w="4675"/>
  </w:tblGrid>
  <w:tr>
    <w:tc>
      <w:tcPr><w:tcW w:w="4675" w:type="dxa"/></w:tcPr>
      <w:p><w:r><w:t>Cell 1</w:t></w:r></w:p>
    </w:tc>
    <w:tc>
      <w:tcPr><w:tcW w:w="4675" w:type="dxa"/></w:tcPr>
      <w:p><w:r><w:t>Cell 2</w:t></w:r></w:p>
    </w:tc>
  </w:tr>
</w:tbl>
```

### Layout
```xml
<!-- Page break -->
<w:p>
  <w:r><w:br w:type="page"/></w:r>
</w:p>

<!-- Centered paragraph -->
<w:p>
  <w:pPr>
    <w:spacing w:before="240" w:after="0"/>
    <w:jc w:val="center"/>
  </w:pPr>
  <w:r><w:t>Centered text</w:t></w:r>
</w:p>

<!-- Font change - run level -->
<w:r>
  <w:rPr><w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/></w:rPr>
  <w:t>Monospace text</w:t>
</w:r>
```

## File Updates

When adding content, update these files:

**`word/_rels/document.xml.rels`:**
```xml
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
```

**`[Content_Types].xml`:**
```xml
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
```

### Images
**CRITICAL**: Calculate dimensions to prevent page overflow and maintain aspect ratio.

```xml
<w:p>
  <w:r>
    <w:drawing>
      <wp:inline>
        <wp:extent cx="2743200" cy="1828800"/>
        <wp:docPr id="1" name="Picture 1"/>
        <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:nvPicPr>
                <pic:cNvPr id="0" name="image1.png"/>
                <pic:cNvPicPr/>
              </pic:nvPicPr>
              <pic:blipFill>
                <a:blip r:embed="rId5"/>
                <a:stretch><a:fillRect/></a:stretch>
              </pic:blipFill>
              <pic:spPr>
                <a:xfrm><a:ext cx="2743200" cy="1828800"/></a:xfrm>
                <a:prstGeom prst="rect"/>
              </pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline>
    </w:drawing>
  </w:r>
</w:p>
```

### Hyperlinks

**External Links:**
```xml
<!-- In document.xml -->
<w:hyperlink r:id="rId5">
  <w:r>
    <w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>
    <w:t>Link Text</w:t>
  </w:r>
</w:hyperlink>

<!-- In word/_rels/document.xml.rels -->
<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
              Target="https://www.example.com/" TargetMode="External"/>
```

**Internal Links:**
```xml
<!-- Link to bookmark -->
<w:hyperlink w:anchor="myBookmark">
  <w:r>
    <w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>
    <w:t>Link Text</w:t>
  </w:r>
</w:hyperlink>

<!-- Bookmark target -->
<w:bookmarkStart w:id="0" w:name="myBookmark"/>
<w:r><w:t>Target content</w:t></w:r>
<w:bookmarkEnd w:id="0"/>
```

**Hyperlink Style (required in styles.xml):**
```xml
<w:style w:type="character" w:styleId="Hyperlink">
  <w:name w:val="Hyperlink"/>
  <w:basedOn w:val="DefaultParagraphFont"/>
  <w:uiPriority w:val="99"/>
  <w:unhideWhenUsed/>
  <w:rPr>
    <w:color w:val="467886" w:themeColor="hyperlink"/>
    <w:u w:val="single"/>
  </w:rPr>
</w:style>
```

## Tracked Changes (Redlining)

### Enable Track Revisions

Add to `word/settings.xml` after `<w:proofState>`:
```xml
<w:trackRevisions/>
```

### Required Attributes

All tracked change elements need these attributes:
- `w:id` - Unique integer ID within document
- `w:author` - Author name (e.g., "Claude")
- `w:date` - ISO 8601 timestamp (e.g., "2025-01-15T10:00:00Z")
- `w:rsidR` or `w:rsidDel` - 8-digit hex (e.g., "00AB1234")

### Tracked Change Patterns

**CRITICAL RULES**:
1. **XML Structure**: Place `<w:del>` and `<w:ins>` at paragraph level containing complete `<w:r>` elements. Never nest inside `<w:r>`.
2. **Minimal edits**: Only mark text that actually changes. Keep unchanged text outside tracked change tags.
3. **Preserve formatting**: Copy `<w:rPr>` from original runs when creating tracked changes.

**Text Insertion:**
```xml
<w:ins w:id="1" w:author="Claude" w:date="2025-01-15T10:00:00Z">
  <w:r w:rsidR="00AB1234">
    <w:t>inserted text</w:t>
  </w:r>
</w:ins>
```

**Text Deletion:**
```xml
<w:del w:id="2" w:author="Claude" w:date="2025-01-15T10:00:00Z">
  <w:r w:rsidDel="00AB1234">
    <w:delText>deleted text</w:delText>
  </w:r>
</w:del>
```

**Replacement (delete + insert):**
```xml
<!-- Change "monthly" to "quarterly" -->
<w:r><w:t>The report is </w:t></w:r>
<w:del w:id="1" w:author="Claude" w:date="2025-01-15T10:00:00Z">
  <w:r w:rsidDel="00AB1234"><w:delText>monthly</w:delText></w:r>
</w:del>
<w:ins w:id="2" w:author="Claude" w:date="2025-01-15T10:00:00Z">
  <w:r w:rsidR="00AB1234"><w:t>quarterly</w:t></w:r>
</w:ins>
```

**Deleting Another Author's Insertion (nested structure required):**
```xml
<!-- Nest your deletion inside their insertion -->
<w:ins w:author="Jane Smith" w:id="16">
  <w:del w:author="Claude" w:id="40">
    <w:r><w:delText>their inserted text</w:delText></w:r>
  </w:del>
</w:ins>
```

**Restoring Another Author's Deletion:**
```xml
<!-- Leave their deletion unchanged, add new insertion after -->
<w:del w:author="Jane Smith" w:id="50">
  <w:r><w:delText>deleted text</w:delText></w:r>
</w:del>
<w:ins w:author="Claude" w:id="51">
  <w:r><w:t>deleted text</w:t></w:r>
</w:ins>
```

### Comments

**Add to `word/comments.xml`:**
```xml
<w:comments>
  <w:comment w:id="0" w:author="Claude" w:date="2025-01-15T10:00:00Z" w:initials="C">
    <w:p>
      <w:r><w:t>Comment text here</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>
```

**Reference in `document.xml`:**
```xml
<w:commentRangeStart w:id="0"/>
<w:r><w:t>Commented text</w:t></w:r>
<w:commentRangeEnd w:id="0"/>
<w:r>
  <w:commentReference w:id="0"/>
</w:r>
```

**Update relationships** (`word/_rels/document.xml.rels`):
```xml
<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
```

**Update content types** (`[Content_Types].xml`):
```xml
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
```
