#!/usr/bin/env python3
"""Render every slide of a .pptx (or .pdf) to a JPEG, one file per slide.

Used two places in the pptx skill:
  - Stage 0: turn a user-supplied reference deck into images for a vision pass.
  - Final QA: turn the assembled output deck into images for a visual check.

Usage:
    python deck_to_images.py input.pptx [output_prefix] [--dpi 150]

Produces output_prefix-01.jpg, output_prefix-02.jpg, ... in the input's directory
(or wherever output_prefix points).
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from office.soffice import run_soffice


def convert(input_path: Path, prefix: str, dpi: int) -> list[Path]:
    workdir = input_path.parent

    pdf_path = input_path.with_suffix(".pdf")
    if input_path.suffix.lower() != ".pdf":
        result = run_soffice(
            ["--headless", "--convert-to", "pdf", "--outdir", str(workdir), str(input_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"soffice conversion failed:\n{result.stderr}")
    else:
        pdf_path = input_path

    subprocess.run(
        ["pdftoppm", "-jpeg", "-r", str(dpi), str(pdf_path), str(workdir / prefix)],
        check=True,
    )

    return sorted(workdir.glob(f"{prefix}-*.jpg"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("prefix", nargs="?", default="slide")
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()

    images = convert(args.input, args.prefix, args.dpi)
    for img in images:
        print(img)


if __name__ == "__main__":
    main()
