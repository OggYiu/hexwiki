#!/usr/bin/env python3
"""Extract text, images, layout, vectors, and visual page captures from a PDF.

The extractor deliberately keeps overlapping representations:

* native text in PDF content order and approximate reading order;
* character-level layout data;
* native, rendered, and raw-stream forms of embedded images;
* vector drawing commands and SVG page snapshots;
* page PNGs suitable for OCR and visual auditing;
* optional exhaustive Tesseract OCR;
* optional independent Poppler text/image reports.

This redundancy makes omissions detectable and preserves content that one PDF
extraction strategy alone may not expose.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import pymupdf as fitz


EXTRACTOR_VERSION = "1.0.0"
FORM_FEED_SEPARATOR = "\n\f\n"


@dataclasses.dataclass(frozen=True)
class ExtractionOptions:
    input_pdf: Path
    output_dir: Path
    render_dpi: int = 300
    ocr_mode: str = "all"
    ocr_language: str = "eng"
    ocr_workers: int = 4
    ocr_psm: int = 3
    tesseract: str | None = None
    ocr_image_fallback: bool = True
    save_svg: bool = True
    poppler_mode: str = "auto"
    resume: bool = False


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))


def json_ready(value: Any) -> Any:
    """Convert PyMuPDF values and byte strings into deterministic JSON values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {
            "byte_length": len(value),
            "sha256": sha256_bytes(value),
        }
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_ready(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    try:
        return [json_ready(item) for item in value]
    except TypeError:
        return str(value)


def write_json(path: Path, value: Any) -> None:
    text = json.dumps(json_ready(value), ensure_ascii=False, indent=2, sort_keys=True)
    write_text(path, f"{text}\n")


def relative_to_output(path: Path, output_dir: Path) -> str:
    return path.relative_to(output_dir).as_posix()


def clean_extension(extension: str | None, fallback: str = "bin") -> str:
    candidate = (extension or fallback).lower().lstrip(".")
    candidate = re.sub(r"[^a-z0-9]+", "", candidate)
    return candidate or fallback


def safe_name(name: str, fallback: str) -> str:
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    return candidate or fallback


def command_version(command: str) -> str | None:
    try:
        result = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else None


def resolve_command(explicit: str | None, default: str) -> str | None:
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate.resolve())
        discovered = shutil.which(explicit)
        return discovered
    return shutil.which(default)


def require_new_or_resumable_output(path: Path, resume: bool) -> None:
    if path.exists() and not path.is_dir():
        raise ValueError(f"Output path exists and is not a directory: {path}")
    if path.exists() and not resume:
        raise ValueError(
            f"Output directory already exists: {path}. "
            "Choose another directory or pass --resume."
        )
    path.mkdir(parents=True, exist_ok=True)


