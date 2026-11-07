"""Garmin Connect session with encrypted token persistence in a secret GitHub gist.

Flow:
  first tool call -> fetch gist -> Fernet-decrypt -> Garmin().login(<token json>)
  after each call -> if client.dumps() changed (refresh rotated tokens) -> encrypt -> PATCH gist

Nothing in this module logs tokens, keys, or credentials.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, Protocol, TypeVar

import httpx
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("garmin_mcp.client")

GIST_FILENAME = "garmin_tokens.enc"
GITHUB_API = "https://api.github.com"
ROTATE_HINT = (
    "Run `python scripts/setup_tokens.py --rotate` on your laptop to log in again and refresh "
    "the tokens stored in the gist, then retry."
)

T = TypeVar("T")


class GarminAuthError(RuntimeError):
    """Login/refresh failed – the user must re-run setup_tokens.py --rotate."""


class GarminCallError(RuntimeError):
    """A Garmin API call failed for a non-auth reason."""


class TokenStore(Protocol):
    def load(self) -> str: ...

    def save(self, tokens_json: str) -> None: ...


# --------------------------------------------------------------------------- #
# Gist store
# --------------------------------------------------------------------------- #

def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "garmin-mcp",
    }


class GistTokenStore:
    """Fernet-encrypted token blob stored as one file in a secret gist."""

    def __init__(self, gist_id: str, gist_token: str, key: str,
                 http: httpx.Client | None = None) -> None:
        self.gist_id = gist_id
        self._token = gist_token
        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        self._http = http or httpx.Client(timeout=20)

    def load(self) -> str:
        r = self._http.get(f"{GITHUB_API}/gists/{self.gist_id}", headers=_gh_headers(self._token))
        if r.status_code != 200:
            raise GarminAuthError(f"Could not read the token gist (GitHub HTTP {r.status_code}). "
                                  "Check GIST_ID and GIST_TOKEN (classic PAT with the `gist` scope).")
        f = (r.json().get("files") or {}).get(GIST_FILENAME)
        if not f:
            raise GarminAuthError(f"Gist has no `{GIST_FILENAME}` file. " + ROTATE_HINT)
        content = f.get("content") or ""
        if f.get("truncated") and f.get("raw_url"):
            content = self._http.get(f["raw_url"], headers=_gh_headers(self._token)).text
        try:
            return self._fernet.decrypt(content.strip().encode()).decode()
        except InvalidToken as e:
            raise GarminAuthError("Could not decrypt the token gist – GARMIN_TOKEN_KEY doesn't match. "
                                  + ROTATE_HINT) from e

    def save(self, tokens_json: str) -> None:
        enc = self._fernet.encrypt(tokens_json.encode()).decode()
        r = self._http.patch(
            f"{GITHUB_API}/gists/{self.gist_id}",
            headers=_gh_headers(self._token),
            json={"files": {GIST_FILENAME: {"content": enc}}},
        )
        if r.status_code != 200:
            raise GarminCallError(f"gist update failed (GitHub HTTP {r.status_code})")


def create_gist(gist_token: str, encrypted: str, http: httpx.Client | None = None) -> str:
    """Create a secret gist holding the encrypted tokens; returns the gist id."""
    http = http or httpx.Client(timeout=20)
    r = http.post(
        f"{GITHUB_API}/gists",
        headers=_gh_headers(gist_token),
        json={
            "description": "garmin-mcp tokens (Fernet-encrypted)",
            "public": False,
            "files": {GIST_FILENAME: {"content": encrypted}},
        },
    )
    if r.status_code != 201:
        raise RuntimeError(f"Creating the gist failed: GitHub HTTP {r.status_code}: {r.text[:200]}")
    return r.json()["id"]


# --------------------------------------------------------------------------- #
# Env store (local dev only)
# --------------------------------------------------------------------------- #

class EnvTokenStore:
    """Plaintext token JSON from GARMINTOKENS. Rotations are kept in memory only."""

    def __init__(self, value: str) -> None:
        self._value = value

    def load(self) -> str:
        return self._value

    def save(self, tokens_json: str) -> None:
        self._value = tokens_json
        log.info("Tokens rotated; GARMINTOKENS mode does not persist them (local dev only).")


def store_from_env(env: dict[str, str] | None = None) -> TokenStore:
    env = dict(os.environ) if env is None else env
    gist_id, gist_token, key = env.get("GIST_ID"), env.get("GIST_TOKEN"), env.get("GARMIN_TOKEN_KEY")
    if gist_id and gist_token and key:
        return GistTokenStore(gist_id, gist_token, key)
    if env.get("GARMINTOKENS"):
        return EnvTokenStore(env["GARMINTOKENS"])
    raise GarminAuthError(
        "No token storage configured: set GIST_ID, GIST_TOKEN and GARMIN_TOKEN_KEY "
        "(or GARMINTOKENS for local dev). " + ROTATE_HINT
    )


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #

def _is_auth_error(e: BaseException) -> bool:
    name = type(e).__name__
    text = str(e)
    return "Authentication" in name or " 401" in text or "401 " in text or "Unauthorized" in text


def _default_factory() -> Any:
    from garminconnect import Garmin

    return Garmin()  # no email/password: tokens only


class GarminSession:
    """Lazy, lock-guarded singleton Garmin client that persists rotated tokens."""

    def __init__(self, store_factory: Callable[[], TokenStore] = store_from_env,
                 garmin_factory: Callable[[], Any] = _default_factory) -> None:
        self._store_factory = store_factory
        self._garmin_factory = garmin_factory
        self._store: TokenStore | None = None
        self._garmin: Any = None
        self._last_saved: str | None = None
        self._lock = threading.RLock()

    def _login(self) -> Any:
        self._store = self._store or self._store_factory()
        tokens = self._store.load()
        g = self._garmin_factory()
        try:
            g.login(tokens)
        except Exception as e:  # noqa: BLE001
            raise GarminAuthError(f"Garmin rejected the stored tokens ({type(e).__name__}). " + ROTATE_HINT) from e
        self._last_saved = tokens
        self._garmin = g
        log.info("Garmin session ready")
        # login() may already have refreshed the access token
        self._persist_if_changed()
        return g

    def _dumps(self) -> str | None:
        client = getattr(self._garmin, "client", None)
        if client is None:
            return None
        lock = getattr(client, "_token_lock", None)
        if lock is not None:
            with lock:
                return client.dumps()
        return client.dumps()

    def _persist_if_changed(self) -> None:
        if self._garmin is None or self._store is None:
            return
        try:
            current = self._dumps()
        except Exception:  # noqa: BLE001
            log.warning("Could not serialize Garmin tokens")
            return
        if not current or current == self._last_saved:
            return
        try:
            self._store.save(current)
            self._last_saved = current
            log.info("Rotated Garmin tokens saved to token store")
        except Exception as e:  # noqa: BLE001 - best effort; never log secrets
            log.warning("Saving rotated tokens failed: %s", type(e).__name__)

    def call(self, fn: Callable[[Any], T]) -> T:
        """Run fn(garmin) under the lock; persist tokens afterwards (even on failure)."""
        with self._lock:
            if self._garmin is None:
                self._login()
            try:
                return fn(self._garmin)
            except Exception as e:  # noqa: BLE001
                if _is_auth_error(e):
                    self._persist_if_changed()
                    self._garmin = None
                    raise GarminAuthError("Garmin authentication failed – the stored tokens have expired or "
                                          "were revoked. " + ROTATE_HINT) from e
                if "TooManyRequests" in type(e).__name__:
                    raise GarminCallError("Garmin is rate-limiting requests; try again in a few minutes.") from e
                raise GarminCallError(f"Garmin request failed: {type(e).__name__}: {e}") from e
            finally:
                self._persist_if_changed()
