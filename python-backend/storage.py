"""
Storage module — the ONLY place in the backend that touches the database
or the encryption key. Everything else talks to `Storage`.

=============================================================================
ACTIVE ENCRYPTION APPROACH  (keep in sync with the security whitepaper)
=============================================================================
ENCRYPTION_MODE = "sqlcipher-full-db"

  * Engine:      SQLCipher 4.19.0 community (SQLite 3.53.4), compiled from
                 source and linked into the `sqlcipher3` Python extension.
                 OpenSSL 3 libcrypto is STATICALLY linked (no system OpenSSL
                 dependency at runtime).
  * Scope:       Whole-file encryption. Every page of the database file is
                 encrypted, including the FTS5 inverted index, the sqlite-vec
                 vector tables, and all metadata. The WAL/journal is
                 encrypted too. Temp storage is forced to RAM
                 (SQLITE_TEMP_STORE=2), so no plaintext temp files hit disk.
  * Cipher:      SQLCipher 4 defaults: AES-256-CBC per page, HMAC-SHA512 per
                 page (tamper detection), random per-page IV.
  * Key:         32 random bytes from `secrets.token_bytes`, generated on
                 first run. Passed to SQLCipher as a raw key
                 (PRAGMA key = "x'...'"), so no PBKDF2 derivation is needed:
                 the key is already full-entropy. Where the key lives depends
                 on the KEY BACKEND, reported at runtime by
                 `Storage.info()["key_backend"]`:

                 "os-keychain"  (preferred) OS credential store via `keyring`:
                     macOS Keychain, Windows Credential Manager, or a Linux
                     D-Bus Secret Service (gnome-keyring, KWallet, KeePassXC).
                     The key is protected by the user's login keyring.

                 "key-file-0600"  (fallback: Linux with no Secret Service,
                     e.g. headless or minimal installs, or forced with
                     AIDESK_KEY_BACKEND=file). The key is stored hex-encoded
                     in <app data dir>/db.key, mode 0600, directory 0700,
                     owned by the user. The app refuses to use the file if its
                     permissions are looser than 0600 or it belongs to another
                     user.
                     HONEST LIMITS of this mode: the key sits on the same disk
                     as the database, protected only by file permissions.
                     Root, any process running as this user, and anyone who
                     reads the disk directly can read it. Without full-disk
                     encryption (LUKS) it does NOT protect a stolen laptop.
                     It still protects a database copied on its own (e.g. a
                     sync or backup that excludes db.key). Backups of the whole
                     app data dir include the key.

                 Selection rules (enforced in KeyProvider):
                   1. A key found in the keychain is used; else a key found
                      in the key file is used.
                   2. A new key is only generated when NO key exists anywhere
                      AND the database file does not exist yet. A missing key
                      for an existing database is a hard error, never a
                      silent re-key (which would look like data loss).
                   3. If a keychain backend exists but errors (e.g. locked),
                      that is a hard error. The app never silently downgrades
                      to the key file. The file is used only when no keychain
                      backend exists at all, or when explicitly forced.
  * Not covered: plaintext while the app is running (in process memory),
                 the original source documents on disk (we index them, we
                 do not move or encrypt them), and the model files.

Fallback (NOT active): "app-layer-aes-gcm+hmac-term-index"
  stock sqlite3 + sqlite-vec, AES-GCM on sensitive columns, keyed-HMAC
  token index instead of FTS5. Only used if SQLCipher cannot be shipped on
  a target platform. Would be implemented as a second class with the same
  public interface as `Storage`.
=============================================================================
"""

from __future__ import annotations

import os
import secrets
import struct
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import sqlcipher3
import sqlite_vec

ENCRYPTION_MODE = "sqlcipher-full-db"

KEYRING_SERVICE = "com.ai-desktop-spike.storage"
KEYRING_USERNAME = "db-key-v1"

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------

class KeyError_(RuntimeError):
    """Key cannot be obtained safely. Message is meant to be shown to the user."""


KEY_BACKEND_KEYCHAIN = "os-keychain"
KEY_BACKEND_FILE = "key-file-0600"
KEY_FILE_NAME = "db.key"


def _keychain_backend_name():
    """Return the usable keyring backend's name, or None if there is none.
    Never raises: 'no backend' and 'broken environment' both mean None."""
    try:
        import keyring
        from keyring.backends import chainer, fail
        kr = keyring.get_keyring()
    except Exception:
        return None
    if isinstance(kr, fail.Keyring):
        return None
    if isinstance(kr, chainer.ChainerBackend) and not kr.backends:
        return None
    if isinstance(kr, chainer.ChainerBackend):
        return "+".join(type(b).__module__ + "." + type(b).__name__ for b in kr.backends)
    return type(kr).__module__ + "." + type(kr).__name__


