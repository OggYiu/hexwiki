"""Run the auditable PDF extractor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from hexwiki.extraction.pdf import ExtractionOptions, extract


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("pdf", type=Path, help="source PDF")
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--ocr", choices=("none", "missing", "all"), default="all")
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument(
        "--ocr-workers", type=int, default=max(1, min(4, os.cpu_count() or 1))
    )
    parser.add_argument("--ocr-psm", type=int, default=3)
    parser.add_argument("--tesseract")
    parser.add_argument("--no-ocr-image-fallback", action="store_true")
    parser.add_argument("--no-svg", action="store_true")
    parser.add_argument(
        "--poppler", choices=("auto", "never", "required"), default="auto"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse compatible page/OCR artifacts in an existing output directory",
    )


def run(args: argparse.Namespace) -> int:
    options = ExtractionOptions(
        input_pdf=args.pdf,
        output_dir=args.output,
        render_dpi=args.dpi,
        ocr_mode=args.ocr,
        ocr_language=args.ocr_language,
        ocr_workers=args.ocr_workers,
        ocr_psm=args.ocr_psm,
        tesseract=args.tesseract,
        ocr_image_fallback=not args.no_ocr_image_fallback,
        save_svg=not args.no_svg,
        poppler_mode=args.poppler,
        resume=args.resume,
    )
    extract(options)
    return 0
