"""Text comparison used by deterministic checks (names, excerpts). No third-party dependency.

``jaro_winkler`` follows the standard definition (prefix scale 0.1, max prefix 4).
``token_set_ratio`` is the fuzzy "same words, OCR noise tolerated" measure used for grounding.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "mx"}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def name_tokens(name: str) -> list[str]:
    return [t for t in normalise(name).split() if t not in _HONORIFICS]


def jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0 if a else 0.0
    if not a or not b:
        return 0.0
    window = max(0, max(len(a), len(b)) // 2 - 1)
    a_flags = [False] * len(a)
    b_flags = [False] * len(b)
    matches = 0
    for i, ch in enumerate(a):
        lo, hi = max(0, i - window), min(len(b), i + window + 1)
        for j in range(lo, hi):
            if not b_flags[j] and b[j] == ch:
                a_flags[i] = b_flags[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    a_m = [c for c, f in zip(a, a_flags, strict=True) if f]
    b_m = [c for c, f in zip(b, b_flags, strict=True) if f]
    transpositions = sum(x != y for x, y in zip(a_m, b_m, strict=True)) / 2
    return (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3


def jaro_winkler(a: str, b: str) -> float:
    j = jaro(a, b)
    prefix = 0
    for x, y in zip(a[:4], b[:4], strict=False):
        if x != y:
            break
        prefix += 1
    return j + prefix * 0.1 * (1 - j)


def name_similarity(a: str, b: str) -> float:
    """Order-insensitive name similarity: "Dr Ayesha Khan" vs "KHAN, Ayesha" -> 1.0."""
    ta, tb = sorted(name_tokens(a)), sorted(name_tokens(b))
    if not ta or not tb:
        return 0.0
    whole = jaro_winkler(" ".join(ta), " ".join(tb))
    if len(ta) != len(tb):
        return whole
    # Token-wise (length-weighted): greedy whole-string Jaro mis-pairs repeated letters across
    # tokens ("Rohen Tanaka" vs "Rohan Tanaka" scores 0.88 whole, 0.96 token-wise).
    weight = sum(max(len(x), len(y)) for x, y in zip(ta, tb, strict=True))
    tokenwise = sum(jaro_winkler(x, y) * max(len(x), len(y)) for x, y in zip(ta, tb, strict=True)) / weight
    return max(whole, tokenwise)


def names_match(a: str, b: str, threshold: float = 0.92) -> bool:
    """Jaro-Winkler >= 0.92 on sorted tokens, plus a token rule: every token of the shorter
    name must appear (or be an initial) in the longer one, so "A Khan" matches "Ayesha Khan"."""
    if name_similarity(a, b) >= threshold:
        return True
    ta, tb = name_tokens(a), name_tokens(b)
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) < 2:
        return False
    return all(any(s == t or (len(s) == 1 and t.startswith(s)) for t in long_) for s in short)


def token_set_ratio(a: str, b: str) -> float:
    """Similarity of two strings as word sets, tolerant of order and of OCR character noise."""
    sa, sb = set(normalise(a).split()), set(normalise(b).split())
    if not sa or not sb:
        return 0.0
    inter = " ".join(sorted(sa & sb))
    left = f"{inter} {' '.join(sorted(sa - sb))}".strip()
    right = f"{inter} {' '.join(sorted(sb - sa))}".strip()

    def ratio(x: str, y: str) -> float:
        return SequenceMatcher(None, x, y).ratio()

    scores = [ratio(left, right)]
    if inter:
        scores += [ratio(inter, left), ratio(inter, right)]
    return max(scores)


def excerpt_in_text(excerpt: str, text: str, threshold: float = 0.90) -> float:
    """Grounding score of ``excerpt`` against page ``text``: 1.0 for an exact (normalised)
    occurrence, else the best token-set ratio over similar-length windows; 0.0 below threshold."""
    ex, tx = normalise(excerpt), normalise(text)
    if not ex:
        return 0.0
    if ex in tx:
        return 1.0
    n = len(ex.split())
    tx_words = tx.split()
    best = 0.0
    for size in {max(1, n - 1), n, n + 1}:
        for i in range(0, max(1, len(tx_words) - size + 1)):
            best = max(best, token_set_ratio(ex, " ".join(tx_words[i : i + size])))
    return best if best >= threshold else 0.0
