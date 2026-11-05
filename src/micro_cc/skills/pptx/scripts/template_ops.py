"""Helpers for building a deck on a user's template (.pptx/.potx) with paper-pptx.

Import from a deck-build script:
    sys.path.insert(0, "<skill>/scripts"); from template_ops import *
"""

import copy
import io
import math
import zipfile
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx import inspect as pi
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.oxml.ns import qn
from pptx.util import Emu

EMU_PER_PX = 9525  # 96dpi
POTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml"
PPTX_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"


def open_deck(path: str | Path):
    """Open a .pptx, or a .potx by retyping its main part in memory (python-pptx refuses .potx)."""
    raw = Path(path).read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as zin:
        ct = zin.read("[Content_Types].xml")
        if POTX_CT.encode() not in ct:
            return Presentation(io.BytesIO(raw))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "[Content_Types].xml":
                    data = data.replace(POTX_CT.encode(), PPTX_CT.encode())
                zout.writestr(item, data)
    return Presentation(io.BytesIO(out.getvalue()))


def px(emu: int | None) -> int | None:
    return None if emu is None else round(emu / EMU_PER_PX)


def layout(prs, name: str):
    """Layout by exact name across all masters."""
    for master in prs.slide_masters:
        for lay in master.slide_layouts:
            if lay.name == name:
                return lay
    names = [lay.name for m in prs.slide_masters for lay in m.slide_layouts]
    raise KeyError(f"no layout {name!r}; have {names}")


def ph(slide, idx: int):
    """Slide placeholder by idx (idx values come from inspect_template.py)."""
    for p in slide.placeholders:
        if p.placeholder_format.idx == idx:
            return p
    raise KeyError(f"no placeholder idx={idx} on slide; have {[p.placeholder_format.idx for p in slide.placeholders]}")


def fill(shape, paras: list[str | tuple[int, str]]):
    """Write paragraphs into a shape; each item is text or (level, text).

    Formatting is cloned from the shape's existing paragraph/run at the same position (else the
    last one), so placeholders keep inheriting the master and cloned sample slides keep local styling.
    """
    tx = shape.text_frame._txBody
    old = tx.findall(qn("a:p"))
    for p in old:
        tx.remove(p)
    for i, item in enumerate(paras):
        level, text = item if isinstance(item, tuple) else (None, item)
        src = old[min(i, len(old) - 1)] if old else None
        p = etree.SubElement(tx, qn("a:p"))
        src_ppr = src.find(qn("a:pPr")) if src is not None else None
        ppr = copy.deepcopy(src_ppr) if src_ppr is not None else (etree.Element(qn("a:pPr")) if level is not None else None)
        if ppr is not None:
            if level is not None:
                ppr.set("lvl", str(level))
            p.append(ppr)  # pPr must precede runs
        r = etree.SubElement(p, qn("a:r"))
        rpr = src.find(f"{qn('a:r')}/{qn('a:rPr')}") if src is not None else None
        if rpr is not None:
            r.append(copy.deepcopy(rpr))
        etree.SubElement(r, qn("a:t")).text = text
    return shape


def _bbox(shape) -> tuple[int, int, int, int]:
    return shape.left, shape.top, shape.width, shape.height


def _remove(shape):
    shape._element.getparent().remove(shape._element)


def place_svg(slide, target, svg: bytes, name: str = "diagram", descr: str = ""):
    """Replace a placeholder/shape with an SVG picture fitted (aspect kept, centred) into its bbox.

    Author the SVG viewBox at the placeholder's px w×h (from inspect_template.py) to fill it exactly.
    """
    from build_deck import add_svg_picture
    x, y, w, h = _bbox(target)
    _remove(target)
    vb = etree.fromstring(svg).get("viewBox")
    if vb:
        vw, vh = (float(v) for v in vb.replace(",", " ").split()[2:4])
        scale = min(w / vw, h / vh)
        fw, fh = int(vw * scale), int(vh * scale)
        x, y, w, h = x + (w - fw) // 2, y + (h - fh) // 2, fw, fh
    return add_svg_picture(slide, svg, x, y, w, h, name=name, descr=descr)


def place_chart(slide, target, chart_type, chart_data):
    """Replace a placeholder with a native chart (editable data, picks up theme colours)."""
    x, y, w, h = _bbox(target)
    _remove(target)
    return slide.shapes.add_chart(chart_type, x, y, w, h, chart_data).chart


def place_table(slide, target, rows: list[list[str]]):
    """Replace a placeholder with a native table (styled by the template's default table style)."""
    x, y, w, h = _bbox(target)
    _remove(target)
    table = slide.shapes.add_table(len(rows), len(rows[0]), x, y, w, Emu(min(h, len(rows) * 370840))).table
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            table.cell(r, c).text = val
    return table


def drop_empty_placeholders(slide, keep_footers: bool = True):
    """Remove unfilled placeholders so no 'Click to add text' prompts ship."""
    footers = {PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}
    for p in list(slide.placeholders):
        if keep_footers and p.placeholder_format.type in footers:
            continue
        if p.has_text_frame and not p.text_frame.text.strip():
            _remove(p)


def delete_slides(prs, indices: list[int]):
    """Delete slides by 0-based index (e.g. the template's original sample slides)."""
    for i in sorted(indices, reverse=True):
        prs.slides.delete(i)


def overflow_report(prs, width_factor: float = 0.55) -> list[str]:
    """Flag text shapes whose estimated wrapped height exceeds the box (resolved font size via pptx.inspect)."""
    issues = []
    for n, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if not shape.has_text_frame or not shape.text_frame.text.strip() or shape.width is None:
                continue
            tf = shape.text_frame
            avail_w = shape.width - (tf.margin_left or 0) - (tf.margin_right or 0)
            avail_h = shape.height - (tf.margin_top or 0) - (tf.margin_bottom or 0)
            need_h = 0
            for para in tf.paragraphs:
                runs = para.runs
                try:
                    size = pi.effective_font(runs[0]).size.value if runs else None
                except Exception:
                    size = None
                size = size or 18 * 12700
                indent = (para.level or 0) * 457200
                chars = len(para.text) or 1
                lines = max(1, math.ceil(chars * width_factor * size / max(avail_w - indent, 1)))
                need_h += lines * size * 1.2
            if need_h > avail_h:
                issues.append(f"slide {n} '{shape.name}': ~{need_h / avail_h:.0%} of box height — cut text or split")
    return issues
