#!/usr/bin/env python3
"""Rasterize slide SVGs to PNG so they can be inspected with the Read tool.

Fast preview path for Stage 2 (before assembly) — cairosvg's rendering is not
pixel-identical to PowerPoint/LibreOffice, but it's accurate enough to catch
overflow, overlap, and contrast problems slide-by-slide as they're written.

Usage:
    python render_svg.py slides/                  # renders every *.svg in the dir
    python render_svg.py slides/03-market.svg      # renders a single file
    python render_svg.py slides/ --scale 2         # 2x resolution (default)
"""

import argparse
import sys
from pathlib import Path

try:
    import cairosvg
except ImportError:
    sys.exit("Missing dependency: pip install cairosvg")


def render(svg_path: Path, scale: float) -> Path:
    png_path = svg_path.with_suffix(".png")
    cairosvg.svg2png(url=str(svg_path), write_to=str(png_path), scale=scale)
    return png_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", type=Path, help="an .svg file or a directory of them")
    parser.add_argument("--scale", type=float, default=2.0)
    args = parser.parse_args()

    if args.target.is_dir():
        svg_files = sorted(args.target.glob("*.svg"))
    else:
        svg_files = [args.target]

    if not svg_files:
        sys.exit(f"No .svg files found at {args.target}")

    for svg_path in svg_files:
        png_path = render(svg_path, args.scale)
        print(png_path)


if __name__ == "__main__":
    main()
