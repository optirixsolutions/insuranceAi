"""OCR engines behind one interface.

Tesseract is the default engine (installed in the worker image). PaddleOCR is supported as an
optional engine when its packages are installed (``INSTALL_PADDLE=1`` image build); selecting
it without the packages fails loudly instead of silently falling back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from PIL import Image


@dataclass(frozen=True, slots=True)
class OcrWord:
    text: str
    box: tuple[float, float, float, float]  # normalised x0, y0, x1, y1 (0..1)
    confidence: float  # 0..1


@dataclass(slots=True)
class OcrResult:
    text: str
    words: list[OcrWord] = field(default_factory=list)
    confidence: float = 0.0
    rotation_applied: int = 0


class OcrUnavailableError(RuntimeError):
    pass


class OcrEngine(Protocol):
    name: str

    def recognise(self, image: Image.Image) -> OcrResult: ...


class TesseractEngine:
    name = "tesseract"

    def __init__(self, lang: str = "eng") -> None:
        try:
            import pytesseract
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise OcrUnavailableError("pytesseract is not installed") from exc
        self._tess: Any = pytesseract
        self.lang = lang
        try:
            self._tess.get_tesseract_version()
        except Exception as exc:
            raise OcrUnavailableError("The tesseract binary is not installed on this host") from exc

    def _orientation(self, image: Image.Image) -> int:
        """Rotation (0/90/180/270) Tesseract's orientation detection recommends; 0 if unsure."""
        try:
            osd = self._tess.image_to_osd(image, output_type=self._tess.Output.DICT)
        except Exception:
            return 0  # too little text for orientation detection
        rotate = int(osd.get("rotate", 0))
        return rotate if float(osd.get("orientation_conf", 0)) >= 2.0 else 0

    def recognise(self, image: Image.Image) -> OcrResult:
        rotation = self._orientation(image)
        if rotation:
            image = image.rotate(-rotation, expand=True)
        data = self._tess.image_to_data(
            image, lang=self.lang, config="--psm 3", output_type=self._tess.Output.DICT
        )
        width, height = image.size
        words: list[OcrWord] = []
        lines: dict[tuple[int, int, int], list[str]] = {}
        for i, raw in enumerate(data["text"]):
            token = str(raw).strip()
            conf = float(data["conf"][i])
            if not token or conf < 0:
                continue
            x, y, w, h = (int(data[k][i]) for k in ("left", "top", "width", "height"))
            words.append(
                OcrWord(token, (x / width, y / height, (x + w) / width, (y + h) / height), conf / 100)
            )
            key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
            lines.setdefault(key, []).append(token)
        text = "\n".join(" ".join(tokens) for _, tokens in sorted(lines.items()))
        confidence = sum(w.confidence for w in words) / len(words) if words else 0.0
        return OcrResult(text, words, round(confidence, 3), rotation)


class PaddleEngine:
    name = "paddleocr"

    def __init__(self) -> None:
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-not-found]
        except ImportError as exc:
            raise OcrUnavailableError(
                "PaddleOCR is not installed; build the worker with INSTALL_PADDLE=1"
            ) from exc
        self._ocr: Any = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)

    def recognise(self, image: Image.Image) -> OcrResult:  # pragma: no cover - needs optional packages
        import numpy as np

        result = self._ocr.ocr(np.asarray(image.convert("RGB")), cls=True) or [[]]
        width, height = image.size
        words: list[OcrWord] = []
        for box, (token, conf) in result[0] or []:
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            words.append(
                OcrWord(
                    token, (min(xs) / width, min(ys) / height, max(xs) / width, max(ys) / height), float(conf)
                )
            )
        confidence = sum(w.confidence for w in words) / len(words) if words else 0.0
        return OcrResult("\n".join(w.text for w in words), words, round(confidence, 3))


def build_engine(name: str) -> OcrEngine:
    if name == "paddleocr":
        return PaddleEngine()
    return TesseractEngine()
