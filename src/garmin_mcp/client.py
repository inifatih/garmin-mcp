"""Thin, cached wrapper around the `garminconnect` library."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

DEFAULT_TOKENSTORE = "~/.garminconnect"


class GarminAuthError(RuntimeError):
    pass


class GarminAPIError(RuntimeError):
    pass


def tokenstore_location() -> str:
    """GARMINTOKENS may be a directory/file path or an inline JSON token string."""
    return os.getenv("GARMINTOKENS") or str(Path(DEFAULT_TOKENSTORE).expanduser())


class GarminClient:
    """Lazily logs in using saved tokens (preferred) or GARMIN_EMAIL / GARMIN_PASSWORD.

    Responses are cached briefly so a single coaching question that touches the same
    endpoint several times doesn't hammer Garmin Connect.
    """

    def __init__(self, ttl_seconds: float | None = None) -> None:
        self._api: Any = None
        self._lock = threading.Lock()
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self.ttl = ttl_seconds if ttl_seconds is not None else float(os.getenv("GARMIN_MCP_CACHE_TTL", "120"))

    # ------------------------------------------------------------------ auth
    def api(self) -> Any:
        with self._lock:
            if self._api is None:
                self._api = self._login()
            return self._api

    def _login(self) -> Any:
        try:
            from garminconnect import Garmin
        except ImportError as e:  # pragma: no cover
            raise GarminAuthError("garminconnect is not installed") from e

        tokenstore = tokenstore_location()
        email = os.getenv("GARMIN_EMAIL")
        password = os.getenv("GARMIN_PASSWORD")
        g = Garmin(email=email, password=password)
        try:
            g.login(tokenstore)
        except Exception as e:  # noqa: BLE001 - surface any login failure clearly
            raise GarminAuthError(
                "Could not log in to Garmin Connect. Run `garmin-mcp-login` once on this machine "
                "(it handles MFA and saves tokens to ~/.garminconnect), or set GARMINTOKENS. "
                f"Underlying error: {type(e).__name__}: {e}"
            ) from e
        log.info("Logged in to Garmin Connect as %s", getattr(g, "display_name", "?"))
        return g

    # ----------------------------------------------------------------- calls
    def call(self, method: str, *args: Any, cache: bool = True, **kwargs: Any) -> Any:
        key = (method, args, tuple(sorted(kwargs.items())))
        now = time.monotonic()
        if cache and key in self._cache:
            ts, val = self._cache[key]
            if now - ts < self.ttl:
                return val
        fn: Callable[..., Any] = getattr(self.api(), method)
        try:
            val = fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "Authentication" in name:
                with self._lock:
                    self._api = None
                raise GarminAuthError(f"Garmin session expired or rejected ({e}). Run `garmin-mcp-login` again.") from e
            if "TooManyRequests" in name:
                raise GarminAPIError("Garmin Connect is rate-limiting requests; try again in a few minutes.") from e
            raise GarminAPIError(f"{method} failed: {name}: {e}") from e
        if cache:
            self._cache[key] = (now, val)
        return val

    def safe(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Like call() but returns None on API errors (auth errors still raise)."""
        try:
            return self.call(method, *args, **kwargs)
        except GarminAuthError:
            raise
        except GarminAPIError as e:
            log.warning("%s", e)
            return None

    def clear_cache(self) -> None:
        self._cache.clear()
