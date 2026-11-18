"""Turn an accepted file into pages of text with word positions, quality scores and preview images."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from docx import Document as DocxDocument
from PIL import Image, ImageOps, ImageSequence

from document_processing import detect
from document_processing.detect import RejectedFileError
from document_processing.ocr import OcrEngine, OcrResult, OcrWord
from document_processing.quality import ImageQuality, measure, page_score, prepare_for_ocr

TEXT_LAYER_MIN_CHARS = 25  # fewer characters than this on a PDF page means it is a scan
RENDER_DPI = 200
PREVIEW_MAX_WIDTH = 1400


@dataclass(slots=True)
class PageContent:
    page_number: int
    text: str
    text_source: str  # text_layer | tesseract | paddleocr | docx | none
    words: list[OcrWord] = field(default_factory=list)
    ocr_confidence: float | None = None
    quality: ImageQuality | None = None
    score: float = 0.0
    preview_png: bytes | None = None
    width: int | None = None
    height: int | None = None
    rotation_applied: int = 0
    flags: list[str] = field(default_factory=list)
    image_for_hash: Image.Image | None = None


def _preview(image: Image.Image) -> bytes:
    img = image.convert("RGB")
    if img.width > PREVIEW_MAX_WIDTH:
        img = img.resize(
            (PREVIEW_MAX_WIDTH, int(img.height * PREVIEW_MAX_WIDTH / img.width)), Image.Resampling.LANCZOS
        )
    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _ocr_page(number: int, image: Image.Image, ocr: OcrEngine | None) -> PageContent:
    quality = measure(image)
    if ocr is None:
        return PageContent(
            number,
            "",
            "none",
            quality=quality,
            score=0.0,
            preview_png=_preview(image),
            width=image.width,
            height=image.height,
            flags=[*quality.flags, "ocr_unavailable"],
            image_for_hash=image,
        )
    result: OcrResult = ocr.recognise(prepare_for_ocr(image))
    shown = image.rotate(-result.rotation_applied, expand=True) if result.rotation_applied else image
    flags = list(quality.flags)
    if result.rotation_applied:
        flags.append(f"rotated_{result.rotation_applied}")
    if len(result.text.strip()) < 10:
        flags.append("little_or_no_text")
    score = page_score(text_chars=len(result.text), ocr_confidence=result.confidence, quality=quality)
    return PageContent(
        number,
        result.text,
        ocr.name,
        result.words,
        result.confidence,
        quality,
        score,
        _preview(shown),
        shown.width,
        shown.height,
        result.rotation_applied,
        flags,
        shown,
    )


def _pdf_words(textpage: pdfium.PdfTextPage, width: float, height: float) -> list[OcrWord]:
    words: list[OcrWord] = []
    current: list[tuple[str, tuple[float, float, float, float]]] = []

    def flush() -> None:
        if current:
            token = "".join(ch for ch, _ in current)
            x0 = min(b[0] for _, b in current)
            x1 = max(b[2] for _, b in current)
            top = max(b[3] for _, b in current)
            bottom = min(b[1] for _, b in current)
            words.append(OcrWord(token, (x0 / width, 1 - top / height, x1 / width, 1 - bottom / height), 1.0))
            current.clear()

    for index in range(textpage.count_chars()):
        char = textpage.get_text_range(index, 1)
        if not char or char.isspace():
            flush()
            continue
        current.append((char, textpage.get_charbox(index)))
    flush()
    return words


def _pdf_pages(data: bytes, ocr: OcrEngine | None, max_pages: int) -> list[PageContent]:
    pdf = pdfium.PdfDocument(data)
    try:
        if len(pdf) > max_pages:
            raise RejectedFileError(
                "too_many_pages", f"The PDF has {len(pdf)} pages; the limit is {max_pages}"
            )
        pages: list[PageContent] = []
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            image = page.render(scale=RENDER_DPI / 72).to_pil()
            textpage = page.get_textpage()
            text = textpage.get_text_range().replace("\r\n", "\n").strip()
            if len(text) >= TEXT_LAYER_MIN_CHARS:
                words = _pdf_words(textpage, width, height)
                score = page_score(text_chars=len(text), ocr_confidence=None, quality=None)
                pages.append(
                    PageContent(
                        index + 1,
                        text,
                        "text_layer",
                        words,
                        None,
                        None,
                        score,
                        _preview(image),
                        image.width,
                        image.height,
                        image_for_hash=image,
                    )
                )
            else:
                pages.append(_ocr_page(index + 1, image, ocr))
            textpage.close()
            page.close()
        return pages
    finally:
        pdf.close()


def _image_pages(data: bytes, ocr: OcrEngine | None, max_pages: int) -> list[PageContent]:
    pages: list[PageContent] = []
    with Image.open(io.BytesIO(data)) as source:
        for index, frame in enumerate(ImageSequence.Iterator(source)):
            if index >= max_pages:
                raise RejectedFileError("too_many_pages", f"The image has more than {max_pages} pages")
            image = ImageOps.exif_transpose(frame.copy()) or frame.copy()
            pages.append(_ocr_page(index + 1, image.convert("RGB"), ocr))
    return pages


def _docx_pages(data: bytes) -> list[PageContent]:
    doc = DocxDocument(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append(" | ".join(cells))
    text = "\n".join(lines)
    score = page_score(text_chars=len(text), ocr_confidence=None, quality=None)
    return [PageContent(1, text, "docx", score=score)]


def extract_pages(data: bytes, mime: str, ocr: OcrEngine | None, *, max_pages: int) -> list[PageContent]:
    if mime == detect.PDF:
        return _pdf_pages(data, ocr, max_pages)
    if mime in detect.IMAGE_TYPES:
        return _image_pages(data, ocr, max_pages)
    if mime == detect.DOCX:
        return _docx_pages(data)
    raise RejectedFileError("unsupported_type", f"No extractor for {mime}")
