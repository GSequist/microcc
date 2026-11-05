#!/usr/bin/env python3
"""Render every slide of a .pptx/.potx/.pdf to JPEGs (<outdir>/<prefix>-NN.jpg) for a vision pass.

Usage:
    python deck_to_images.py input.pptx [prefix] [--outdir renders] [--dpi 150]
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from office.soffice import run_soffice


def convert(input_path: Path, prefix: str, outdir: Path, dpi: int = 150) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    for stale in outdir.glob(f"{prefix}-*.jpg"):
        stale.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        pdf = input_path
        if input_path.suffix.lower() != ".pdf":
            # soffice names the pdf after the input stem; convert in a temp dir so nothing lands next to the input
            result = run_soffice(
                ["--headless", "--convert-to", "pdf", "--outdir", tmp, str(input_path)],
                capture_output=True, text=True,
            )
            pdf = Path(tmp) / f"{input_path.stem}.pdf"
            if result.returncode != 0 or not pdf.exists():
                raise RuntimeError(f"soffice conversion failed:\n{result.stderr}")
        subprocess.run(["pdftoppm", "-jpeg", "-r", str(dpi), str(pdf), str(outdir / prefix)], check=True)
    return sorted(outdir.glob(f"{prefix}-*.jpg"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("prefix", nargs="?", default="slide")
    parser.add_argument("--outdir", type=Path, default=Path("renders"))
    parser.add_argument("--dpi", type=int, default=150)
    args = parser.parse_args()
    if not shutil.which("pdftoppm"):
        sys.exit("Missing dependency: Poppler (pdftoppm)")
    for img in convert(args.input, args.prefix, args.outdir, args.dpi):
        print(img)


if __name__ == "__main__":
    main()
