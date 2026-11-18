"""Synthetic document renderers shared by the evaluation sets and the test-suite.

PDFs are written by hand so the text layer is genuine; scans are rendered images of text,
optionally degraded (blur, noise, low contrast, rotation). All content is synthetic. These ship
in the backend image (unlike ``tests/``) so the evaluation runner works inside the container.
"""

from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def text_pdf(lines: list[str], *, extra_catalog: str = "") -> bytes:
    """A one-page PDF whose text is a real, selectable text layer (Helvetica, 11pt)."""
    stream_lines = ["BT", "/F1 11 Tf", "14 TL", "56 780 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_lines.append(f"({escaped}) Tj T*")
    stream_lines.append("ET")
    content = "\n".join(stream_lines).encode("latin-1")
    objects = [
        f"<< /Type /Catalog /Pages 2 0 R {extra_catalog}>>".encode(),
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def scan_image(
    lines: list[str], *, fmt: str = "PNG", degrade: str | None = None, rotate: int = 0, seed: int = 1
) -> bytes:
    """A 'scanned' page: dark text on a white A4-ratio canvas at roughly 150 dpi."""
    font = ImageFont.load_default(size=30)
    image = Image.new("L", (1240, 1754), 255)
    draw = ImageDraw.Draw(image)
    y = 110
    for line in lines:
        draw.text((100, y), line, fill=20, font=font)
        y += 52
    if degrade == "blur":
        image = image.filter(ImageFilter.GaussianBlur(1.2))
    elif degrade == "noise":
        rng = random.Random(seed)
        pixels = image.load()
        assert pixels is not None
        for _ in range(60000):
            x, yy = rng.randrange(image.width), rng.randrange(image.height)
            pixels[x, yy] = rng.choice((0, 255, 140))
    elif degrade == "faded":
        image = image.point(lambda p: 150 + p * 105 // 255)
    elif degrade == "small":
        image = image.resize((image.width // 2, image.height // 2), Image.Resampling.BILINEAR)
    if rotate:
        image = image.rotate(rotate, expand=True, fillcolor=255)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format=fmt, **({"quality": 70} if fmt == "JPEG" else {}))
    return buffer.getvalue()


def photo_image(seed: int = 3) -> bytes:
    """A text-free 'photo' (coloured shapes) such as damage evidence."""
    rng = random.Random(seed)
    image = Image.new("RGB", (1024, 768), (90, 110, 130))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = rng.randrange(1024), rng.randrange(768)
        draw.ellipse(
            (x, y, x + rng.randrange(20, 200), y + rng.randrange(20, 160)),
            fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)),
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()