class KeyProvider:
    """Gets (or creates) the database key. See module docstring for the rules.

    After get_or_create_key(), `active_backend` is KEY_BACKEND_KEYCHAIN or
    KEY_BACKEND_FILE and `backend_detail` names the keyring class or file path.
    """

    def __init__(self, service: str = KEYRING_SERVICE, username: str = KEYRING_USERNAME,
                 key_dir: Optional[str] = None, force: Optional[str] = None):
        self.service = service
        self.username = username
        self.key_dir = key_dir                      # defaults to the DB's directory
        self.force = force or os.environ.get("AIDESK_KEY_BACKEND")  # "file" | "keychain"
        self.active_backend: Optional[str] = None
        self.backend_detail: Optional[str] = None

    # -- public -------------------------------------------------------------

    def get_or_create_key(self, db_path: str) -> bytes:
        key_dir = self.key_dir or os.path.dirname(os.path.abspath(db_path))
        key_file = os.path.join(key_dir, KEY_FILE_NAME)
        db_exists = os.path.exists(db_path) and os.path.getsize(db_path) > 0

        if self.force not in (None, "", "file", "keychain"):
            raise KeyError_(f"AIDESK_KEY_BACKEND must be 'file' or 'keychain', got {self.force!r}")

        kc_name = None if self.force == "file" else _keychain_backend_name()
        if self.force == "keychain" and kc_name is None:
            raise KeyError_("AIDESK_KEY_BACKEND=keychain but no OS keychain is available "
                            "(on Linux: is a Secret Service such as gnome-keyring running?)")

        # 1. Existing key: keychain first, then file.
        if kc_name is not None:
            hex_key = self._keychain_get()          # raises KeyError_ if keychain errors
            if hex_key is not None:
                return self._done(hex_key, KEY_BACKEND_KEYCHAIN, kc_name)
        if os.path.exists(key_file):
            return self._done(self._file_read(key_file), KEY_BACKEND_FILE, key_file)

        # 2. No key anywhere. Never re-key an existing database.
        if db_exists:
            raise KeyError_(
                f"The encryption key for {db_path} was not found "
                f"(looked in: {'OS keychain, ' if kc_name else ''}{key_file}). "
                f"Refusing to create a new key, because the existing data would "
                f"become unreadable. Restore the key, or move the database aside "
                f"to start fresh.")

        # 3. New key: keychain if one exists, else the 0600 file.
        hex_key = secrets.token_bytes(32).hex()
        if kc_name is not None:
            self._keychain_set(hex_key)
            return self._done(hex_key, KEY_BACKEND_KEYCHAIN, kc_name)
        self._file_create(key_dir, key_file, hex_key)
        return self._done(hex_key, KEY_BACKEND_FILE, key_file)

    def delete_key(self, db_path: Optional[str] = None) -> None:
        """Tests/self-test only."""
        if self.active_backend == KEY_BACKEND_FILE and self.backend_detail:
            try:
                os.remove(self.backend_detail)
            except FileNotFoundError:
                pass
        elif self.active_backend == KEY_BACKEND_KEYCHAIN:
            import keyring
            try:
                keyring.delete_password(self.service, self.username)
            except keyring.errors.PasswordDeleteError:
                pass

    # -- internals ----------------------------------------------------------

    def _done(self, hex_key: str, backend: str, detail: str) -> bytes:
        key = bytes.fromhex(hex_key.strip())
        if len(key) != 32:
            raise KeyError_(f"Stored database key ({backend}) has the wrong length")
        self.active_backend, self.backend_detail = backend, detail
        return key

    def _keychain_get(self) -> Optional[str]:
        import keyring
        try:
            return keyring.get_password(self.service, self.username)
        except Exception as e:
            raise KeyError_(f"The OS keychain is present but could not be read "
                            f"({type(e).__name__}: {e}). Unlock it and retry. "
                            f"Not falling back to a key file.") from e

    def _keychain_set(self, hex_key: str) -> None:
        import keyring
        try:
            keyring.set_password(self.service, self.username, hex_key)
        except Exception as e:
            raise KeyError_(f"Could not save the key to the OS keychain "
                            f"({type(e).__name__}: {e}).") from e

    @staticmethod
    def _file_read(path: str) -> str:
        st = os.stat(path)
        if os.name == "posix":
            if st.st_uid != os.getuid():
                raise KeyError_(f"{path} is owned by another user; refusing to use it.")
            if st.st_mode & 0o077:
                raise KeyError_(f"{path} has permissions {oct(st.st_mode & 0o777)}; "
                                f"it must be 0600. Fix with: chmod 600 '{path}'")
        with open(path, "r", encoding="ascii") as f:
            return f.read()

    @staticmethod
    def _file_create(key_dir: str, path: str, hex_key: str) -> None:
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        if os.name == "posix":
            os.chmod(key_dir, os.stat(key_dir).st_mode & ~0o077)
        # O_EXCL: never overwrite an existing key file (race-safe).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, hex_key.encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)


