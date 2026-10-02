#!/usr/bin/env python3
"""Stage 3: paste each finished slide SVG into an empty deck, one full-bleed picture per slide.

Each picture carries two images: a real PNG fallback (rasterized with cairosvg) and the SVG
itself via PowerPoint's svgBlip extension. Microsoft 365 renders the SVG, so right-click ->
"Convert to Shape" explodes it into editable native shapes; older viewers show the PNG.

Usage:
    python build_deck.py <svg_dir> <output.pptx>

Slides are ordered by filename (01-*.svg, 02-*.svg, ...). Write every SVG with
viewBox="0 0 1280 720" — 1 unit = 1/96in on the 13.333in x 7.5in 16:9 canvas.

Requires: paper-pptx (import name `pptx`), cairosvg.
"""

import io
import sys
from pathlib import Path

try:
    import cairosvg
except ImportError:
    sys.exit("Missing dependency: pip install cairosvg")
from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.util import Inches

SVG_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_SVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
BLANK_LAYOUT = 6


def add_svg_slide(prs, svg: bytes):
    slide = prs.slides.add_slide(prs.slide_layouts[BLANK_LAYOUT])
    png = cairosvg.svg2png(bytestring=svg, output_width=2560, output_height=1440)
    pic = slide.shapes.add_picture(io.BytesIO(png), 0, 0, prs.slide_width, prs.slide_height)
    package = prs.part.package
    svg_part = Part(package.next_partname("/ppt/media/image%d.svg"), "image/svg+xml", package, svg)
    rid = slide.part.relate_to(svg_part, RT.IMAGE)
    ext_lst = etree.SubElement(pic._element.blipFill.blip, f"{{{NS_A}}}extLst")
    ext = etree.SubElement(ext_lst, f"{{{NS_A}}}ext", uri=SVG_EXT_URI)
    svg_blip = etree.SubElement(ext, f"{{{NS_SVG}}}svgBlip", nsmap={"asvg": NS_SVG})
    svg_blip.set(f"{{{NS_R}}}embed", rid)


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: python build_deck.py <svg_dir> <output.pptx>")
    svg_dir, output = Path(sys.argv[1]), Path(sys.argv[2])
    files = sorted(svg_dir.glob("*.svg"))
    if not files:
        sys.exit(f"No .svg files found in {svg_dir}")

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    with prs.batch():
        for f in files:
            add_svg_slide(prs, f.read_bytes())
    prs.save(output)
    print(f"Wrote {len(files)} slides -> {output}")


if __name__ == "__main__":
    main()
