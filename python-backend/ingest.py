"""
PDF ingestion: PDF -> per-page text -> page-bounded chunks -> embeddings -> Storage.

Rules this module guarantees (tested in tests/test_ingest.py):
  * PDF parsing uses pypdf (BSD-3). PyMuPDF is AGPL and must not be used.
  * Chunks never cross a page boundary, so every citation is exactly one page.
  * For every chunk: page.text[char_start:char_end] == chunk.text, where
    page.text is the exact pypdf output stored in the `pages` table.
  * A page with no extractable text is RECORDED (status no_text_has_images or
    blank), never silently dropped. Documents whose pages need OCR get status
    `partial` or `needs_ocr`, and the IngestReport says so in plain language.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Protocol, Sequence

from pypdf import PdfReader
from pypdf.errors import DependencyError, FileNotDecryptedError, PdfReadError
from pypdf.generic import IndirectObject

from storage import ChunkRecord, PageRecord, Storage

log = logging.getLogger(__name__)

# A page with fewer non-whitespace characters than this is treated as having
# no real text. This catches scans that only carry a Bates stamp or page number
# as a text overlay (e.g. "ABC000123"). Known false positive: a cover page with
# a logo and very little text is flagged as needing OCR, which is harmless.
MIN_TEXT_CHARS = 100

CHUNK_SIZE = 1000      # characters
CHUNK_OVERLAP = 150    # characters

# Page statuses
PAGE_OK = "ok"
PAGE_NEEDS_OCR = "no_text_has_images"   # scanned page: text is in an image
PAGE_BLANK = "blank"                    # no text and no images
PAGE_ERROR = "error"

# Document statuses
DOC_OK = "ok"
DOC_PARTIAL = "partial"                 # some pages indexed, some need OCR
DOC_NEEDS_OCR = "needs_ocr"             # nothing indexed: all text is in images
DOC_EMPTY = "empty"                     # no text and no images at all
DOC_PASSWORD = "password_protected"
DOC_ERROR = "error"


class Embedder(Protocol):
    dim: int
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass
class IngestReport:
    source_path: str
    status: str
    page_count: int = 0
    chunk_count: int = 0
    pages_ok: list[int] = field(default_factory=list)
    pages_needing_ocr: list[int] = field(default_factory=list)
    pages_blank: list[int] = field(default_factory=list)
    pages_error: list[int] = field(default_factory=list)
    document_id: int | None = None
    message: str = ""

    @property
    def fully_indexed(self) -> bool:
        return self.status == DOC_OK


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _has_image(resources, depth: int = 0) -> bool:
    """True if the page (or a form XObject it uses) draws a raster image."""
    if resources is None or depth > 3:
        return False
    if isinstance(resources, IndirectObject):
        resources = resources.get_object()
    xobjects = resources.get("/XObject")
    if xobjects is None:
        return False
    for ref in xobjects.get_object().values():
        xo = ref.get_object()
        subtype = xo.get("/Subtype")
        if subtype == "/Image":
            return True
        if subtype == "/Form" and _has_image(xo.get("/Resources"), depth + 1):
            return True
    return False


def extract_pages(reader: PdfReader) -> list[PageRecord]:
    pages: list[PageRecord] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # malformed content stream on one page
            log.warning("page %d: text extraction failed: %s", i, e)
            pages.append(PageRecord(i, "", PAGE_ERROR, "pypdf"))
            continue

        if len("".join(text.split())) >= MIN_TEXT_CHARS:
            status = PAGE_OK
        elif _has_image(page.get("/Resources")):
            status = PAGE_NEEDS_OCR
            # ----------------------------------------------------------------
            # OCR HOOK (not built yet). This is where Tesseract slots in:
            #   1. Render the page to a bitmap. pypdf cannot render; use
            #      pypdfium2 (Apache-2.0/BSD), NOT PyMuPDF (AGPL).
            #   2. Run Tesseract and keep its word-level output.
            #   3. Replace `text` with the OCR text, set status = PAGE_OK and
            #      extraction_method = "tesseract".
            #   4. Persist word bounding boxes too: an OCR'd page has no PDF
            #      text layer, so viewer highlighting must use boxes, not
            #      char offsets.
            # Any small existing text (e.g. a Bates stamp) is kept below so it
            # stays searchable until OCR exists.
            # ----------------------------------------------------------------
        else:
            status = PAGE_BLANK
        pages.append(PageRecord(i, text, status, "pypdf"))
    return pages


# ---------------------------------------------------------------------------
# Chunking (page-bounded, exact offsets)
# ---------------------------------------------------------------------------

def chunk_page(page: PageRecord, size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[ChunkRecord]:
    text = page.text
    n = len(text)
    chunks: list[ChunkRecord] = []
    start = 0
    while start < n:
        while start < n and text[start].isspace():   # no leading whitespace
            start += 1
        if start >= n:
            break
        end = min(start + size, n)
        if end < n:
            # Prefer breaking at a paragraph, then line, then sentence, then word.
            floor = start + size // 2
            for sep in ("\n\n", "\n", ". ", " "):
                cut = text.rfind(sep, floor, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        stop = end
        while stop > start and text[stop - 1].isspace():  # no trailing whitespace
            stop -= 1
        if stop > start:
            chunks.append(ChunkRecord(page.page_number, start, stop, text[start:stop]))
        if end >= n:
            break
        # Step back by `overlap`, then forward to the next word start.
        nxt = max(end - overlap, start + 1)
        while nxt < end and not text[nxt - 1].isspace():
            nxt += 1
        start = nxt
    return chunks


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _summarize(r: IngestReport) -> str:
    def pages(ps: list[int]) -> str:
        return ", ".join(map(str, ps[:20])) + (" ..." if len(ps) > 20 else "")
    if r.status == DOC_OK:
        return f"Indexed {r.page_count} pages ({r.chunk_count} passages)."
    if r.status == DOC_PARTIAL:
        return (f"Partially indexed: {len(r.pages_ok)} of {r.page_count} pages. "
                f"Pages {pages(r.pages_needing_ocr)} are scanned images and are NOT "
                f"searchable until OCR is added.")
    if r.status == DOC_NEEDS_OCR:
        return (f"Not indexed: all {r.page_count} pages are scanned images with no text "
                f"layer. This document is NOT searchable until OCR is added.")
    if r.status == DOC_EMPTY:
        return f"Not indexed: the document's {r.page_count} pages contain no text or images."
    if r.status == DOC_PASSWORD:
        return "Not indexed: the PDF is password-protected."
    return r.message or "Not indexed: the PDF could not be read."


def ingest_pdf(path: str, storage: Storage, embedder: Embedder) -> IngestReport:
    path = os.path.abspath(path)
    report = IngestReport(source_path=path, status=DOC_ERROR)

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # Owner-password-only PDFs (common for court filings) open with "".
            if not reader.decrypt(""):
                report.status = DOC_PASSWORD
                report.message = _summarize(report)
                log.warning("%s: %s", path, report.message)
                return report
        pages = extract_pages(reader)
    except FileNotDecryptedError:
        report.status = DOC_PASSWORD
        report.message = _summarize(report)
        return report
    except DependencyError as e:
        # Packaging bug (e.g. `cryptography` missing for AES PDFs), not a user problem.
        report.message = f"Not indexed: missing PDF decryption support ({e})."
        log.error("%s: %s", path, report.message)
        return report
    except Exception as e:  # malformed PDFs raise a wide range of exceptions
        report.message = f"Not indexed: the PDF could not be read ({type(e).__name__}: {e})."
        log.warning("%s: %s", path, report.message)
        return report

    report.page_count = len(pages)
    for p in pages:
        {PAGE_OK: report.pages_ok, PAGE_NEEDS_OCR: report.pages_needing_ocr,
         PAGE_BLANK: report.pages_blank, PAGE_ERROR: report.pages_error}[p.status].append(p.page_number)

    if report.pages_ok:
        report.status = DOC_PARTIAL if (report.pages_needing_ocr or report.pages_error) else DOC_OK
    elif report.pages_needing_ocr:
        report.status = DOC_NEEDS_OCR
    elif report.pages_error:
        report.status = DOC_ERROR
    else:
        report.status = DOC_EMPTY

    chunks = [c for p in pages for c in chunk_page(p)]
    embeddings = embedder.embed([c.text for c in chunks]) if chunks else []

    # The document is recorded even when nothing is searchable, so the UI can
    # list it with a "needs OCR" badge instead of it silently disappearing.
    report.document_id = storage.ingest_document(
        path, _sha256(path), report.status, pages, chunks, embeddings)
    report.chunk_count = len(chunks)
    report.message = _summarize(report)

    if report.status != DOC_OK:
        log.warning("%s: %s", path, report.message)
    return report
