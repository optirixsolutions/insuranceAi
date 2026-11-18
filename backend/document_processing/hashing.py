"""Content fingerprints for duplicate detection.

* SHA-256: exact duplicates.
* dHash (64-bit difference hash): visually similar images and scans, robust to re-encoding
  and resizing. Hamming distance <= 6 is treated as a likely duplicate.
* SimHash (64-bit) over word shingles: near-identical text with small OCR differences.
"""

from __future__ import annotations

import hashlib
import re

from PIL import Image, ImageOps

PHASH_DUPLICATE_DISTANCE = 6
SIMHASH_DUPLICATE_DISTANCE = 3
_WORD = re.compile(r"[a-z0-9]+")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dhash(image: Image.Image, size: int = 8) -> str:
    grey = ImageOps.grayscale(image).resize((size + 1, size), Image.Resampling.LANCZOS)
    pixels = list(grey.getdata())
    value = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            value = (value << 1) | (1 if left > right else 0)
    return f"{value:016x}"


def simhash(text: str) -> str | None:
    words = _WORD.findall(text.lower())
    if len(words) < 8:
        return None  # too little text to fingerprint meaningfully
    shingles = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    weights = [0] * 64
    for shingle in shingles:
        h = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if h >> bit & 1 else -1
    value = sum(1 << bit for bit in range(64) if weights[bit] > 0)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()
