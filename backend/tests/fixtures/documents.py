"""Generate real document files for tests (no binary fixtures checked in, no mocks).

All content is synthetic. PDFs are written by hand so the text layer is genuine; scans are
rendered images of text, optionally degraded (blur, noise, low contrast, rotation).
"""

from __future__ import annotations

import io
import zipfile

from docx import Document
from PIL import Image

from evaluations.renderers import photo_image, scan_image, text_pdf

__all__ = ["docx_file", "docx_with_macro", "huge_png", "photo_image", "scan_image", "text_pdf"]


def docx_file(lines: list[str]) -> bytes:
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def docx_with_macro(lines: list[str]) -> bytes:
    source = io.BytesIO(docx_file(lines))
    out = io.BytesIO()
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item))
        dst.writestr("word/vbaProject.bin", b"\xd0\xcf\x11\xe0 fake macro container")
    return out.getvalue()


def huge_png() -> bytes:
    """Tiny file that decodes to 9000x5000 pixels (a decompression-bomb shape)."""
    buffer = io.BytesIO()
    Image.new("L", (9000, 5000), 255).save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


REPAIR_ESTIMATE = [
    "BRIDGEWATER AUTO BODY - REPAIR ESTIMATE",
    "Estimate No: EST-55210",
    "Invoice Date: 12/10/2026",
    "Policyholder: Rohan Tanaka",
    "Policy Number: NWM-M-24001",
    "Registration: LK21 ABC",
    "Date of loss: 08 Oct 2026",
    "Rear bumper replacement - parts 640.00",
    "Labour 6.5 hours at 70.00 - 455.00",
    "Paint and panel work - 380.00",
    "Total due: GBP 1,475.00",
]

HOSPITAL_BILL = [
    "ST. ANNE'S HOSPITAL - PATIENT INVOICE",
    "Invoice No: INV-778812",
    "Patient: Amelia Hartley",
    "Policy No: NWM-H-24000",
    "Admission date: 2026-09-14",
    "Discharge date: 2026-09-17",
    "Ward charges 3 nights - 2,250.00",
    "Procedure: laparoscopic appendectomy - 4,100.00",
    "Pharmacy - 310.50",
    "Amount due: $6,660.50",
]
