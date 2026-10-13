"""Question answering over one ingested document: retrieve, prompt, generate.

Retrieval is keyword search (SQLite FTS5, BM25 ranking) inside the encrypted
database. The chat model is not an embedding model, and llama-server serves
/embedding only when started in embedding-only mode, so vector search waits
for a dedicated embedding model. Each passage keeps its page number, so every
answer can cite where it came from.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

LLAMA_URL = "http://127.0.0.1:8080"
TOP_K = 4
MAX_PASSAGE_CHARS = 1200

_STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does doing for from had has have
having he her here hers him his how i if in into is it its itself just me more most my no nor
not of on once only or other our ours out over own same she should so some such than that the
their theirs them then there these they this those through to too under until up very was we
were what when where which while who whom why will with would you your yours tell please
give show explain describe say says said
""".split())


@dataclass
class Passage:
    page: int
    text: str


def build_fts_query(question: str, max_terms: int = 16) -> str:
    """Question -> safe FTS5 query: quoted prefix terms joined by OR.

    Only word characters reach the query, so user input can never inject
    FTS5 syntax (quotes, NEAR, column filters, ...)."""
    tokens = re.findall(r"[^\W_]+", question.lower())
    keep = [t for t in tokens if len(t) > 1 and t not in _STOPWORDS] or tokens
    seen: list[str] = []
    for t in keep:
        if t not in seen:
            seen.append(t)
    return " OR ".join(f'"{t}"*' for t in seen[:max_terms])


def retrieve(storage, document_id: int, question: str, k: int = TOP_K) -> list[Passage]:
    """Best-matching passages of one document; falls back to its first pages
    when nothing matches (e.g. "What is this document about?")."""
    passages: list[Passage] = []
    query = build_fts_query(question)
    if query:
        for hit in storage.keyword_search(query, limit=80):
            if hit.document_id == document_id:
                passages.append(Passage(hit.page_number, hit.text[:MAX_PASSAGE_CHARS]))
                if len(passages) >= k:
                    break
    if not passages:
        for page in (1, 2):
            text = storage.get_page_text(document_id, page)
            if text and text.strip():
                passages.append(Passage(page, text[:MAX_PASSAGE_CHARS * 2]))
    return passages


SYSTEM_PROMPT = (
    "You answer questions about the user's document using ONLY the excerpts provided. "
    "Each excerpt starts with its page number, like [Page 3]. "
    "Always end your answer with the page numbers you used, in the form (p. 3) or (p. 2, p. 5). "
    "If the excerpts do not contain the answer, reply exactly: \"I couldn't find that in the document.\" "
    "Do not use outside knowledge. Be concise and precise."
)


def build_messages(question: str, passages: list[Passage]) -> list[dict]:
    excerpts = "\n\n".join(f"[Page {p.page}]\n{p.text.strip()}" for p in passages)
    user = f"Document excerpts:\n\n{excerpts}\n\n---\nQuestion: {question.strip()}"
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def cited_pages(answer: str) -> set[int]:
    pages: set[int] = set()
    for group in re.findall(r"\b(?:p\.|pp\.|pages?)\s*(\d+(?:\s*(?:,|and|&|-)\s*(?:p\.\s*)?\d+)*)",
                            answer, re.IGNORECASE):
        pages.update(int(n) for n in re.findall(r"\d+", group))
    return pages


def llm_healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{LLAMA_URL}/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def generate(messages: list[dict], max_tokens: int = 512, timeout: float = 600.0) -> str:
    body = json.dumps({
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()
    req = urllib.request.Request(f"{LLAMA_URL}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    return (data["choices"][0]["message"].get("content") or "").strip()


def citations_for(answer: str, passages: list[Passage]) -> list[dict]:
    """One citation per page. Pages the answer cites come first (flagged
    `cited`), then the rest in retrieval rank order (best match first)."""
    cited = cited_pages(answer)
    by_page: dict[int, tuple[int, Passage]] = {}
    for rank, p in enumerate(passages):
        by_page.setdefault(p.page, (rank, p))
    out = [{"page": pg, "text": p.text, "cited": pg in cited, "_rank": rank}
           for pg, (rank, p) in by_page.items()]
    out.sort(key=lambda c: (not c["cited"], c["_rank"]))
    for c in out:
        del c["_rank"]
    return out
