#!/usr/bin/env python3
"""Map a user template: slide size, theme colours/fonts, every layout's placeholders, sample slides.

Usage:
    python inspect_template.py brand.pptx|.potx [--render renders/]

Prints JSON (geometry in px at 96dpi). --render also writes renders/layout-NN.jpg (one slide
per layout, each placeholder labelled "idx N · TYPE") and renders/sample-NN.jpg (the template's
own slides) for a vision pass.
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lxml import etree
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from template_ops import open_deck, px

NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def theme(prs) -> dict:
    root = etree.fromstring(prs.slide_master.part.part_related_by(RT.THEME).blob)
    colors = {}
    for el in root.find(".//a:clrScheme", NS):
        val = el.find("a:srgbClr", NS)
        sys_ = el.find("a:sysClr", NS)
        colors[etree.QName(el).localname] = val.get("val") if val is not None else (sys_.get("lastClr") if sys_ is not None else None)
    major = root.find(".//a:majorFont/a:latin", NS)
    minor = root.find(".//a:minorFont/a:latin", NS)
    return {
        "colors": colors,
        "heading_font": major.get("typeface") if major is not None else None,
        "body_font": minor.get("typeface") if minor is not None else None,
    }


def placeholders(shapes) -> list[dict]:
    return [
        {
            "idx": p.placeholder_format.idx,
            "type": p.placeholder_format.type.name if p.placeholder_format.type else None,
            "name": p.name,
            "x": px(p.left), "y": px(p.top), "w": px(p.width), "h": px(p.height),
        }
        for p in shapes.placeholders
    ]


def inspect(prs) -> dict:
    layouts = [lay for m in prs.slide_masters for lay in m.slide_layouts]
    used = {}
    for s in prs.slides:
        used[s.slide_layout.name] = used.get(s.slide_layout.name, 0) + 1
    return {
        "slide_px": [px(prs.slide_width), px(prs.slide_height)],
        "theme": theme(prs),
        "layouts": [
            {"index": i, "name": lay.name, "used_by_samples": used.get(lay.name, 0), "placeholders": placeholders(lay)}
            for i, lay in enumerate(layouts)
        ],
        "sample_slides": [
            {
                "index": i,
                "layout": s.slide_layout.name,
                "shapes": [
                    {
                        "name": sh.name,
                        "placeholder_idx": sh.placeholder_format.idx if sh.is_placeholder else None,
                        "kind": sh.shape_type.name if sh.shape_type else None,
                        "x": px(sh.left), "y": px(sh.top), "w": px(sh.width), "h": px(sh.height),
                        "text": sh.text_frame.text[:120] if sh.has_text_frame else None,
                    }
                    for sh in s.shapes
                ],
            }
            for i, s in enumerate(prs.slides)
        ],
    }


def render(path: Path, outdir: Path) -> list[Path]:
    """One labelled slide per layout + the template's own slides, as JPEGs."""
    from deck_to_images import convert

    prs = open_deck(path)
    n_sample = len(prs.slides)
    for lay in [lay for m in prs.slide_masters for lay in m.slide_layouts]:
        slide = prs.slides.add_slide(lay)
        for p in slide.placeholders:
            if p.has_text_frame:
                p.text_frame.text = f"idx {p.placeholder_format.idx} · {p.placeholder_format.type.name if p.placeholder_format.type else '?'}"
    with tempfile.TemporaryDirectory() as tmp:
        layouts_deck = Path(tmp) / "layouts.pptx"
        prs.save(layouts_deck)
        imgs = convert(layouts_deck, "all", Path(tmp) / "r")
        outdir.mkdir(parents=True, exist_ok=True)
        for stale in [*outdir.glob("sample-*.jpg"), *outdir.glob("layout-*.jpg")]:
            stale.unlink()
        out = []
        for i, img in enumerate(imgs):
            name = f"sample-{i + 1:02d}.jpg" if i < n_sample else f"layout-{i - n_sample:02d}.jpg"
            out.append(img.replace(outdir / name))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("template", type=Path)
    ap.add_argument("--render", type=Path, metavar="OUTDIR")
    args = ap.parse_args()
    print(json.dumps(inspect(open_deck(args.template)), indent=1))
    if args.render:
        for p in render(args.template, args.render):
            print(p, file=sys.stderr)


if __name__ == "__main__":
    main()
