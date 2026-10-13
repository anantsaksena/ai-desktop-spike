import hashlib
import os

import pytest

import qa
from conftest import draw_text_pdf, ground_truth_pages
from ingest import ingest_pdf
from model_download import ModelDownloader, ModelSpec, VerifyError, verify_file


# ------------------------------------------------------------------ qa

def test_fts_query_drops_stopwords_and_quotes_terms():
    q = qa.build_fts_query("Who must the Lessee notify?")
    assert q == '"must"* OR "lessee"* OR "notify"*'


def test_fts_query_cannot_inject_syntax():
    q = qa.build_fts_query('foo" OR bar NEAR(x) col:* ) -- ;')
    # Only word characters survive; every term is a quoted prefix.
    for term in q.split(" OR "):
        assert term.startswith('"') and term.endswith('"*')
        assert '"' not in term[1:-2]


def test_fts_query_empty_for_punctuation():
    assert qa.build_fts_query("?!") == ""


def test_retrieve_finds_page_and_cites_it(tmp_path, storage, embedder):
    pdf = str(tmp_path / "lease.pdf")
    draw_text_pdf(pdf, ground_truth_pages(n_pages=3, sentences_per_page=10))
    doc = ingest_pdf(pdf, storage, embedder).document_id
    # Reference string unique to page 2.
    passages = qa.retrieve(storage, doc, "What does clause P2-S3 say?")
    assert passages and passages[0].page == 2
    cites = qa.citations_for("See (p. 2).", passages)
    assert cites[0]["page"] == 2 and cites[0]["cited"] is True


def test_retrieve_falls_back_to_first_pages(tmp_path, storage, embedder):
    pdf = str(tmp_path / "lease.pdf")
    draw_text_pdf(pdf, ground_truth_pages(n_pages=3, sentences_per_page=5))
    doc = ingest_pdf(pdf, storage, embedder).document_id
    passages = qa.retrieve(storage, doc, "zzzzqqq")
    assert [p.page for p in passages] == [1, 2]


def test_retrieve_is_scoped_to_document(tmp_path, storage, embedder):
    a, b = str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")
    draw_text_pdf(a, ground_truth_pages(n_pages=1, sentences_per_page=5))
    draw_text_pdf(b, [["Completely different words about zebras."]])
    doc_b = ingest_pdf(b, storage, embedder).document_id
    ingest_pdf(a, storage, embedder)
    assert all("zebras" in p.text for p in qa.retrieve(storage, doc_b, "Lessee zebras"))


def test_messages_contain_pages_and_question():
    msgs = qa.build_messages("Q?", [qa.Passage(3, "text three")])
    assert msgs[0]["role"] == "system"
    assert "[Page 3]" in msgs[1]["content"] and "Question: Q?" in msgs[1]["content"]


def test_cited_pages_parsing():
    assert qa.cited_pages("As stated (p. 3) and on page 12, also pp. 4") == {3, 12, 4}


# ------------------------------------------------------------------ model download

def _fake_model(tmp_path, payload=b"x" * 5000, magic=b"GGUF\x03\x00\x00\x00"):
    src = tmp_path / "src.gguf"
    src.write_bytes(magic + payload)
    data = src.read_bytes()
    spec = ModelSpec("fake", "m.gguf", src.as_uri(), hashlib.sha256(data).hexdigest(), len(data), "test")
    return src, spec


def test_verify_accepts_exact_file(tmp_path):
    src, spec = _fake_model(tmp_path)
    verify_file(str(src), spec)


@pytest.mark.parametrize("mutate", ["sha", "size", "magic"])
def test_verify_rejects_bad_file(tmp_path, mutate):
    src, spec = _fake_model(tmp_path, magic=b"<html>\x00\x00" if mutate == "magic" else b"GGUF\x03\x00\x00\x00")
    if mutate == "sha":
        spec = ModelSpec(spec.name, spec.filename, spec.url, "0" * 64, spec.size, spec.license)
    if mutate == "size":
        spec = ModelSpec(spec.name, spec.filename, spec.url, spec.sha256, spec.size + 1, spec.license)
    with pytest.raises(VerifyError):
        verify_file(str(src), spec)


def _wait(d, timeout=10):
    import time
    end = time.time() + timeout
    while time.time() < end and d.status()["state"] in ("downloading", "verifying"):
        time.sleep(0.05)
    return d.status()


def test_download_verifies_then_moves_into_place(tmp_path):
    _, spec = _fake_model(tmp_path)
    dest = str(tmp_path / "models" / "m.gguf")
    d = ModelDownloader(dest, spec)
    d.start()
    st = _wait(d)
    assert st["state"] == "ready", st
    assert os.path.getsize(dest) == spec.size and not os.path.exists(dest + ".part")


def test_download_with_wrong_hash_leaves_nothing(tmp_path):
    _, spec = _fake_model(tmp_path)
    bad = ModelSpec(spec.name, spec.filename, spec.url, "f" * 64, spec.size, spec.license)
    dest = str(tmp_path / "models" / "m.gguf")
    d = ModelDownloader(dest, bad)
    d.start()
    st = _wait(d)
    assert st["state"] == "error" and "SHA-256" in st["error"]
    assert not os.path.exists(dest) and not os.path.exists(dest + ".part")
