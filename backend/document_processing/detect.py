"""File type detection from content (never trusting the declared type) and safety checks.

Uploaded files are untrusted. A file is accepted only when its bytes match an allowed
format, the declared type agrees with the detected one, and format-specific checks find
no active or hidden content.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass

from PIL import Image

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

ALLOWED_TYPES = frozenset({PDF, PNG, JPEG, TIFF, DOCX})
IMAGE_TYPES = frozenset({PNG, JPEG, TIFF})
# Declared types browsers commonly send for the same formats.
DECLARED_ALIASES = {"image/jpg": JPEG, "image/pjpeg": JPEG, "image/x-png": PNG, "application/x-pdf": PDF}

MAX_IMAGE_PIXELS = 40_000_000
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
_PDF_ACTIVE = re.compile(
    rb"/(JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA|OpenAction\s*<<[^>]*?/S\s*/JavaScript)\b"
)
_PDF_ENCRYPT = re.compile(rb"/Encrypt\b")


class RejectedFileError(ValueError):
    """The file must not be stored or processed. ``code`` is stable for API clients and tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Detection:
    mime: str
    extension: str


def sniff(data: bytes) -> Detection | None:
    head = data[:16]
    if head.startswith(b"%PDF-"):
        return Detection(PDF, "pdf")
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return Detection(PNG, "png")
    if head.startswith(b"\xff\xd8\xff"):
        return Detection(JPEG, "jpg")
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return Detection(TIFF, "tiff")
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
        except zipfile.BadZipFile:
            return None
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return Detection(DOCX, "docx")
    return None


def _check_pdf(data: bytes) -> None:
    if _PDF_ENCRYPT.search(data):
        raise RejectedFileError("pdf_encrypted", "Encrypted or password-protected PDFs cannot be processed")
    match = _PDF_ACTIVE.search(data)
    if match:
        raise RejectedFileError(
            "pdf_active_content",
            f"PDF contains active or embedded content ({match.group(1).decode(errors='replace')})",
        )


def _check_image(data: bytes) -> None:
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if width * height > MAX_IMAGE_PIXELS:
                raise RejectedFileError(
                    "image_too_large", f"Image is {width}x{height}; the limit is 40 megapixels"
                )
            img.verify()
    except RejectedFileError:
        raise
    except Exception as exc:  # Pillow raises many types for corrupt input
        raise RejectedFileError("image_corrupt", "The image could not be read") from exc


def _check_docx(data: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
            raise RejectedFileError("docx_too_large", "The Word document expands beyond the 50 MB limit")
        if any(i.filename.lower().endswith("vbaproject.bin") for i in infos):
            raise RejectedFileError("docx_macros", "Word documents with macros are not accepted")
        for info in infos:
            if info.filename.lower().endswith(".rels") and b'TargetMode="External"' in zf.read(info):
                raise RejectedFileError(
                    "docx_external_links", "Word documents with external links are not accepted"
                )


def inspect(data: bytes, declared_mime: str) -> Detection:
    """Detect the real type, compare with the declared type and run the safety checks."""
    if not data:
        raise RejectedFileError("empty", "The file is empty")
    detected = sniff(data)
    if detected is None:
        raise RejectedFileError("unsupported_type", "Only PDF, PNG, JPEG, TIFF and DOCX files are accepted")
    declared = DECLARED_ALIASES.get(declared_mime.lower(), declared_mime.lower())
    if declared not in ("application/octet-stream", "") and declared != detected.mime:
        raise RejectedFileError(
            "type_mismatch", f"The file says it is {declared_mime} but its content is {detected.mime}"
        )
    if detected.mime == PDF:
        _check_pdf(data)
    elif detected.mime in IMAGE_TYPES:
        _check_image(data)
    elif detected.mime == DOCX:
        _check_docx(data)
    return detected


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_filename(name: str) -> str:
    """Keep a readable original name for display only; storage keys never use it."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip() or "document"
    cleaned = _UNSAFE_NAME.sub("_", base).strip(" .")[:200]
    return cleaned or "document"
