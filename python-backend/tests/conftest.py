import hashlib
import io
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage import StaticKeyProvider, Storage  # noqa: E402

DIM = 8

PARTIES = ["Lessor", "Lessee", "Guarantor", "Contractor", "Client", "Trustee"]
VERBS = ["shall indemnify", "shall notify", "may terminate", "shall reimburse",
         "must disclose to", "shall not assign obligations to"]


def make_sentence(page: int, n: int) -> str:
    a = PARTIES[(page + n) % len(PARTIES)]
    b = PARTIES[(page + 2 * n + 1) % len(PARTIES)]
    v = VERBS[(n * 7 + page) % len(VERBS)]
    return (f"Clause {page}.{n}: The {a} {v} the {b} within {10 + n} days "
            f"of written notice, pursuant to \u00a7{page}{n:02d} of the Agreement "
            f"(ref. P{page}-S{n}).")


def ground_truth_pages(n_pages: int = 3, sentences_per_page: int = 30) -> list[list[str]]:
    """Paragraphs per page; each paragraph is a list of sentences."""
    pages = []
    for p in range(1, n_pages + 1):
        sents = [make_sentence(p, n) for n in range(1, sentences_per_page + 1)]
        pages.append([" ".join(sents[i:i + 5]) for i in range(0, len(sents), 5)])
    return pages


def draw_text_pdf(path: str, pages: list[list[str]]) -> list[str]:
    """Draw paragraphs line by line. Returns the exact text drawn per page."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(path, pagesize=letter)
    drawn = []
    for paras in pages:
        y = 750
        page_lines = []
        c.setFont("Helvetica", 9)
        for para in paras:
            for line in textwrap.wrap(para, 100):
                c.drawString(50, y, line)
                page_lines.append(line)
                y -= 12
            y -= 10
        drawn.append("\n".join(page_lines))
        c.showPage()
    c.save()
    return drawn


def text_image(text: str):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1275, 1650), "white")  # letter @ 150 dpi
    d = ImageDraw.Draw(img)
    y = 100
    for line in textwrap.wrap(text, 70):
        d.text((100, y), line, fill="black")
        y += 30
    return img


def draw_scanned_pdf(path: str, n_pages: int = 2):
    imgs = [text_image(" ".join(make_sentence(p, n) for n in range(1, 8)))
            for p in range(1, n_pages + 1)]
    imgs[0].save(path, "PDF", resolution=150, save_all=True, append_images=imgs[1:])


class FakeEmbedder:
    """Deterministic stand-in. The real one calls llama-server's /embedding."""
    dim = DIM

    def embed(self, texts):
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([b / 255.0 for b in h[:DIM]])
        return out


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "t.db"), DIM, key_provider=StaticKeyProvider(os.urandom(32)))
    yield s
    s.close()


@pytest.fixture
def embedder():
    return FakeEmbedder()
