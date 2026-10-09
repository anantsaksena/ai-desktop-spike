"""
Citation -> highlight mapping.

Chunk offsets are exact against OUR stored page text (pypdf output). A PDF
viewer (pdf.js in the UI) builds its own text layer, which contains the same
characters but different whitespace and line breaks. `locate()` maps a chunk
onto any other extraction of the same page by matching on whitespace-
normalized text and mapping back to offsets in that other text.

If it returns None (e.g. reading order differs in multi-column layouts or
tables), the UI must fall back to a page-level citation: open the page,
no highlight. It must never highlight the wrong passage.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

# Ligatures and typographic variants that differ between extractors.
_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u00ad": "",   # soft hyphen
    "\u00a0": " ",
})


def _normalize_with_map(s: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs to one space and fold variants.
    Returns (normalized, idx) where idx[k] is the offset in `s` of normalized char k."""
    out: list[str] = []
    idx: list[int] = []
    prev_space = True  # also strips leading whitespace
    for i, ch in enumerate(s):
        for c in unicodedata.normalize("NFKC", ch.translate(_FOLD)):  # NFKC expands "ﬁ" -> "fi"
            if c.isspace():
                if not prev_space:
                    out.append(" "); idx.append(i)
                prev_space = True
            else:
                out.append(c); idx.append(i)
                prev_space = False
    if out and out[-1] == " ":
        out.pop(); idx.pop()
    return "".join(out), idx


def normalize(s: str) -> str:
    return _normalize_with_map(s)[0]


def locate(chunk_text: str, other_page_text: str) -> Optional[tuple[int, int]]:
    """Find chunk_text inside other_page_text, ignoring whitespace differences.
    Returns (start, end) offsets into other_page_text, or None if not found
    or ambiguous (found more than once)."""
    needle = normalize(chunk_text)
    hay, idx = _normalize_with_map(other_page_text)
    if not needle:
        return None
    pos = hay.find(needle)
    if pos == -1 or hay.find(needle, pos + 1) != -1:
        return None
    return idx[pos], idx[pos + len(needle) - 1] + 1
