"""Image quality signals used to score pages and route poor scans to human review."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter, ImageOps

SHARPNESS_LOW = 60.0  # variance of the Laplacian below this reads as blurred
CONTRAST_LOW = 110.0  # ink-to-paper spread (grey levels) below this reads as faded or washed out
MIN_SHORT_SIDE = 900  # pixels; smaller scans are upscaled before OCR


@dataclass(frozen=True, slots=True)
class ImageQuality:
    sharpness: float
    contrast: float
    flags: tuple[str, ...]


def measure(image: Image.Image) -> ImageQuality:
    grey = np.asarray(ImageOps.grayscale(image), dtype=np.float32)
    lap = -4 * grey[1:-1, 1:-1] + grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:]
    sharpness = float(lap.var()) if lap.size else 0.0
    # Spread between the darkest and lightest pixels. A plain standard deviation would call any
    # mostly-white page "low contrast", however crisp the text is.
    dark, light = np.percentile(grey, [0.5, 99.5]) if grey.size else (0.0, 0.0)
    contrast = float(light - dark)
    flags: list[str] = []
    if sharpness < SHARPNESS_LOW:
        flags.append("blurred")
    if contrast < CONTRAST_LOW:
        flags.append("low_contrast")
    if min(image.size) < MIN_SHORT_SIDE:
        flags.append("low_resolution")
    return ImageQuality(sharpness, contrast, tuple(flags))


def prepare_for_ocr(image: Image.Image) -> Image.Image:
    """Greyscale, upscale small scans, stretch contrast and remove speckle noise."""
    img = ImageOps.exif_transpose(image) or image
    img = ImageOps.grayscale(img)
    short = min(img.size)
    if short < MIN_SHORT_SIDE:
        factor = MIN_SHORT_SIDE / max(short, 1)
        img = img.resize((int(img.width * factor), int(img.height * factor)), Image.Resampling.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    return img.filter(ImageFilter.MedianFilter(3))


def page_score(*, text_chars: int, ocr_confidence: float | None, quality: ImageQuality | None) -> float:
    """0..1 score combining how much text was read, how sure OCR was, and image quality."""
    density = min(text_chars / 400.0, 1.0)
    confidence = 1.0 if ocr_confidence is None else ocr_confidence
    penalty = 0.0
    if quality is not None:
        penalty += 0.15 * len(quality.flags)
    return round(max(0.0, min(1.0, 0.35 * density + 0.65 * confidence - penalty)), 3)
