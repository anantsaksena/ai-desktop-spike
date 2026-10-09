import os

import pytest
from pypdf import PdfReader, PdfWriter

import ingest
from citations import locate, normalize
from conftest import (draw_scanned_pdf, draw_text_pdf, ground_truth_pages,
                      make_sentence, text_image)
from storage import ChunkRecord, PageRecord


# ---------------------------------------------------------------------------
# Offsets / highlighting
# ---------------------------------------------------------------------------

@pytest.fixture
def text_pdf(tmp_path):
    path = str(tmp_path / "contract.pdf")
    drawn = draw_text_pdf(path, ground_truth_pages())
    return path, drawn


def _all_chunks(storage, doc_id):
    return storage.conn.execute(
        "SELECT page_number, char_start, char_end, text FROM chunks "
        "WHERE document_id = ? ORDER BY page_number, char_start", (doc_id,)).fetchall()


def test_offsets_are_exact_against_stored_page_text(storage, embedder, text_pdf):
    path, _ = text_pdf
    r = ingest.ingest_pdf(path, storage, embedder)
    assert r.status == ingest.DOC_OK, r.message
    assert r.page_count == 3
    chunks = _all_chunks(storage, r.document_id)
    assert len(chunks) >= 6, "expected several chunks per page"
    for page, s, e, text in chunks:
        assert storage.get_page_text(r.document_id, page)[s:e] == text


def test_chunks_cover_all_page_text(storage, embedder, text_pdf):
    """No characters are lost between chunks."""
    path, _ = text_pdf
    r = ingest.ingest_pdf(path, storage, embedder)
    chunks = _all_chunks(storage, r.document_id)
    for page in range(1, r.page_count + 1):
        page_text = storage.get_page_text(r.document_id, page)
        covered = [False] * len(page_text)
        for p, s, e, _ in chunks:
            if p == page:
                covered[s:e] = [True] * (e - s)
        missed = [i for i, ch in enumerate(page_text) if not ch.isspace() and not covered[i]]
        assert missed == [], f"page {page}: {len(missed)} chars not in any chunk"


def test_pypdf_text_matches_what_was_drawn(storage, embedder, text_pdf):
    """Extraction fidelity: stored page text == drawn text, modulo whitespace."""
    path, drawn = text_pdf
    r = ingest.ingest_pdf(path, storage, embedder)
    for page, expected in enumerate(drawn, start=1):
        assert normalize(storage.get_page_text(r.document_id, page)) == normalize(expected)


def test_every_chunk_locatable_in_independent_extraction(storage, embedder, text_pdf):
    """Proxy for the viewer's text layer: pypdf 'layout' mode produces the same
    characters with very different whitespace. Every chunk must map onto it."""
    path, _ = text_pdf
    r = ingest.ingest_pdf(path, storage, embedder)
    reader = PdfReader(path)
    layout = {i: p.extract_text(extraction_mode="layout") for i, p in enumerate(reader.pages, 1)}
    chunks = _all_chunks(storage, r.document_id)
    for page, s, e, text in chunks:
        assert layout[page] != storage.get_page_text(r.document_id, page)  # really different
        span = locate(text, layout[page])
        assert span is not None, f"chunk on page {page} [{s}:{e}] not locatable"
        assert normalize(layout[page][span[0]:span[1]]) == normalize(text)


def test_known_sentence_cites_correct_page_and_span(storage, embedder, text_pdf):
    path, _ = text_pdf
    r = ingest.ingest_pdf(path, storage, embedder)
    target = make_sentence(2, 17)                      # drawn on page 2
    hits = storage.keyword_search('"P2-S17"')
    assert hits, "FTS5 found nothing"
    hit = hits[0]
    assert hit.page_number == 2 and hit.source_path == os.path.abspath(path)

    page_text = storage.get_page_text(hit.document_id, 2)
    # The sentence wraps across lines in the PDF; find it in the page text.
    span = locate(target, page_text)
    assert span is not None
    assert hit.char_start <= span[0] and span[1] <= hit.char_end or \
        hit.char_start < span[1] and span[0] < hit.char_end   # fully or partly inside
    # Highlight the sentence within the cited chunk
    inner = locate(target, hit.text) or locate("(ref. P2-S17)", hit.text)
    assert inner is not None


def test_locate_handles_ligatures_and_refuses_ambiguity():
    assert locate("financial filing", "the \ufb01nancial\n\ufb01ling is due") is not None
    assert locate("same", "same text, same text") is None      # ambiguous -> None
    assert locate("absent", "nothing here") is None


