"""
Storage self-test. Runs INSIDE the frozen PyInstaller binary via
`backend --selftest-storage` to prove SQLCipher + sqlite-vec + FTS5 +
OS keychain all work in the shipped artifact, not just in the dev venv.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
import tempfile
import traceback

from storage import (ENCRYPTION_MODE, ChunkRecord, KeyProvider, PageRecord,
                     StaticKeyProvider, Storage)

DIM = 8
CANARY = "zqxcanary" + secrets.token_hex(6)  # unique token we grep the raw file for


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


def _minimal_pdf(text: str) -> bytes:
    """Hand-built one-page PDF with a Helvetica text layer (no reportlab in the bundle)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def run() -> int:
    results: dict[str, object] = {"frozen": bool(getattr(sys, "frozen", False))}
    tmp = tempfile.mkdtemp(prefix="storage-selftest-")
    db_path = os.path.join(tmp, "test.db")
    # Separate keychain entry / key dir so the self-test never touches the real
    # DB key. Backend selection is the real one: keychain if available, else
    # the 0600 key file (or whatever AIDESK_KEY_BACKEND forces).
    kp = KeyProvider(service="com.ai-desktop-spike.selftest", username="selftest",
                     key_dir=os.path.join(tmp, "keys"))
    ok = True

    def check(name: str, cond: bool, detail: object = None):
        nonlocal ok
        results[name] = {"pass": bool(cond), **({"detail": detail} if detail is not None else {})}
        ok = ok and bool(cond)

    try:
        # 1. Open with a key from the real OS keychain
        s = Storage(db_path, DIM, key_provider=kp)
        info = s.info()
        results["info"] = info
        check("sqlcipher_active", bool(info["cipher_version"]), info["cipher_version"])
        check("sqlite_vec_loaded", bool(info["vec_version"]), info["vec_version"])
        check("fts5_enabled", info["fts5"])
        check("mode_is_sqlcipher", ENCRYPTION_MODE == "sqlcipher-full-db")
        check("key_backend_reported", info["key_backend"] in ("os-keychain", "key-file-0600"),
              f'{info["key_backend"]} ({info["key_backend_detail"]})')

        # 2. Write: document + pages + chunks with citation metadata (atomic)
        p1 = "The parties agree to the terms below."
        p2 = f"Section 4. Indemnification clause {CANARY} applies."
        p3 = "Termination requires thirty days notice."
        pages = [PageRecord(1, p1, "ok", "pypdf"), PageRecord(2, p2, "ok", "pypdf"),
                 PageRecord(3, p3, "ok", "pypdf")]
        s2_start = p2.index("Indemnification")
        chunks = [ChunkRecord(1, 0, len(p1), p1),
                  ChunkRecord(2, s2_start, len(p2), p2[s2_start:]),
                  ChunkRecord(3, 0, len(p3), p3)]
        s.ingest_document("/tmp/contract.pdf", "deadbeef", "ok", pages, chunks,
                          [_vec(0), _vec(1), _vec(2)])

        # 3. FTS5 keyword search returns correct citation
        hits = s.keyword_search(CANARY)
        check("fts5_search",
              len(hits) == 1 and hits[0].page_number == 2
              and (hits[0].char_start, hits[0].char_end) == (s2_start, len(p2))
              and s.get_page_text(hits[0].document_id, 2)[hits[0].char_start:hits[0].char_end] == hits[0].text
              and hits[0].source_path == "/tmp/contract.pdf",
              [h.__dict__ for h in hits])

        # 4. Vector KNN returns nearest chunk
        vhits = s.vector_search(_vec(2), limit=1)
        check("vector_search", len(vhits) == 1 and vhits[0].page_number == 3,
              [h.__dict__ for h in vhits])
        s.close()  # checkpoints WAL into main file

        # 5. Raw file is encrypted: no SQLite header, canary not present
        raw = b""
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                with open(p, "rb") as f:
                    raw += f.read()
        with open(db_path, "rb") as f:
            header = f.read(16)
        check("file_header_not_plain_sqlite", header != b"SQLite format 3\x00", header.hex())
        check("canary_absent_from_disk", CANARY.encode() not in raw)
        check("chunk_text_absent_from_disk", b"Indemnification" not in raw)

        # 6. Wrong key is rejected
        try:
            Storage(db_path, DIM, key_provider=StaticKeyProvider(secrets.token_bytes(32))).close()
            check("wrong_key_rejected", False, "opened with wrong key!")
        except Exception as e:  # sqlcipher3.DatabaseError: file is not a database
            check("wrong_key_rejected", True, f"{type(e).__name__}: {e}")

        # 7. Correct key reopens and data persists
        s2 = Storage(db_path, DIM, key_provider=kp)
        check("reopen_persists", len(s2.keyword_search("Termination")) == 1)

        # 8. pypdf works frozen: parse a real PDF end-to-end into storage
        import ingest

        class _Emb:
            dim = DIM
            def embed(self, texts):
                return [_vec(i) for i in range(len(texts))]

        pdf_path = os.path.join(tmp, "frozen.pdf")
        with open(pdf_path, "wb") as f:
            f.write(_minimal_pdf("Frozen pypdf check: the Lessee shall pay rent monthly. " * 3))
        rep = ingest.ingest_pdf(pdf_path, s2, _Emb())
        check("pypdf_ingest_frozen",
              rep.status == "ok" and rep.chunk_count == 1
              and s2.keyword_search("Lessee")[0].page_number == 1,
              rep.message)

        # 9. AES-encrypted (owner-password-only) PDF: needs `cryptography` bundled
        from pypdf import PdfWriter
        w = PdfWriter(clone_from=pdf_path)
        w.encrypt(user_password="", owner_password="owner", algorithm="AES-256")
        aes_path = os.path.join(tmp, "aes.pdf")
        w.write(aes_path)
        rep_aes = ingest.ingest_pdf(aes_path, s2, _Emb())
        check("pypdf_aes_frozen", rep_aes.status == "ok" and rep_aes.chunk_count == 1,
              rep_aes.message)
        s2.close()

    except Exception:
        ok = False
        results["exception"] = traceback.format_exc()
    finally:
        try:
            kp.delete_key()
        except Exception as e:
            results["keyring_cleanup_error"] = repr(e)
        shutil.rmtree(tmp, ignore_errors=True)

    results["ALL_PASS"] = ok
    print(json.dumps(results, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