class StaticKeyProvider(KeyProvider):
    """For tests only. Never used in the shipped app."""

    def __init__(self, key: bytes):
        super().__init__()
        self._key = key

    def get_or_create_key(self, db_path: str) -> bytes:
        self.active_backend, self.backend_detail = "static-test-key", "test"
        return self._key

    def delete_key(self, db_path: Optional[str] = None) -> None:
        pass


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class PageRecord:
    page_number: int          # 1-based
    text: str                 # exact extracted text; chunk offsets index into this
    status: str               # ok | no_text_has_images | blank | error
    extraction_method: str    # "pypdf" (later: "tesseract")


@dataclass
class ChunkRecord:
    page_number: int
    char_start: int           # offset into PageRecord.text
    char_end: int             # exclusive
    text: str                 # == page.text[char_start:char_end]


@dataclass
class ChunkHit:
    chunk_id: int
    document_id: int
    source_path: str     # absolute path of the original PDF
    page_number: int     # 1-based, matches PDF viewer page numbering
    char_start: int      # offset into the extracted text of that page
    char_end: int        # exclusive
    text: str
    score: float         # bm25 (lower = better) or vector distance (lower = better)


def _serialize_f32(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class Storage:
    def __init__(self, db_path: str, embedding_dim: int,
                 key_provider: Optional[KeyProvider] = None):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._key_provider = key_provider or KeyProvider()
        self.conn = self._open()
        self._migrate()

    # -- connection ---------------------------------------------------------

    def _open(self) -> sqlcipher3.Connection:
        key = self._key_provider.get_or_create_key(self.db_path)
        conn = sqlcipher3.connect(self.db_path, check_same_thread=False)
        # PRAGMA key must be the first statement on the connection.
        conn.execute(f"PRAGMA key = \"x'{key.hex()}'\"")
        del key
        # Fails with "file is not a database" if the key is wrong.
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()

        if conn.execute("PRAGMA cipher_version").fetchone() is None:
            raise RuntimeError("SQLCipher not active — refusing to open unencrypted DB")

        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA temp_store = MEMORY")

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.row_factory = sqlcipher3.Row
        return conn

    def _migrate(self) -> None:
        # No shipped databases exist yet, so schema v1 is still being edited
        # in place. Once a build ships, every change becomes a numbered migration.
        cur_version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if cur_version >= SCHEMA_VERSION:
            return
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS documents (
                    id           INTEGER PRIMARY KEY,
                    source_path  TEXT NOT NULL UNIQUE,
                    sha256       TEXT NOT NULL,
                    page_count   INTEGER NOT NULL,
                    -- ok | partial (some pages need OCR) | needs_ocr (no text at all)
                    -- | password_protected | error
                    status       TEXT NOT NULL,
                    ingested_at  TEXT NOT NULL DEFAULT (datetime('now'))
                );

                -- One row per PDF page. `text` is the EXACT string chunk offsets
                -- refer to: chunks.text == substr(pages.text, char_start+1, char_end-char_start).
                CREATE TABLE IF NOT EXISTS pages (
                    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page_number  INTEGER NOT NULL CHECK (page_number >= 1),
                    text         TEXT NOT NULL,
                    -- ok | no_text_has_images (scanned: needs OCR) | blank | error
                    status       TEXT NOT NULL,
                    -- pypdf for now; 'tesseract' once OCR exists
                    extraction_method TEXT NOT NULL,
                    PRIMARY KEY (document_id, page_number)
                );

                CREATE TABLE IF NOT EXISTS chunks (
                    id           INTEGER PRIMARY KEY,
                    document_id  INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
                    page_number  INTEGER NOT NULL CHECK (page_number >= 1),
                    char_start   INTEGER NOT NULL CHECK (char_start >= 0),
                    char_end     INTEGER NOT NULL CHECK (char_end > char_start),
                    text         TEXT NOT NULL,
                    FOREIGN KEY (document_id, page_number) REFERENCES pages(document_id, page_number)
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id, page_number);

                -- External-content FTS5: index lives in the encrypted file,
                -- text is read from `chunks`. No plaintext copy outside SQLCipher.
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END;

                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                    embedding float[{self.embedding_dim}]
                );

                PRAGMA user_version = {SCHEMA_VERSION};
            """)

    # -- writes -------------------------------------------------------------

    def ingest_document(self, source_path: str, sha256: str, status: str,
                        pages: Sequence[PageRecord],
                        chunks: Sequence[ChunkRecord],
                        embeddings: Sequence[Sequence[float]]) -> int:
        """Atomically (re)index one document. Replaces any previous ingest of
        the same path. Either everything is written or nothing is."""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings length mismatch")
        page_text = {p.page_number: p.text for p in pages}
        for c, e in zip(chunks, embeddings):
            if len(e) != self.embedding_dim:
                raise ValueError(f"embedding dim {len(e)} != {self.embedding_dim}")
            # Enforce the citation invariant at the storage boundary.
            if page_text.get(c.page_number, "")[c.char_start:c.char_end] != c.text:
                raise ValueError(f"chunk text does not match page {c.page_number} "
                                 f"[{c.char_start}:{c.char_end}]")

        with self.conn:
            old = self.conn.execute(
                "SELECT id FROM documents WHERE source_path = ?", (source_path,)
            ).fetchone()
            if old:
                self._delete_document_rows(old[0])
            doc_id = self.conn.execute(
                "INSERT INTO documents (source_path, sha256, page_count, status) "
                "VALUES (?, ?, ?, ?)", (source_path, sha256, len(pages), status),
            ).lastrowid
            self.conn.executemany(
                "INSERT INTO pages (document_id, page_number, text, status, extraction_method) "
                "VALUES (?, ?, ?, ?, ?)",
                [(doc_id, p.page_number, p.text, p.status, p.extraction_method) for p in pages],
            )
            for c, e in zip(chunks, embeddings):
                chunk_id = self.conn.execute(
                    "INSERT INTO chunks (document_id, page_number, char_start, char_end, text) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (doc_id, c.page_number, c.char_start, c.char_end, c.text),
                ).lastrowid
                self.conn.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (chunk_id, _serialize_f32(e)),
                )
            return doc_id

    def _delete_document_rows(self, document_id: int) -> None:
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM chunks WHERE document_id = ?", (document_id,))]
        self.conn.executemany("DELETE FROM chunks_vec WHERE rowid = ?", [(i,) for i in ids])
        self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        self.conn.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        self.conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))

    def get_page_text(self, document_id: int, page_number: int) -> Optional[str]:
        r = self.conn.execute(
            "SELECT text FROM pages WHERE document_id = ? AND page_number = ?",
            (document_id, page_number)).fetchone()
        return r[0] if r else None

    def get_document(self, source_path: str) -> Optional[dict]:
        r = self.conn.execute(
            "SELECT id, source_path, sha256, page_count, status FROM documents "
            "WHERE source_path = ?", (source_path,)).fetchone()
        if not r:
            return None
        pages = self.conn.execute(
            "SELECT page_number, status, extraction_method, length(text) FROM pages "
            "WHERE document_id = ? ORDER BY page_number", (r[0],)).fetchall()
        return {"id": r[0], "source_path": r[1], "sha256": r[2], "page_count": r[3],
                "status": r[4],
                "pages": [{"page_number": p[0], "status": p[1], "method": p[2],
                           "chars": p[3]} for p in pages]}

    # -- reads --------------------------------------------------------------

    _HIT_COLS = ("c.id, c.document_id, d.source_path, c.page_number, c.char_start, c.char_end, c.text")

    def keyword_search(self, query: str, limit: int = 10) -> list[ChunkHit]:
        rows = self.conn.execute(f"""
            SELECT {self._HIT_COLS}, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c    ON c.id = chunks_fts.rowid
            JOIN documents d ON d.id = c.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY score LIMIT ?
        """, (query, limit)).fetchall()
        return [self._hit(r) for r in rows]

    def vector_search(self, embedding: Sequence[float], limit: int = 10) -> list[ChunkHit]:
        rows = self.conn.execute(f"""
            SELECT {self._HIT_COLS}, v.distance AS score
            FROM (SELECT rowid, distance FROM chunks_vec
                  WHERE embedding MATCH ? AND k = ?) v
            JOIN chunks c    ON c.id = v.rowid
            JOIN documents d ON d.id = c.document_id
            ORDER BY v.distance
        """, (_serialize_f32(embedding), limit)).fetchall()
        return [self._hit(r) for r in rows]

    @staticmethod
    def _hit(r) -> ChunkHit:
        return ChunkHit(r[0], r[1], r[2], r[3], r[4], r[5], r[6], float(r[7]))

    # -- diagnostics --------------------------------------------------------

    def info(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "encryption_mode": ENCRYPTION_MODE,
            "key_backend": self._key_provider.active_backend,
            "key_backend_detail": self._key_provider.backend_detail,
            "cipher_version": q("PRAGMA cipher_version"),
            "cipher_provider": q("PRAGMA cipher_provider"),
            "cipher_provider_version": q("PRAGMA cipher_provider_version"),
            "sqlite_version": q("SELECT sqlite_version()"),
            "vec_version": q("SELECT vec_version()"),
            "fts5": bool(q("SELECT sqlite_compileoption_used('ENABLE_FTS5')")),
        }

    def close(self) -> None:
        self.conn.close()
