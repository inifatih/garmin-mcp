"""One-time setup (run on your own computer, not on Render).

    python scripts/setup_tokens.py           # first setup: login, create encrypted secret gist, print env vars
    python scripts/setup_tokens.py --rotate  # tokens died: login again and overwrite the existing gist

Your Garmin email/password are only used for this login and are never saved anywhere.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet  # noqa: E402

from garmin_client import GistTokenStore, create_gist  # noqa: E402


def ask(prompt: str, secret: bool = False, env: str | None = None) -> str:
    if env and os.environ.get(env):
        return os.environ[env].strip()
    while True:
        v = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if v:
            return v


def garmin_login() -> str:
    from garminconnect import Garmin

    print("\n== Garmin Connect login ==")
    email = ask("Garmin email: ")
    password = ask("Garmin password (hidden): ", secret=True)
    g = Garmin(email=email, password=password,
               prompt_mfa=lambda: ask("MFA code from Garmin (email/app): "))
    try:
        g.login()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Garmin login failed: {type(e).__name__}: {e}")
    finally:
        del password
    tokens = g.client.dumps()
    try:
        name = g.get_full_name() or g.display_name
    except Exception:  # noqa: BLE001
        name = g.display_name
    print(f"Logged in as {name}.")
    return tokens


def first_setup() -> None:
    tokens = garmin_login()
    key = Fernet.generate_key().decode()
    path_secret = secrets.token_urlsafe(32)

    print("\n== GitHub gist ==")
    print("Create a *classic* personal access token with ONLY the `gist` scope:")
    print("  https://github.com/settings/tokens/new?scopes=gist&description=garmin-mcp")
    gist_token = ask("Paste the token (hidden): ", secret=True, env="GIST_TOKEN")
    enc = Fernet(key.encode()).encrypt(tokens.encode()).decode()
    try:
        gist_id = create_gist(gist_token, enc)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not create the gist: {e}")

    # Read it back to prove the round trip works
    if GistTokenStore(gist_id, gist_token, key).load() != tokens:
        sys.exit("Gist round-trip check failed.")
    print(f"Secret gist created and verified (id {gist_id}).")

    print("\n" + "=" * 70)
    print("Paste these 4 environment variables into Render (Environment tab):\n")
    print(f"GARMIN_TOKEN_KEY={key}")
    print(f"GIST_ID={gist_id}")
    print(f"GIST_TOKEN={gist_token}")
    print(f"MCP_PATH_SECRET={path_secret}")
    print("\nclaude.ai connector URL (replace <service> with your Render service name):\n")
    print(f"https://<service>.onrender.com/{path_secret}/mcp")
    print("=" * 70)
    print("Keep these private: the URL alone grants read access to your Garmin data.")


def rotate() -> None:
    print("Rotate: log in again and overwrite the tokens in your existing gist.")
    print("You need the GIST_ID, GIST_TOKEN and GARMIN_TOKEN_KEY values from Render.\n")
    gist_id = ask("GIST_ID: ", env="GIST_ID")
    gist_token = ask("GIST_TOKEN (hidden): ", secret=True, env="GIST_TOKEN")
    key = ask("GARMIN_TOKEN_KEY (hidden): ", secret=True, env="GARMIN_TOKEN_KEY")
    try:
        Fernet(key.encode())
    except Exception:  # noqa: BLE001
        sys.exit("GARMIN_TOKEN_KEY is not a valid Fernet key.")
    tokens = garmin_login()
    store = GistTokenStore(gist_id, gist_token, key)
    try:
        store.save(tokens)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Could not update the gist: {e}")
    if store.load() != tokens:
        sys.exit("Gist round-trip check failed.")
    print("\nGist updated. Restart the Render service (Manual Deploy → Restart, or just wait for it to")
    print("sleep and wake) so it loads the new tokens. No env vars need to change.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rotate", action="store_true", help="re-login and overwrite the existing gist")
    args = p.parse_args()
    rotate() if args.rotate else first_setup()


if __name__ == "__main__":
    main()
