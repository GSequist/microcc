#!/usr/bin/env python3
"""Paste each slide SVG into a deck as one full-bleed picture (SVG + PNG fallback) per slide.

Microsoft 365 renders the SVG (right-click -> "Convert to Shape" makes it editable); older
viewers show the PNG. With --template the slides use the template's blank layout, so its
master (background, logo, footer furniture) stays; the template's own sample slides are dropped.

Usage:
    python build_deck.py <svg_dir> <output.pptx> [--template brand.pptx|.potx]

Slides are ordered by filename (01-*.svg, ...). Every SVG uses viewBox="0 0 1280 720".
"""

import argparse
import io
import re
import sys
from pathlib import Path

try:
    import cairosvg
except ImportError:
    sys.exit("Missing dependency: pip install cairosvg")
from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.util import Inches

SVG_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_SVG = "http://schemas.microsoft.com/office/drawing/2016/SVG/main"
FALLBACK_W = 1920


def add_svg_picture(slide, svg: bytes, x, y, w, h, name: str = "slide", descr: str = ""):
    """Add an SVG picture (svgBlip) with a PNG fallback at x/y/w/h (EMU)."""
    png = cairosvg.svg2png(bytestring=svg, output_width=FALLBACK_W, output_height=round(FALLBACK_W * h / w))
    pic = slide.shapes.add_picture(io.BytesIO(png), x, y, w, h)
    pic.name = name
    pic._element.nvPicPr.cNvPr.set("descr", descr or name)
    package = slide.part.package
    svg_part = Part(package.next_partname("/ppt/media/image%d.svg"), "image/svg+xml", package, svg)
    rid = slide.part.relate_to(svg_part, RT.IMAGE)
    ext_lst = etree.SubElement(pic._element.blipFill.blip, f"{{{NS_A}}}extLst")
    ext = etree.SubElement(ext_lst, f"{{{NS_A}}}ext", uri=SVG_EXT_URI)
    svg_blip = etree.SubElement(ext, f"{{{NS_SVG}}}svgBlip", nsmap={"asvg": NS_SVG})
    svg_blip.set(f"{{{NS_R}}}embed", rid)
    return pic


def blank_layout(prs):
    """Layout with the fewest non-footer placeholders (named 'Blank' wins)."""
    layouts = [lay for m in prs.slide_masters for lay in m.slide_layouts]
    for lay in layouts:
        if lay.name.strip().lower() == "blank":
            return lay
    footers = {PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}
    return min(layouts, key=lambda lay: sum(p.placeholder_format.type not in footers for p in lay.placeholders))


def headline(svg: bytes) -> str:
    """Best-effort alt text: the svg <title>, else the first <text>."""
    root = etree.fromstring(svg)
    for tag in ("title", "text"):
        el = root.find(f".//{{http://www.w3.org/2000/svg}}{tag}")
        if el is not None and "".join(el.itertext()).strip():
            return " ".join("".join(el.itertext()).split())
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("svg_dir", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("--template", type=Path)
    args = ap.parse_args()
    files = sorted(args.svg_dir.glob("*.svg"))
    if not files:
        sys.exit(f"No .svg files found in {args.svg_dir}")
    bad = [f.name for f in files if not re.search(rb'viewBox="0 0 1280 720"', f.read_bytes())]
    if bad:
        sys.exit(f'viewBox must be "0 0 1280 720": {bad}')

    if args.template:
        sys.path.insert(0, str(Path(__file__).parent))
        from template_ops import open_deck
        prs = open_deck(args.template)
        if abs(prs.slide_width / prs.slide_height - 16 / 9) > 0.01:
            sys.exit(f"template is not 16:9 ({prs.slide_width}x{prs.slide_height} EMU); use the template path in template.md")
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    n_sample = len(prs.slides)
    lay = blank_layout(prs)

    with prs.batch():
        for i in reversed(range(n_sample)):
            prs.slides.delete(i)
        for f in files:
            svg = f.read_bytes()
            slide = prs.slides.add_slide(lay)
            for p in list(slide.placeholders):  # blank layouts may still carry footer placeholders
                p._element.getparent().remove(p._element)
            add_svg_picture(slide, svg, 0, 0, prs.slide_width, prs.slide_height, name=f.stem, descr=headline(svg))
    prs.save(args.output)
    print(f"Wrote {len(files)} slides -> {args.output}" + (f" (dropped {n_sample} template sample slides)" if n_sample else ""))


if __name__ == "__main__":
    main()
