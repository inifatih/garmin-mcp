import json
import sys
from pathlib import Path
from unittest import mock

import httpx
from cryptography.fernet import Fernet

from conftest import TOKENS_V1, FakeGist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import setup_tokens  # noqa: E402


def test_first_setup_prints_env_and_url(capsys, monkeypatch):
    holder = {}

    def fake_create(token, encrypted, http=None):
        holder["enc"] = encrypted
        return "g123"

    class FakeStore:
        def __init__(self, gid, tok, key, http=None):
            self.key = key

        def load(self):
            return Fernet(self.key.encode()).decrypt(holder["enc"].encode()).decode()

    monkeypatch.setenv("GIST_TOKEN", "ghp_test")
    with mock.patch.object(setup_tokens, "garmin_login", return_value=TOKENS_V1), \
         mock.patch.object(setup_tokens, "create_gist", fake_create), \
         mock.patch.object(setup_tokens, "GistTokenStore", FakeStore):
        setup_tokens.first_setup()
    out = capsys.readouterr().out
    for var in ("GARMIN_TOKEN_KEY=", "GIST_ID=g123", "GIST_TOKEN=ghp_test", "MCP_PATH_SECRET="):
        assert var in out
    secret = out.split("MCP_PATH_SECRET=")[1].split()[0]
    assert len(secret) >= 43
    assert f"https://<service>.onrender.com/{secret}/mcp" in out
    assert "refresh-1" not in holder["enc"]


def test_rotate_overwrites_gist(monkeypatch):
    key = Fernet.generate_key().decode()
    fg = FakeGist(key, Fernet(key.encode()).encrypt(b"old").decode())
    client = httpx.Client(transport=httpx.MockTransport(fg))
    real = setup_tokens.GistTokenStore
    monkeypatch.setenv("GIST_ID", "g123")
    monkeypatch.setenv("GIST_TOKEN", "ghp_test")
    monkeypatch.setenv("GARMIN_TOKEN_KEY", key)
    with mock.patch.object(setup_tokens, "garmin_login", return_value=TOKENS_V1), \
         mock.patch.object(setup_tokens, "GistTokenStore", lambda *a: real(*a, http=client)):
        setup_tokens.rotate()
    assert json.loads(fg.plaintext())["di_refresh_token"] == "refresh-1"