def test_storage_rejects_chunk_with_wrong_offsets(storage):
    pages = [PageRecord(1, "Hello world, this is page one.", "ok", "pypdf")]
    bad = [ChunkRecord(1, 0, 5, "world")]                          # text != page[0:5]
    with pytest.raises(ValueError, match="does not match"):
        storage.ingest_document("/x.pdf", "h", "ok", pages, bad, [[0.0] * 8])


def test_reingest_replaces_not_duplicates(storage, embedder, text_pdf):
    path, _ = text_pdf
    r1 = ingest.ingest_pdf(path, storage, embedder)
    r2 = ingest.ingest_pdf(path, storage, embedder)
    n = storage.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    assert n == r2.chunk_count == r1.chunk_count
    assert len(storage.keyword_search('"P1-S3"')) == 1


# ---------------------------------------------------------------------------
# Scanned / problem PDFs
# ---------------------------------------------------------------------------

def test_scanned_pdf_is_reported_not_silently_empty(storage, embedder, tmp_path):
    path = str(tmp_path / "scan.pdf")
    draw_scanned_pdf(path, n_pages=2)
    r = ingest.ingest_pdf(path, storage, embedder)

    assert r.status == ingest.DOC_NEEDS_OCR
    assert r.chunk_count == 0
    assert r.pages_needing_ocr == [1, 2]
    assert not r.fully_indexed
    assert "NOT searchable" in r.message and "OCR" in r.message
    # Recorded, so the UI can show it with a "needs OCR" badge
    doc = storage.get_document(os.path.abspath(path))
    assert doc["status"] == "needs_ocr"
    assert [p["status"] for p in doc["pages"]] == ["no_text_has_images"] * 2


def test_scan_with_bates_overlay_still_flagged(storage, embedder, tmp_path):
    """Scans often carry a text-layer Bates stamp. That must not count as 'has text'."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = str(tmp_path / "bates.pdf")
    c = canvas.Canvas(path, pagesize=letter)
    c.drawImage(ImageReader(text_image(make_sentence(1, 1) * 5)), 0, 0, 612, 792)
    c.setFont("Helvetica", 8)
    c.drawString(500, 20, "ABC000123")
    c.save()

    r = ingest.ingest_pdf(path, storage, embedder)
    assert r.status == ingest.DOC_NEEDS_OCR
    assert r.pages_needing_ocr == [1]
    assert storage.keyword_search("ABC000123"), "Bates number should stay searchable"


def test_mixed_text_and_scanned_is_partial(storage, embedder, tmp_path):
    text_path, scan_path = str(tmp_path / "t.pdf"), str(tmp_path / "s.pdf")
    draw_text_pdf(text_path, ground_truth_pages(n_pages=1))
    draw_scanned_pdf(scan_path, n_pages=1)
    w = PdfWriter()
    w.append(text_path)
    w.append(scan_path)
    mixed = str(tmp_path / "mixed.pdf")
    w.write(mixed)

    r = ingest.ingest_pdf(mixed, storage, embedder)
    assert r.status == ingest.DOC_PARTIAL
    assert r.pages_ok == [1] and r.pages_needing_ocr == [2]
    assert "Pages 2 are scanned" in r.message
    assert r.chunk_count > 0


def test_blank_pdf_is_empty_not_needs_ocr(storage, embedder, tmp_path):
    w = PdfWriter()
    w.add_blank_page(612, 792)
    path = str(tmp_path / "blank.pdf")
    w.write(path)
    r = ingest.ingest_pdf(path, storage, embedder)
    assert r.status == ingest.DOC_EMPTY and r.pages_blank == [1]


def test_user_password_pdf_is_reported(storage, embedder, tmp_path, text_pdf):
    src, _ = text_pdf
    w = PdfWriter(clone_from=src)
    w.encrypt(user_password="secret", owner_password="owner", algorithm="AES-256")
    path = str(tmp_path / "locked.pdf")
    w.write(path)
    r = ingest.ingest_pdf(path, storage, embedder)
    assert r.status == ingest.DOC_PASSWORD
    assert storage.conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_owner_password_only_pdf_is_indexed(storage, embedder, tmp_path, text_pdf):
    """Court filings are often AES-encrypted with an owner password only
    (opens without a password, restricts editing). These must ingest."""
    src, _ = text_pdf
    w = PdfWriter(clone_from=src)
    w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
    path = str(tmp_path / "restricted.pdf")
    w.write(path)
    r = ingest.ingest_pdf(path, storage, embedder)
    assert r.status == ingest.DOC_OK, r.message
    assert r.chunk_count > 0
