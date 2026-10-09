"""Key backend selection rules (see storage.py docstring). Runs on macOS and Linux."""
import os
import stat

import pytest

import storage
from storage import (KEY_BACKEND_FILE, KEY_FILE_NAME, KeyError_, KeyProvider,
                     Storage)

DIM = 8


@pytest.fixture
def no_keychain(monkeypatch):
    """Simulate a headless Linux box with no Secret Service."""
    monkeypatch.setattr(storage, "_keychain_backend_name", lambda: None)


def test_falls_back_to_0600_key_file_without_keychain(tmp_path, no_keychain):
    db = str(tmp_path / "app" / "data.db")
    os.makedirs(os.path.dirname(db))
    s = Storage(db, DIM, key_provider=KeyProvider())
    info = s.info()
    s.close()
    key_file = tmp_path / "app" / KEY_FILE_NAME
    assert info["key_backend"] == KEY_BACKEND_FILE
    assert info["key_backend_detail"] == str(key_file)
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(key_file.parent).st_mode) & 0o077 == 0
    # Reopen uses the same key
    s2 = Storage(db, DIM, key_provider=KeyProvider())
    assert s2.info()["key_backend"] == KEY_BACKEND_FILE
    s2.close()


def test_forced_file_backend_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDESK_KEY_BACKEND", "file")
    s = Storage(str(tmp_path / "d.db"), DIM, key_provider=KeyProvider())
    assert s.info()["key_backend"] == KEY_BACKEND_FILE
    s.close()


def test_refuses_key_file_with_loose_permissions(tmp_path, no_keychain):
    db = str(tmp_path / "d.db")
    Storage(db, DIM, key_provider=KeyProvider()).close()
    os.chmod(tmp_path / KEY_FILE_NAME, 0o644)
    with pytest.raises(KeyError_, match="must be 0600"):
        Storage(db, DIM, key_provider=KeyProvider())


def test_never_rekeys_existing_database(tmp_path, no_keychain):
    db = str(tmp_path / "d.db")
    Storage(db, DIM, key_provider=KeyProvider()).close()
    os.remove(tmp_path / KEY_FILE_NAME)                 # key lost
    with pytest.raises(KeyError_, match="Refusing to create a new key"):
        Storage(db, DIM, key_provider=KeyProvider())
    assert not (tmp_path / KEY_FILE_NAME).exists()      # did not silently create one


def test_broken_keychain_does_not_downgrade_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_keychain_backend_name", lambda: "fake.LockedKeyring")
    import keyring

    def boom(*a, **k):
        raise RuntimeError("collection is locked")
    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(KeyError_, match="Not falling back"):
        Storage(str(tmp_path / "d.db"), DIM, key_provider=KeyProvider())
    assert not (tmp_path / KEY_FILE_NAME).exists()


def test_forced_keychain_without_one_is_an_error(tmp_path, monkeypatch, no_keychain):
    monkeypatch.setenv("AIDESK_KEY_BACKEND", "keychain")
    with pytest.raises(KeyError_, match="no OS keychain"):
        Storage(str(tmp_path / "d.db"), DIM, key_provider=KeyProvider())
