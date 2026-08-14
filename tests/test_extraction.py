"""PDF extractor regressions learned from structurally different documents."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from hexwiki.extraction import pdf


class ExtractionRegressionTests(unittest.TestCase):
    def test_poppler_count_is_compared_with_image_occurrences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_pdf = root / "source.pdf"
            source_pdf.write_bytes(b"synthetic-pdf-bytes")
            output = root / "extracted"
            files = {
                "native": output / "text/native-pages/page-0001.txt",
                "reading": output / "text/reading-order-pages/page-0001.txt",
                "layout": output / "layout/page-0001.json",
                "vectors": output / "vectors/page-0001.json",
                "render": output / "pages/page-0001.png",
            }
            for path in files.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")
            page_records = [
                {
                    "page": 1,
                    "native_text_file": "text/native-pages/page-0001.txt",
                    "reading_order_text_file": "text/reading-order-pages/page-0001.txt",
                    "layout_file": "layout/page-0001.json",
                    "vector_file": "vectors/page-0001.json",
                    "render_file": "pages/page-0001.png",
                    "native_text_characters": 1,
                    "raw_layout_characters": 1,
                    "vector_drawing_count": 0,
                }
            ]
            occurrences = [{"page": 1}, {"page": 1}]
            image_records = [
                {"xref": 7, "encoded_stream_file": "raw.bin", "native_file": "image.png"}
            ]
            poppler = {
                "available": True,
                "tools": {
                    "pdfimages": {
                        "listed_image_occurrences": 2,
                        "extracted_file_count": 2,
                    }
                },
            }
            options = pdf.ExtractionOptions(
                input_pdf=source_pdf,
                output_dir=output,
                ocr_mode="none",
                save_svg=False,
            )
            doc = SimpleNamespace(page_count=1)
            with patch.object(pdf, "collect_image_xrefs", return_value=[7]):
                report = pdf.validate_outputs(
                    doc,
                    options,
                    pdf.sha256_file(source_pdf),
                    page_records,
                    occurrences,
                    image_records,
                    {"target_pages": [], "pages": [], "image_orientation_fallback": {}},
                    poppler,
                )
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["checks"]["poppler_image_occurrences_match"])

    def test_non_device_colourspace_is_converted_for_png(self) -> None:
        document = object()
        base = SimpleNamespace(colorspace=SimpleNamespace(name="Separation(DeviceCMYK)"))
        converted = SimpleNamespace(colorspace=pdf.fitz.csRGB)
        with patch.object(pdf.fitz, "Pixmap", side_effect=[base, converted]) as pixmap:
            result = pdf.rendered_image_pixmap(document, 17, None)
        self.assertIs(result, converted)
        self.assertEqual(
            pixmap.call_args_list,
            [call(document, 17), call(pdf.fitz.csRGB, base)],
        )

    def test_one_unrenderable_image_is_recorded_not_raised(self) -> None:
        class UnrenderableImage(Exception):
            pass

        class Document:
            def xref_object(self, xref: int, compressed: bool = False) -> str:
                return f"object {xref} {compressed}"

            def xref_stream_raw(self, xref: int) -> bytes:
                return b"encoded"

            def extract_image(self, xref: int) -> dict:
                return {"image": b"native", "ext": "png"}

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            with (
                patch.object(pdf, "collect_image_xrefs", return_value=[17]),
                patch.object(pdf, "collect_smask_references", return_value={}),
                patch.object(
                    pdf,
                    "rendered_image_pixmap",
                    side_effect=UnrenderableImage("synthetic renderer refusal"),
                ),
            ):
                records = pdf.extract_embedded_images(Document(), output, {17: {1}})
            self.assertEqual(len(records), 1)
            self.assertIn("UnrenderableImage", records[0]["rendered_extraction_error"])
            self.assertTrue((output / records[0]["native_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
