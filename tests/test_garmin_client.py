import json
import logging

import httpx
import pytest
from cryptography.fernet import Fernet

from conftest import TOKENS_V1, FakeGarmin, FakeGist, MemoryStore
from garmin_client import (
    EnvTokenStore,
    GarminAuthError,
    GarminCallError,
    GarminSession,
    GistTokenStore,
    create_gist,
    store_from_env,
)


# ----------------------------------------------------------------- gist store

def test_gist_load_decrypts(key, gist_http):
    store = GistTokenStore("g123", "ghp_test", key, http=gist_http)
    assert store.load() == TOKENS_V1


def test_gist_save_patches_encrypted_only(key, fake_gist, gist_http):
    store = GistTokenStore("g123", "ghp_test", key, http=gist_http)
    new = TOKENS_V1.replace("refresh-1", "refresh-2")
    store.save(new)
    patch = fake_gist.requests[-1]
    assert patch.method == "PATCH"
    assert b"refresh-2" not in patch.content and b"access-1" not in patch.content
    assert fake_gist.plaintext() == new


def test_gist_wrong_key(fake_gist, gist_http):
    store = GistTokenStore("g123", "ghp_test", Fernet.generate_key().decode(), http=gist_http)
    with pytest.raises(GarminAuthError, match="GARMIN_TOKEN_KEY"):
        store.load()


def test_gist_bad_pat(key, gist_http):
    store = GistTokenStore("g123", "ghp_wrong", key, http=gist_http)
    with pytest.raises(GarminAuthError, match="HTTP 401"):
        store.load()


def test_create_gist_is_secret(key):
    fg = FakeGist(key)
    gid = create_gist("ghp_test", "ENCRYPTED", http=httpx.Client(transport=httpx.MockTransport(fg)))
    assert gid == "g123"
    assert json.loads(fg.requests[0].content)["public"] is False


def test_store_from_env_selection(key):
    s = store_from_env({"GIST_ID": "g", "GIST_TOKEN": "t", "GARMIN_TOKEN_KEY": key})
    assert isinstance(s, GistTokenStore)
    assert isinstance(store_from_env({"GARMINTOKENS": TOKENS_V1}), EnvTokenStore)
    with pytest.raises(GarminAuthError):
        store_from_env({})


# ------------------------------------------------------------------- session

def test_lazy_single_login():
    FakeGarmin.instances = 0
    store = MemoryStore()
    s = GarminSession(lambda: store, FakeGarmin)
    assert FakeGarmin.instances == 0
    s.call(lambda g: g.get_user_summary("2026-11-07"))
    s.call(lambda g: g.get_user_summary("2026-11-07"))
    assert FakeGarmin.instances == 1
    assert store.saved == []  # tokens unchanged → no write


def test_rotation_is_persisted_once_per_change():
    store = MemoryStore()
    s = GarminSession(lambda: store, lambda: FakeGarmin(rotate_on_call=True))
    s.call(lambda g: g.get_user_summary("2026-11-07"))
    assert len(store.saved) == 1 and "refresh-2" in store.saved[0]
    s.call(lambda g: g.get_user_summary("2026-11-07"))
    assert len(store.saved) == 2 and "refresh-3" in store.saved[1]


def test_rotation_patches_gist_end_to_end(key, fake_gist, gist_http):
    store = GistTokenStore("g123", "ghp_test", key, http=gist_http)
    s = GarminSession(lambda: store, lambda: FakeGarmin(rotate_on_call=True))
    s.call(lambda g: g.get_race_predictions())
    methods = [r.method for r in fake_gist.requests]
    assert methods == ["GET", "PATCH"]
    assert json.loads(fake_gist.plaintext())["di_refresh_token"] == "refresh-2"


def test_auth_failure_gives_rotate_hint_and_resets():
    store = MemoryStore()
    s = GarminSession(lambda: store, lambda: FakeGarmin(auth_fail=True))
    with pytest.raises(GarminAuthError, match="setup_tokens.py --rotate"):
        s.call(lambda g: g.get_user_summary("2026-11-07"))
    assert s._garmin is None


def test_login_failure_gives_rotate_hint():
    class BadGarmin(FakeGarmin):
        def login(self, tokenstore):
            raise Exception("GarminConnectAuthenticationError: refresh failed")

    s = GarminSession(lambda: MemoryStore(), BadGarmin)
    with pytest.raises(GarminAuthError, match="--rotate"):
        s.call(lambda g: None)


def test_other_errors_are_wrapped():
    def boom(g):
        raise ValueError("bad thing")

    s = GarminSession(lambda: MemoryStore(), FakeGarmin)
    with pytest.raises(GarminCallError, match="bad thing"):
        s.call(boom)


def test_save_failure_is_best_effort_and_secret_free(caplog):
    store = MemoryStore(fail_save=True)
    s = GarminSession(lambda: store, lambda: FakeGarmin(rotate_on_call=True))
    with caplog.at_level(logging.DEBUG):
        assert s.call(lambda g: g.get_user_summary("2026-11-07"))["totalSteps"] == 12034
    text = caplog.text
    assert "Saving rotated tokens failed" in text
    assert "refresh-" not in text and "access-" not in text