def extract_rawdict_images(
    rawdict: dict[str, Any],
    page_number: int,
    output_dir: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Write image bytes present in rawdict blocks and replace them with references."""
    records: list[dict[str, Any]] = []
    blocks = rawdict.get("blocks", [])
    for block_index, block in enumerate(blocks):
        data = block.get("image")
        if block.get("type") != 1 or not isinstance(data, bytes):
            continue

        digest = sha256_bytes(data)
        extension = clean_extension(block.get("ext"))
        image_path = (
            output_dir
            / "images"
            / "block-images"
            / f"image-{digest[:24]}.{extension}"
        )
        if not image_path.exists():
            write_bytes(image_path, data)

        block.pop("image", None)
        block["image_file"] = relative_to_output(image_path, output_dir)
        block["image_sha256"] = digest
        block["image_bytes"] = len(data)
        records.append(
            {
                "page": page_number,
                "block_index": block_index,
                "file": relative_to_output(image_path, output_dir),
                "sha256": digest,
                "bytes": len(data),
                "extension": extension,
                "bbox": block.get("bbox"),
                "transform": block.get("transform"),
                "width": block.get("width"),
                "height": block.get("height"),
                "colorspace": block.get("colorspace"),
                "bpc": block.get("bpc"),
                "xres": block.get("xres"),
                "yres": block.get("yres"),
            }
        )
    return rawdict, records


def annotation_records(page: fitz.Page) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    annotation = page.first_annot
    while annotation is not None:
        record: dict[str, Any] = {
            "xref": annotation.xref,
            "type": annotation.type,
            "rect": annotation.rect,
            "flags": annotation.flags,
            "info": annotation.info,
            "colors": annotation.colors,
            "opacity": annotation.opacity,
        }
        try:
            record["vertices"] = annotation.vertices
        except (RuntimeError, ValueError):
            record["vertices"] = None
        records.append(json_ready(record))
        annotation = annotation.next
    return records


def widget_records(page: fitz.Page) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    widgets = page.widgets()
    if widgets is None:
        return records
    for widget in widgets:
        records.append(
            json_ready(
                {
                    "xref": widget.xref,
                    "field_name": widget.field_name,
                    "field_type": widget.field_type,
                    "field_type_string": widget.field_type_string,
                    "field_value": widget.field_value,
                    "field_flags": widget.field_flags,
                    "field_label": widget.field_label,
                    "rect": widget.rect,
                }
            )
        )
    return records


def raw_character_count(rawdict: dict[str, Any]) -> int:
    count = 0
    for block in rawdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                count += len(span.get("chars", []))
    return count


def extract_content_streams(
    doc: fitz.Document,
    xrefs: Iterable[int],
    output_dir: Path,
    already_written: set[int],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw_dir = output_dir / "raw" / "content-streams"
    for xref in xrefs:
        record: dict[str, Any] = {"xref": xref}
        object_path = raw_dir / f"xref-{xref:06d}-object.txt"
        decoded_path = raw_dir / f"xref-{xref:06d}-decoded.pdfops"
        encoded_path = raw_dir / f"xref-{xref:06d}-encoded.bin"

        if xref not in already_written:
            write_text(object_path, doc.xref_object(xref, compressed=False))
            decoded = doc.xref_stream(xref)
            encoded = doc.xref_stream_raw(xref)
            write_bytes(decoded_path, decoded)
            write_bytes(encoded_path, encoded)
            already_written.add(xref)

        for key, path in (
            ("object_file", object_path),
            ("decoded_stream_file", decoded_path),
            ("encoded_stream_file", encoded_path),
        ):
            record[key] = relative_to_output(path, output_dir)
        record["decoded_sha256"] = sha256_file(decoded_path)
        record["encoded_sha256"] = sha256_file(encoded_path)
        records.append(record)
    return records


def collect_image_xrefs(doc: fitz.Document) -> list[int]:
    xrefs: list[int] = []
    for xref in range(1, doc.xref_length()):
        try:
            subtype = doc.xref_get_key(xref, "Subtype")[1]
        except (RuntimeError, ValueError):
            continue
        if subtype == "/Image":
            xrefs.append(xref)
    return xrefs


def collect_smask_references(doc: fitz.Document) -> dict[int, int]:
    references: dict[int, int] = {}
    for page in doc:
        for item in page.get_images(full=True):
            xref, smask = int(item[0]), int(item[1])
            if xref > 0 and smask > 0:
                references[xref] = smask
    return references


def rendered_image_pixmap(
    doc: fitz.Document,
    xref: int,
    smask_xref: int | None,
) -> fitz.Pixmap:
    base = fitz.Pixmap(doc, xref)
    if smask_xref:
        mask = fitz.Pixmap(doc, smask_xref)
        combined = fitz.Pixmap(base, mask)
        base = combined
    # PNG can only carry DeviceGray or DeviceRGB, and the test for that is the
    # colourspace's identity, not its channel count. A Separation (spot colour)
    # plate can report a low channel count while still being unsuitable for PNG;
    # a channel-count test therefore lets it fail later inside MuPDF.
    if base.colorspace is not None and base.colorspace.name not in (
        fitz.csGRAY.name, fitz.csRGB.name
    ):
        base = fitz.Pixmap(fitz.csRGB, base)
    return base


def extract_embedded_images(
    doc: fitz.Document,
    output_dir: Path,
    referenced_pages: dict[int, set[int]],
) -> list[dict[str, Any]]:
    image_xrefs = collect_image_xrefs(doc)
    smasks = collect_smask_references(doc)
    records: list[dict[str, Any]] = []
    native_dir = output_dir / "images" / "embedded-native"
    rendered_dir = output_dir / "images" / "embedded-rendered"
    raw_dir = output_dir / "raw" / "image-streams"

    for index, xref in enumerate(image_xrefs, start=1):
        record: dict[str, Any] = {
            "index": index,
            "xref": xref,
            "referenced_on_pages": sorted(referenced_pages.get(xref, set())),
            "smask_xref": smasks.get(xref),
        }

        object_path = raw_dir / f"image-xref-{xref:06d}-object.txt"
        encoded_path = raw_dir / f"image-xref-{xref:06d}-encoded.bin"
        write_text(object_path, doc.xref_object(xref, compressed=False))
        encoded = doc.xref_stream_raw(xref)
        write_bytes(encoded_path, encoded)
        record["object_file"] = relative_to_output(object_path, output_dir)
        record["encoded_stream_file"] = relative_to_output(encoded_path, output_dir)
        record["encoded_stream_bytes"] = len(encoded)
        record["encoded_stream_sha256"] = sha256_bytes(encoded)

        try:
            image = doc.extract_image(xref)
            native_data = image.pop("image")
            extension = clean_extension(image.get("ext"))
            native_path = native_dir / f"image-xref-{xref:06d}.{extension}"
            write_bytes(native_path, native_data)
            record["native_file"] = relative_to_output(native_path, output_dir)
            record["native_bytes"] = len(native_data)
            record["native_sha256"] = sha256_bytes(native_data)
            record["native_properties"] = image
        except (RuntimeError, ValueError) as error:
            record["native_extraction_error"] = str(error)

        try:
            pixmap = rendered_image_pixmap(doc, xref, smasks.get(xref))
            rendered_path = rendered_dir / f"image-xref-{xref:06d}.png"
            rendered_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap.save(rendered_path)
            record["rendered_file"] = relative_to_output(rendered_path, output_dir)
            record["rendered_bytes"] = rendered_path.stat().st_size
            record["rendered_sha256"] = sha256_file(rendered_path)
            record["rendered_width"] = pixmap.width
            record["rendered_height"] = pixmap.height
        except Exception as error:
            # Deliberately broad. MuPDF raises its own exception hierarchy
            # (FzErrorArgument and friends) which is neither RuntimeError nor
            # ValueError, so the narrower catch let a single unrenderable image
            # abort the whole run after 440 pages of work. One bad image is a
            # recorded defect on one record; it is not a reason to lose the
            # extraction, and validation still reports it because the record
            # carries the error instead of a rendered file.
            record["rendered_extraction_error"] = f"{type(error).__name__}: {error}"

        records.append(json_ready(record))

    write_json(output_dir / "images" / "embedded-images.json", records)
    return records


def extract_attachments(
    doc: fitz.Document,
    output_dir: Path,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    names = doc.embfile_names()
    for index, name in enumerate(names, start=1):
        data = doc.embfile_get(name)
        filename = safe_name(name, f"attachment-{index:04d}.bin")
        path = output_dir / "attachments" / filename
        write_bytes(path, data)
        records.append(
            {
                "name": name,
                "file": relative_to_output(path, output_dir),
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "info": doc.embfile_info(name),
            }
        )
    if records:
        write_json(output_dir / "attachments" / "attachments.json", records)
    return records


def page_filename(page_number: int, digits: int, suffix: str) -> str:
    return f"page-{page_number:0{digits}d}{suffix}"


def extract_pages(
    doc: fitz.Document,
    options: ExtractionOptions,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, set[int]]]:
    output_dir = options.output_dir
    digits = max(4, len(str(doc.page_count)))
    page_records: list[dict[str, Any]] = []
    image_occurrences: list[dict[str, Any]] = []
    referenced_pages: dict[int, set[int]] = {}
    content_streams_written: set[int] = set()
    native_pages: list[str] = []
    reading_pages: list[str] = []

    for page_index, page in enumerate(doc):
        page_number = page_index + 1
        stem = page_filename(page_number, digits, "")
        native_text = page.get_text("text", sort=False)
        reading_text = page.get_text("text", sort=True)
        rawdict = page.get_text("rawdict", sort=False)
        raw_characters = raw_character_count(rawdict)
        rawdict, block_images = extract_rawdict_images(
            rawdict, page_number, output_dir
        )

        native_path = output_dir / "text" / "native-pages" / f"{stem}.txt"
        reading_path = (
            output_dir / "text" / "reading-order-pages" / f"{stem}.txt"
        )
        layout_path = output_dir / "layout" / f"{stem}.json"
        vector_path = output_dir / "vectors" / f"{stem}.json"
        render_path = output_dir / "pages" / f"{stem}.png"
        svg_path = output_dir / "pages-svg" / f"{stem}.svg"

        write_text(native_path, native_text)
        write_text(reading_path, reading_text)
        native_pages.append(native_text)
        reading_pages.append(reading_text)

        image_info = page.get_image_info(hashes=True, xrefs=True)
        page_image_records: list[dict[str, Any]] = []
        for occurrence_index, info in enumerate(image_info, start=1):
            occurrence = {
                "page": page_number,
                "occurrence": occurrence_index,
                **json_ready(info),
            }
            xref = int(info.get("xref", 0) or 0)
            if xref > 0:
                referenced_pages.setdefault(xref, set()).add(page_number)
            page_image_records.append(occurrence)
            image_occurrences.append(occurrence)

        content_stream_records = extract_content_streams(
            doc,
            page.get_contents(),
            output_dir,
            content_streams_written,
        )

        vectors = page.get_drawings(extended=True)
        write_json(
            vector_path,
            {
                "page": page_number,
                "drawing_count": len(vectors),
                "drawings": vectors,
            },
        )

        links = page.get_links()
        annotations = annotation_records(page)
        widgets = widget_records(page)
        words = page.get_text("words", sort=False)
        text_blocks = page.get_text("blocks", sort=False)

        layout = {
            "page": page_number,
            "rotation": page.rotation,
            "rect": page.rect,
            "mediabox": page.mediabox,
            "cropbox": page.cropbox,
            "bleedbox": page.bleedbox,
            "trimbox": page.trimbox,
            "artbox": page.artbox,
            "native_text_characters": len(native_text),
            "reading_order_text_characters": len(reading_text),
            "raw_layout_characters": raw_characters,
            "rawdict": rawdict,
            "words": words,
            "text_blocks": text_blocks,
            "image_occurrences": page_image_records,
            "rawdict_image_blocks": block_images,
            "links": links,
            "annotations": annotations,
            "widgets": widgets,
            "content_streams": content_stream_records,
        }
        write_json(layout_path, layout)

        pixmap = page.get_pixmap(
            dpi=options.render_dpi,
            colorspace=fitz.csRGB,
            alpha=False,
            annots=True,
        )
        if not (options.resume and render_path.exists()):
            render_path.parent.mkdir(parents=True, exist_ok=True)
            pixmap.save(render_path)

        if options.save_svg and not (options.resume and svg_path.exists()):
            svg = page.get_svg_image(text_as_path=False)
            write_text(svg_path, svg)

        record = {
            "page": page_number,
            "native_text_file": relative_to_output(native_path, output_dir),
            "native_text_characters": len(native_text),
            "native_text_sha256": sha256_file(native_path),
            "reading_order_text_file": relative_to_output(
                reading_path, output_dir
            ),
            "reading_order_text_characters": len(reading_text),
            "reading_order_text_sha256": sha256_file(reading_path),
            "raw_layout_characters": raw_characters,
            "layout_file": relative_to_output(layout_path, output_dir),
            "vector_file": relative_to_output(vector_path, output_dir),
            "vector_drawing_count": len(vectors),
            "image_occurrence_count": len(page_image_records),
            "rawdict_image_block_count": len(block_images),
            "link_count": len(links),
            "annotation_count": len(annotations),
            "widget_count": len(widgets),
            "content_stream_xrefs": list(page.get_contents()),
            "render_file": relative_to_output(render_path, output_dir),
            "render_width": pixmap.width,
            "render_height": pixmap.height,
            "render_bytes": render_path.stat().st_size,
            "render_sha256": sha256_file(render_path),
            "svg_file": (
                relative_to_output(svg_path, output_dir)
                if options.save_svg
                else None
            ),
        }
        page_records.append(record)

        if page_number == 1 or page_number % 10 == 0 or page_number == doc.page_count:
            print(
                f"[extract] page {page_number}/{doc.page_count}",
                flush=True,
            )

    write_text(
        output_dir / "text" / "full-native.txt",
        FORM_FEED_SEPARATOR.join(native_pages),
    )
    write_text(
        output_dir / "text" / "full-reading-order.txt",
        FORM_FEED_SEPARATOR.join(reading_pages),
    )
    write_json(output_dir / "pages.json", page_records)
    write_json(output_dir / "images" / "occurrences.json", image_occurrences)
    return page_records, image_occurrences, referenced_pages


def ocr_page(
    page_number: int,
    digits: int,
    page_png: Path,
    ocr_dir: Path,
    command: str,
    language: str,
    dpi: int,
    psm: int,
    resume: bool,
) -> dict[str, Any]:
    stem = page_filename(page_number, digits, "")
    output_base = ocr_dir / stem
    text_path = output_base.with_suffix(".txt")
    tsv_path = output_base.with_suffix(".tsv")
    hocr_path = output_base.with_suffix(".hocr")

    expected = (text_path, tsv_path, hocr_path)
    if not (resume and all(path.exists() for path in expected)):
        ocr_dir.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [
                command,
                str(page_png),
                str(output_base),
                "-l",
                language,
                "--dpi",
                str(dpi),
                "--psm",
                str(psm),
                "txt",
                "tsv",
                "hocr",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"Tesseract failed on page {page_number}: "
                f"{process.stderr.strip()}"
            )
        missing = [str(path) for path in expected if not path.exists()]
        if missing:
            raise RuntimeError(
                f"Tesseract did not create expected files for page "
                f"{page_number}: {missing}"
            )
        stderr = process.stderr.strip()
    else:
        stderr = "resumed existing OCR files"

    text = text_path.read_text(encoding="utf-8", errors="replace")
    return {
        "page": page_number,
        "text_file": text_path.name,
        "tsv_file": tsv_path.name,
        "hocr_file": hocr_path.name,
        "text_characters": len(text),
        "text_sha256": sha256_file(text_path),
        "tsv_sha256": sha256_file(tsv_path),
        "hocr_sha256": sha256_file(hocr_path),
        "diagnostic": stderr,
    }


def ocr_variant(
    page_number: int,
    rotation: int,
    psm: int,
    source_image: Path,
    output_dir: Path,
    command: str,
    language: str,
    dpi: int,
    resume: bool,
) -> dict[str, Any]:
    digits = max(4, len(str(page_number)))
    stem = (
        f"page-{page_number:0{digits}d}-rot{rotation:03d}-psm{psm:02d}"
    )
    output_base = output_dir / "ocr" / "fallback" / "pages" / stem
    text_path = output_base.with_suffix(".txt")
    tsv_path = output_base.with_suffix(".tsv")
    hocr_path = output_base.with_suffix(".hocr")
    expected = (text_path, tsv_path, hocr_path)

    if not (resume and all(path.exists() for path in expected)):
        output_base.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.run(
            [
                command,
                str(source_image),
                str(output_base),
                "-l",
                language,
                "--dpi",
                str(dpi),
                "--psm",
                str(psm),
                "txt",
                "tsv",
                "hocr",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"Tesseract fallback failed on page {page_number}, "
                f"rotation {rotation}, PSM {psm}: {process.stderr.strip()}"
            )
        missing = [str(path) for path in expected if not path.exists()]
        if missing:
            raise RuntimeError(
                f"Tesseract fallback did not create expected files: {missing}"
            )
        diagnostic = process.stderr.strip()
    else:
        diagnostic = "resumed existing OCR files"

    text = text_path.read_text(encoding="utf-8", errors="replace")
    return {
        "page": page_number,
        "rotation_degrees_counterclockwise": rotation,
        "psm": psm,
        "source_image": relative_to_output(source_image, output_dir),
        "text_file": relative_to_output(text_path, output_dir),
        "tsv_file": relative_to_output(tsv_path, output_dir),
        "hocr_file": relative_to_output(hocr_path, output_dir),
        "text_characters": len(text),
        "text_sha256": sha256_file(text_path),
        "tsv_sha256": sha256_file(tsv_path),
        "hocr_sha256": sha256_file(hocr_path),
        "diagnostic": diagnostic,
    }


def prepare_rotated_page_images(
    page_numbers: list[int],
    page_count: int,
    options: ExtractionOptions,
) -> dict[tuple[int, int], Path]:
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Pillow is required for multi-orientation image-page OCR"
        ) from error

    digits = max(4, len(str(page_count)))
    rotations = {
        90: Image.Transpose.ROTATE_90,
        180: Image.Transpose.ROTATE_180,
        270: Image.Transpose.ROTATE_270,
    }
    paths: dict[tuple[int, int], Path] = {}
    for page_number in page_numbers:
        original = (
            options.output_dir
            / "pages"
            / page_filename(page_number, digits, ".png")
        )
        paths[(page_number, 0)] = original
        with Image.open(original) as source:
            rgb = source.convert("RGB")
            for rotation, transpose in rotations.items():
                rotated_path = (
                    options.output_dir
                    / "ocr"
                    / "fallback"
                    / "rotated-pages"
                    / (
                        f"page-{page_number:0{digits}d}"
                        f"-rot{rotation:03d}.png"
                    )
                )
                if not (options.resume and rotated_path.exists()):
                    rotated_path.parent.mkdir(parents=True, exist_ok=True)
                    rotated = rgb.transpose(transpose)
                    rotated.save(rotated_path, format="PNG")
                paths[(page_number, rotation)] = rotated_path
    return paths


def run_image_orientation_fallback(
    page_count: int,
    page_records: list[dict[str, Any]],
    options: ExtractionOptions,
    command: str,
) -> dict[str, Any]:
    if not options.ocr_image_fallback:
        return {
            "enabled": False,
            "target_pages": [],
            "variants": [],
        }

    target_pages = [
        int(record["page"])
        for record in page_records
        if int(record["image_occurrence_count"]) > 0
        or int(record["native_text_characters"]) == 0
    ]
    rotations = (0, 90, 180, 270)
    psms = (6, 11)
    rotated_paths = prepare_rotated_page_images(
        target_pages, page_count, options
    )
    variants: list[dict[str, Any]] = []
    work_items = [
        (page_number, rotation, psm)
        for page_number in target_pages
        for rotation in rotations
        for psm in psms
    ]

    print(
        f"[ocr-fallback] processing {len(work_items)} orientation/PSM "
        f"variant(s) across {len(target_pages)} image-bearing or "
        f"native-text-empty page(s)",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=options.ocr_workers
    ) as executor:
        futures = {
            executor.submit(
                ocr_variant,
                page_number,
                rotation,
                psm,
                rotated_paths[(page_number, rotation)],
                options.output_dir,
                command,
                options.ocr_language,
                options.render_dpi,
                options.resume,
            ): (page_number, rotation, psm)
            for page_number, rotation, psm in work_items
        }
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            variants.append(future.result())
            completed += 1
            if completed == 1 or completed % 10 == 0 or completed == len(futures):
                print(
                    f"[ocr-fallback] completed {completed}/{len(futures)}",
                    flush=True,
                )

    variants.sort(
        key=lambda item: (
            item["page"],
            item["rotation_degrees_counterclockwise"],
            item["psm"],
        )
    )
    combined_sections: list[str] = []
    for record in variants:
        text_path = options.output_dir / record["text_file"]
        text = text_path.read_text(encoding="utf-8", errors="replace")
        header = (
            f"===== page {record['page']} | "
            f"rotation {record['rotation_degrees_counterclockwise']} CCW | "
            f"PSM {record['psm']} ====="
        )
        combined_sections.append(f"{header}\n{text}")

    full_path = options.output_dir / "ocr" / "fallback" / "full-fallback.txt"
    write_text(full_path, "\n\n".join(combined_sections))
    result = {
        "enabled": True,
        "purpose": (
            "Redundant OCR for rotated/sparse text inside raster image pages. "
            "Variants are kept separate because some modes can produce noise."
        ),
        "target_pages": target_pages,
        "rotations_degrees_counterclockwise": list(rotations),
        "page_segmentation_modes": list(psms),
        "variants": variants,
        "variants_with_text": sum(
            1 for record in variants if record["text_characters"] > 0
        ),
        "full_text_file": relative_to_output(full_path, options.output_dir),
        "full_text_sha256": sha256_file(full_path),
    }
    write_json(
        options.output_dir / "ocr" / "fallback" / "fallback.json",
        result,
    )
    return result


def run_ocr(
    doc_page_count: int,
    page_records: list[dict[str, Any]],
    options: ExtractionOptions,
) -> dict[str, Any]:
    if options.ocr_mode == "none":
        return {
            "mode": "none",
            "target_pages": [],
            "pages": [],
            "tesseract": None,
            "image_orientation_fallback": {
                "enabled": False,
                "target_pages": [],
                "variants": [],
            },
        }

    command = resolve_command(options.tesseract, "tesseract")
    if not command:
        raise RuntimeError(
            "Tesseract is required for OCR but was not found. "
            "Install it, pass --tesseract PATH, or choose --ocr none."
        )

    if options.ocr_mode == "all":
        target_pages = [record["page"] for record in page_records]
    else:
        target_pages = [
            record["page"]
            for record in page_records
            if record["native_text_characters"] == 0
        ]

    output_dir = options.output_dir
    ocr_dir = output_dir / "ocr" / "pages"
    pages_dir = output_dir / "pages"
    digits = max(4, len(str(doc_page_count)))
    records: list[dict[str, Any]] = []

    print(
        f"[ocr] processing {len(target_pages)} page(s) with "
        f"{options.ocr_workers} worker(s)",
        flush=True,
    )
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=options.ocr_workers
    ) as executor:
        future_to_page = {
            executor.submit(
                ocr_page,
                page_number,
                digits,
                pages_dir / page_filename(page_number, digits, ".png"),
                ocr_dir,
                command,
                options.ocr_language,
                options.render_dpi,
                options.ocr_psm,
                options.resume,
            ): page_number
            for page_number in target_pages
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_to_page):
            records.append(future.result())
            completed += 1
            if completed == 1 or completed % 10 == 0 or completed == len(target_pages):
                print(
                    f"[ocr] completed {completed}/{len(target_pages)}",
                    flush=True,
                )

    records.sort(key=lambda item: item["page"])
    record_by_page = {record["page"]: record for record in records}
    combined: list[str] = []
    for page_number in range(1, doc_page_count + 1):
        record = record_by_page.get(page_number)
        if record is None:
            combined.append("")
            continue
        path = ocr_dir / record["text_file"]
        combined.append(path.read_text(encoding="utf-8", errors="replace"))

    full_path = output_dir / "ocr" / "full-ocr.txt"
    write_text(full_path, FORM_FEED_SEPARATOR.join(combined))
    fallback = run_image_orientation_fallback(
        doc_page_count,
        page_records,
        options,
        command,
    )
    result = {
        "mode": options.ocr_mode,
        "language": options.ocr_language,
        "psm": options.ocr_psm,
        "workers": options.ocr_workers,
        "tesseract": command,
        "tesseract_version": command_version(command),
        "target_pages": target_pages,
        "pages": records,
        "full_text_file": relative_to_output(full_path, output_dir),
        "full_text_characters": len(FORM_FEED_SEPARATOR.join(combined)),
        "full_text_sha256": sha256_file(full_path),
        "image_orientation_fallback": fallback,
    }
    write_json(output_dir / "ocr" / "ocr.json", result)
    return result


def run_command_report(
    command: list[str],
    report_path: Path,
    display_command: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        check=False,
    )
    report = [
        f"command: {subprocess.list2cmdline(display_command or command)}",
        f"exit_code: {result.returncode}",
        "",
        "[stdout]",
        result.stdout,
        "",
        "[stderr]",
        result.stderr,
    ]
    write_text(report_path, "\n".join(report))
    return result


def run_poppler_verification(options: ExtractionOptions) -> dict[str, Any]:
    if options.poppler_mode == "never":
        return {"mode": "never", "available": False}

    tools = {
        name: shutil.which(name)
        for name in ("pdfinfo", "pdftotext", "pdfimages", "pdffonts")
    }
    missing = [name for name, command in tools.items() if not command]
    if missing:
        if options.poppler_mode == "required":
            raise RuntimeError(
                f"Required Poppler tools not found: {', '.join(missing)}"
            )
        return {
            "mode": options.poppler_mode,
            "available": False,
            "missing_tools": missing,
        }

    verification_dir = options.output_dir / "verification" / "poppler"
    verification_dir.mkdir(parents=True, exist_ok=True)
    source = str(options.input_pdf)
    reports: dict[str, Any] = {}

    for tool_name in ("pdfinfo", "pdfimages", "pdffonts"):
        report_path = verification_dir / f"{tool_name}.txt"
        result = run_command_report(
            [str(tools[tool_name]), source]
            if tool_name != "pdfimages"
            else [str(tools[tool_name]), "-list", source],
            report_path,
            [tool_name, "<source.pdf>"]
            if tool_name != "pdfimages"
            else [tool_name, "-list", "<source.pdf>"],
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{tool_name} verification failed; see {report_path}"
            )
        reports[tool_name] = {
            "command": tool_name,
            "report_file": relative_to_output(
                report_path, options.output_dir
            ),
        }
        if tool_name == "pdfimages":
            entries = [
                line
                for line in result.stdout.splitlines()
                if re.match(r"^\s*\d+\s+\d+\s+\w+", line)
            ]
            # A soft mask is the alpha channel of its parent image, not a
            # separate picture. pdfimages lists it as its own row while PyMuPDF
            # attaches it to the parent. Counting drawable content on both sides
            # makes the comparison like-for-like.
            drawable = [
                line for line in entries
                if len(line.split()) < 3 or line.split()[2] != "smask"
            ]
            reports[tool_name]["listed_image_occurrences"] = len(drawable)
            reports[tool_name]["listed_rows_total"] = len(entries)
            reports[tool_name]["listed_soft_masks"] = len(entries) - len(drawable)

    poppler_images_dir = verification_dir / "images"
    poppler_images_dir.mkdir(parents=True, exist_ok=True)
    poppler_image_prefix = poppler_images_dir / "image"
    poppler_image_report = verification_dir / "pdfimages-extract-report.txt"
    poppler_image_result = run_command_report(
        [
            str(tools["pdfimages"]),
            "-all",
            source,
            str(poppler_image_prefix),
        ],
        poppler_image_report,
        ["pdfimages", "-all", "<source.pdf>", "<output-prefix>"],
    )
    if poppler_image_result.returncode != 0:
        raise RuntimeError(
            f"Independent pdfimages extraction failed; "
            f"see {poppler_image_report}"
        )
    poppler_image_files = sorted(
        path for path in poppler_images_dir.glob("image-*") if path.is_file()
    )
    reports["pdfimages"]["extraction_report_file"] = relative_to_output(
        poppler_image_report, options.output_dir
    )
    reports["pdfimages"]["extracted_files"] = [
        {
            "file": relative_to_output(path, options.output_dir),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in poppler_image_files
    ]
    reports["pdfimages"]["extracted_file_count"] = len(poppler_image_files)

    text_outputs: dict[str, Any] = {}
    for mode, flag in (("layout", "-layout"), ("raw", "-raw")):
        text_path = verification_dir / f"pdftotext-{mode}.txt"
        report_path = verification_dir / f"pdftotext-{mode}-report.txt"
        result = run_command_report(
            [
                str(tools["pdftotext"]),
                flag,
                "-enc",
                "UTF-8",
                source,
                str(text_path),
            ],
            report_path,
            ["pdftotext", flag, "-enc", "UTF-8", "<source.pdf>", "<output.txt>"],
        )
        if result.returncode != 0 or not text_path.exists():
            raise RuntimeError(
                f"pdftotext {mode} verification failed; see {report_path}"
            )
        text = text_path.read_text(encoding="utf-8", errors="replace")
        text_outputs[mode] = {
            "text_file": relative_to_output(text_path, options.output_dir),
            "report_file": relative_to_output(report_path, options.output_dir),
            "characters": len(text),
            "sha256": sha256_file(text_path),
            "form_feed_count": text.count("\f"),
        }

    return {
        "mode": options.poppler_mode,
        "available": True,
        "tools": reports,
        "text_outputs": text_outputs,
    }


def document_metadata(
    doc: fitz.Document,
    options: ExtractionOptions,
    source_hash: str,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    xml_path: str | None = None
    xml_metadata = doc.get_xml_metadata()
    if xml_metadata:
        path = options.output_dir / "metadata.xml"
        write_text(path, xml_metadata)
        xml_path = relative_to_output(path, options.output_dir)

    return {
        "extractor": {
            "name": "HexWiki PDF extractor",
            "version": EXTRACTOR_VERSION,
            "pymupdf_version": fitz.VersionBind,
            "python_version": sys.version,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
        "source": {
            "path": options.input_pdf.name,
            "bytes": options.input_pdf.stat().st_size,
            "sha256": source_hash,
        },
        "pdf": {
            "page_count": doc.page_count,
            "metadata": doc.metadata,
            "xml_metadata_file": xml_path,
            "table_of_contents": doc.get_toc(simple=False),
            "page_labels": doc.get_page_labels(),
            "is_encrypted": doc.is_encrypted,
            "needs_password": bool(doc.needs_pass),
            "permissions": doc.permissions,
            "xref_length": doc.xref_length(),
            "pdf_catalog_xref": doc.pdf_catalog(),
            "embedded_file_count": doc.embfile_count(),
            "attachments": attachments,
        },
        "options": {
            "render_dpi": options.render_dpi,
            "ocr_mode": options.ocr_mode,
            "ocr_language": options.ocr_language,
            "ocr_workers": options.ocr_workers,
            "ocr_psm": options.ocr_psm,
            "tesseract_explicitly_selected": options.tesseract is not None,
            "ocr_image_fallback": options.ocr_image_fallback,
            "save_svg": options.save_svg,
            "poppler_mode": options.poppler_mode,
            "resume": options.resume,
        },
    }


def validate_outputs(
    doc: fitz.Document,
    options: ExtractionOptions,
    source_hash_before: str,
    page_records: list[dict[str, Any]],
    image_occurrences: list[dict[str, Any]],
    image_records: list[dict[str, Any]],
    ocr: dict[str, Any],
    poppler: dict[str, Any],
) -> dict[str, Any]:
    output_dir = options.output_dir
    page_count = doc.page_count
    image_xrefs = collect_image_xrefs(doc)
    extracted_xrefs = [int(record["xref"]) for record in image_records]
    pages_without_native = [
        record["page"]
        for record in page_records
        if not (
            output_dir / record["native_text_file"]
        ).read_text(encoding="utf-8", errors="replace").strip()
    ]
    ocr_by_page = {record["page"]: record for record in ocr.get("pages", [])}
    pages_without_ocr = [
        page_number
        for page_number in ocr.get("target_pages", [])
        if ocr_by_page.get(page_number, {}).get("text_characters", 0) == 0
    ]
    recovered_native_empty_pages = [
        page_number
        for page_number in pages_without_native
        if ocr_by_page.get(page_number, {}).get("text_characters", 0) > 0
    ]

    expected_page_files = {
        "native_text": [
            output_dir / record["native_text_file"] for record in page_records
        ],
        "reading_order_text": [
            output_dir / record["reading_order_text_file"]
            for record in page_records
        ],
        "layout": [
            output_dir / record["layout_file"] for record in page_records
        ],
        "vectors": [
            output_dir / record["vector_file"] for record in page_records
        ],
        "renders": [
            output_dir / record["render_file"] for record in page_records
        ],
    }
    if options.save_svg:
        expected_page_files["svg"] = [
            output_dir / str(record["svg_file"]) for record in page_records
        ]

    checks: dict[str, bool] = {
        "source_pdf_unchanged": sha256_file(options.input_pdf)
        == source_hash_before,
        "page_record_count_matches_pdf": len(page_records) == page_count,
        "all_pdf_image_xrefs_extracted": sorted(extracted_xrefs)
        == sorted(image_xrefs),
        "all_image_records_have_preserved_data": all(
            record.get("encoded_stream_file")
            and (record.get("native_file") or record.get("rendered_file"))
            for record in image_records
        ),
        "all_image_occurrences_have_page_numbers": all(
            1 <= int(record["page"]) <= page_count
            for record in image_occurrences
        ),
    }
    for name, paths in expected_page_files.items():
        checks[f"{name}_page_count_complete"] = (
            len(paths) == page_count and all(path.exists() for path in paths)
        )

    expected_ocr_count = len(ocr.get("target_pages", []))
    checks["ocr_target_count_complete"] = (
        options.ocr_mode == "none"
        or len(ocr.get("pages", [])) == expected_ocr_count
    )
    checks["ocr_all_requested_pages_have_outputs"] = (
        options.ocr_mode == "none"
        or all(
            (output_dir / "ocr" / "pages" / record[key]).exists()
            for record in ocr.get("pages", [])
            for key in ("text_file", "tsv_file", "hocr_file")
        )
    )
    fallback = ocr.get("image_orientation_fallback", {})
    expected_fallback_count = (
        len(fallback.get("target_pages", []))
        * len(fallback.get("rotations_degrees_counterclockwise", []))
        * len(fallback.get("page_segmentation_modes", []))
    )
    checks["ocr_image_orientation_fallback_complete"] = (
        not fallback.get("enabled")
        or (
            len(fallback.get("variants", [])) == expected_fallback_count
            and all(
                (output_dir / record[key]).exists()
                for record in fallback.get("variants", [])
                for key in ("text_file", "tsv_file", "hocr_file")
            )
        )
    )

    poppler_image_count = (
        poppler.get("tools", {})
        .get("pdfimages", {})
        .get("listed_image_occurrences")
    )
    if poppler.get("available") and poppler_image_count is not None:
        # Exact, once both sides count the same thing: drawable images, with soft
        # masks attributed to their parent rather than counted separately. Once
        # both tools count drawable occurrences, a divergence is a real
        # enumeration defect and should fail.
        checks["poppler_image_occurrences_match"] = (
            poppler_image_count == len(image_occurrences)
        )
        # Files actually written are a separate signal from what Poppler lists,
        # and here the assertion has to stay directional: pdfimages declines to
        # emit some CMYK and separation images at all. Fewer files is a limitation
        # of that tool; more files than we found would mean we missed content.
        extracted_file_count = (
            poppler.get("tools", {})
            .get("pdfimages", {})
            .get("extracted_file_count")
        )
        if extracted_file_count is not None:
            checks["poppler_wrote_no_images_we_missed"] = (
                extracted_file_count <= len(image_occurrences)
            )

    native_total = sum(
        int(record["native_text_characters"]) for record in page_records
    )
    raw_layout_total = sum(
        int(record["raw_layout_characters"]) for record in page_records
    )
    vector_total = sum(
        int(record["vector_drawing_count"]) for record in page_records
    )

    # A tolerated divergence must still be visible: the checks above only assert
    # that Poppler found nothing we missed, so the size of any shortfall is
    # recorded here rather than left for someone to recompute.
    poppler_image_delta = (
        len(image_occurrences) - poppler_image_count
        if poppler.get("available") and poppler_image_count is not None
        else None
    )

    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "poppler_image_shortfall": poppler_image_delta,
        "counts": {
            "pdf_pages": page_count,
            "page_records": len(page_records),
            "native_text_characters": native_total,
            "raw_layout_characters": raw_layout_total,
            "pages_without_native_text": pages_without_native,
            "pdf_image_object_xrefs": len(image_xrefs),
            "extracted_image_objects": len(image_records),
            "image_occurrences": len(image_occurrences),
            "vector_drawing_elements": vector_total,
            "ocr_target_pages": expected_ocr_count,
            "ocr_pages_without_text": pages_without_ocr,
            "native_empty_pages_with_ocr_text": recovered_native_empty_pages,
            "ocr_image_fallback_target_pages": len(
                fallback.get("target_pages", [])
            ),
            "ocr_image_fallback_variants": len(
                fallback.get("variants", [])
            ),
            "ocr_image_fallback_variants_with_text": fallback.get(
                "variants_with_text", 0
            ),
            "poppler_independent_image_files": (
                poppler.get("tools", {})
                .get("pdfimages", {})
                .get("extracted_file_count", 0)
            ),
        },
        "independent_poppler_verification": poppler,
    }


def write_checksum_inventory(output_dir: Path) -> dict[str, Any]:
    checksum_path = output_dir / "checksums.sha256"
    files = sorted(
        (
            path
            for path in output_dir.rglob("*")
            if path.is_file() and path != checksum_path
        ),
        key=lambda path: path.relative_to(output_dir).as_posix(),
    )
    lines = [
        f"{sha256_file(path)}  {path.relative_to(output_dir).as_posix()}"
        for path in files
    ]
    write_text(checksum_path, "\n".join(lines) + "\n")
    return {
        "file": relative_to_output(checksum_path, output_dir),
        "listed_files": len(files),
        "sha256": sha256_file(checksum_path),
    }


def extraction_readme(options: ExtractionOptions) -> str:
    return f"""PDF extraction output
=====================

Source filename: {options.input_pdf.name}
Render resolution: {options.render_dpi} DPI
OCR mode: {options.ocr_mode}

Important layers
----------------

* text/full-native.txt: native PDF text in content-stream order.
* text/full-reading-order.txt: native PDF text spatially sorted for reading.
* text/*-pages/: one native text file per page.
* layout/: character-level positions, fonts, words, blocks, links, and image uses.
* images/embedded-native/: conventional files decoded from PDF image objects.
* images/embedded-rendered/: PNG renderings, including soft masks when present.
* images/block-images/: image bytes exposed through page layout extraction.
* images/occurrences.json: every displayed image placement and bounding box.
* pages/: {options.render_dpi}-DPI PNG of every page for visual auditing and OCR.
* pages-svg/: vector-preserving page snapshots, when enabled.
* vectors/: PDF vector drawing commands for every page.
* raw/: original encoded image streams and page content streams.
* ocr/: Tesseract text, TSV coordinates/confidence, and hOCR markup.
* ocr/fallback/: multi-orientation PSM 6/11 OCR for all image-bearing pages.
* verification/poppler/: independent Poppler text and image extraction.
* validation.json: completeness checks and aggregate counts.
* checksums.sha256: SHA-256 inventory for every generated artifact.

Native text is authoritative for born-digital pages. OCR is a redundant supplement
that can recover text drawn inside images, but OCR errors are always possible.
The per-page PNG/SVG files and preserved raw streams provide a visual and binary
audit trail when exact transcription matters.
"""


def extract(options: ExtractionOptions) -> dict[str, Any]:
    input_pdf = options.input_pdf.resolve()
    output_dir = options.output_dir.resolve()
    options = dataclasses.replace(
        options,
        input_pdf=input_pdf,
        output_dir=output_dir,
    )

    if not input_pdf.is_file():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")
    if input_pdf.suffix.lower() != ".pdf":
        raise ValueError(f"Input does not have a .pdf extension: {input_pdf}")
    if options.render_dpi < 72:
        raise ValueError("--dpi must be at least 72")
    if options.ocr_workers < 1:
        raise ValueError("--ocr-workers must be at least 1")

    require_new_or_resumable_output(output_dir, options.resume)
    source_hash = sha256_file(input_pdf)

    with fitz.open(input_pdf) as doc:
        if doc.needs_pass:
            raise RuntimeError("The input PDF requires a password")
        if not doc.is_pdf:
            raise ValueError("The input is not a PDF document")

        print(
            f"[start] {doc.page_count} pages -> {output_dir}",
            flush=True,
        )
        attachments = extract_attachments(doc, output_dir)
        page_records, image_occurrences, referenced_pages = extract_pages(
            doc, options
        )
        image_records = extract_embedded_images(
            doc, output_dir, referenced_pages
        )
        ocr = run_ocr(doc.page_count, page_records, options)
        poppler = run_poppler_verification(options)
        metadata = document_metadata(
            doc, options, source_hash, attachments
        )
        write_json(output_dir / "metadata.json", metadata)

        validation = validate_outputs(
            doc,
            options,
            source_hash,
            page_records,
            image_occurrences,
            image_records,
            ocr,
            poppler,
        )
        write_json(output_dir / "validation.json", validation)

        manifest = {
            "extractor_version": EXTRACTOR_VERSION,
            "source": metadata["source"],
            "output": ".",
            "status": validation["status"],
            "counts": validation["counts"],
            "key_files": {
                "metadata": "metadata.json",
                "pages": "pages.json",
                "validation": "validation.json",
                "native_text": "text/full-native.txt",
                "reading_order_text": "text/full-reading-order.txt",
                "ocr_text": ocr.get("full_text_file"),
                "ocr_fallback_text": ocr.get(
                    "image_orientation_fallback", {}
                ).get("full_text_file"),
                "embedded_images": "images/embedded-images.json",
                "image_occurrences": "images/occurrences.json",
            },
        }
        write_json(output_dir / "manifest.json", manifest)
        write_text(output_dir / "README.txt", extraction_readme(options))
        checksum_inventory = write_checksum_inventory(output_dir)

    result = {
        "status": validation["status"],
        "output": str(output_dir),
        "counts": validation["counts"],
        "checksum_inventory": checksum_inventory,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if validation["status"] != "passed":
        raise RuntimeError(
            f"Extraction completed but validation failed. "
            f"See {output_dir / 'validation.json'}"
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract all native text, layout, embedded images, vectors, "
            "page renders, and optional OCR from a PDF."
        )
    )
    parser.add_argument("pdf", type=Path, help="Source PDF")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "Output directory (default: ./extracted/<source filename>)"
        ),
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Page render and OCR DPI (default: 300)",
    )
    parser.add_argument(
        "--ocr",
        choices=("none", "missing", "all"),
        default="all",
        help=(
            "OCR no pages, native-text-empty pages, or every page "
            "(default: all)"
        ),
    )
    parser.add_argument(
        "--ocr-language",
        default="eng",
        help="Tesseract language code(s), such as eng or eng+fra",
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=max(1, min(4, os.cpu_count() or 1)),
        help="Concurrent Tesseract processes (default: up to 4)",
    )
    parser.add_argument(
        "--ocr-psm",
        type=int,
        default=3,
        help="Tesseract page segmentation mode (default: 3)",
    )
    parser.add_argument(
        "--tesseract",
        help="Tesseract executable path or command name",
    )
    parser.add_argument(
        "--no-ocr-image-fallback",
        action="store_true",
        help=(
            "Disable multi-orientation OCR for image-bearing and "
            "native-text-empty pages"
        ),
    )
    parser.add_argument(
        "--no-svg",
        action="store_true",
        help="Do not create SVG snapshots of every page",
    )
    parser.add_argument(
        "--poppler",
        choices=("auto", "never", "required"),
        default="auto",
        help=(
            "Use Poppler as an independent verifier when available "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse existing page renders/OCR files in an output directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    input_pdf = args.pdf.resolve()
    output = (
        args.output.resolve()
        if args.output
        else (Path.cwd() / "extracted" / input_pdf.stem).resolve()
    )
    options = ExtractionOptions(
        input_pdf=input_pdf,
        output_dir=output,
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
    try:
        extract(options)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
