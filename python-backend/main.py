import argparse
import os
import sys
import threading
import time
import tempfile
import requests

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

import ingest
from storage import Storage

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class LlamaEmbedder:
    def __init__(self):
        self.dim = 288 # TinyLlama embedding dimension
        
    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            # llama.cpp /embedding takes {"content": "..."}
            res = requests.post("http://127.0.0.1:8080/embedding", json={"content": texts[0]})
            res.raise_for_status()
            data = res.json()
            if "embedding" in data:
                return [data["embedding"]] * len(texts) # simplified for spike: embed one by one or assume len=1
        except Exception as e:
            print(f"Embedding failed: {e}")
        return [[0.0] * self.dim for _ in texts]

db_path = os.path.join(os.path.expanduser("~"), ".ai-desktop-spike.db")
storage = Storage(db_path, embedding_dim=288)
embedder = LlamaEmbedder()

@app.get("/health")
def health():
    return {"status": "backend OK"}

@app.post("/upload")
async def upload_pdf(file: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        report = ingest.ingest_pdf(tmp_path, storage, embedder)
        return {
            "status": report.status,
            "document_id": report.document_id,
            "message": report.message
        }
    finally:
        os.remove(tmp_path)

class AskRequest(BaseModel):
    document_id: int
    question: str

@app.post("/ask")
def ask_question(req: AskRequest):
    question_emb = embedder.embed([req.question])[0]
    hits = storage.vector_search(question_emb, limit=3)
    
    context_texts = []
    citations = []
    for hit in hits:
        if hit.document_id == req.document_id:
            context_texts.append(hit.text)
            citations.append({"page": hit.page_number, "text": hit.text[:50] + "..."})
            
    context_str = "\n\n".join(context_texts)
    prompt = f"Based on the following document excerpts, answer the question.\n\nContext:\n{context_str}\n\nQuestion: {req.question}\nAnswer:"
    
    try:
        res = requests.post("http://127.0.0.1:8080/completion", json={
            "prompt": prompt,
            "n_predict": 100,
            "temperature": 0.1
        })
        res.raise_for_status()
        answer = res.json().get("content", "")
    except Exception as e:
        answer = f"Failed to generate answer: {e}"
        
    return {
        "answer": answer.strip(),
        "citations": citations
    }



def _bundle_dir() -> str:
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def run_full_selftest() -> int:
    """Storage/crypto self-test + the pytest suite, run INSIDE this binary."""
    import selftest_storage
    rc = selftest_storage.run()
    import pytest
    tests = os.path.join(_bundle_dir(), "tests")
    # -p no:cacheprovider: don't write .pytest_cache into the bundle dir.
    rc_tests = pytest.main([tests, "-q", "-p", "no:cacheprovider", "--rootdir", tests])
    print(f"SELFTEST storage_rc={rc} pytest_rc={int(rc_tests)}")
    return 0 if rc == 0 and int(rc_tests) == 0 else 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_parent_watchdog(parent_pid: int, also_kill: list[int]) -> None:
    """Exit if the desktop app dies without shutting us down (crash, SIGKILL).
    PyInstaller one-file runs us as a child of a bootloader, so a SIGKILL to
    the bootloader would otherwise orphan this process and keep port 8000
    bound. On the way out, also terminate llama-server (`also_kill`)."""
    import signal

    def loop():
        while _pid_alive(parent_pid):
            time.sleep(1.0)
        for pid in also_kill:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        os._exit(0)

    threading.Thread(target=loop, daemon=True, name="parent-watchdog").start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest-storage", action="store_true",
                        help="Run encrypted-storage self-test and exit")
    parser.add_argument("--selftest-full", action="store_true",
                        help="Storage self-test + full pytest suite, then exit")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="PID of the desktop app; exit when it disappears")
    parser.add_argument("--kill-on-exit", type=int, action="append", default=[],
                        help="PID to SIGTERM if the parent dies (llama-server)")
    args, _ = parser.parse_known_args()

    if args.selftest_storage:
        import selftest_storage
        sys.exit(selftest_storage.run())
    if args.selftest_full:
        sys.exit(run_full_selftest())

    if args.parent_pid:
        start_parent_watchdog(args.parent_pid, args.kill_on_exit)

    # Localhost-only sidecar. Never bind 0.0.0.0.
    uvicorn.run(app, host="127.0.0.1", port=8000)
