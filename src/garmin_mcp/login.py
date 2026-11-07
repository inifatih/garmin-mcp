"""One-time interactive Garmin Connect login (handles MFA) that saves reusable tokens."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .client import DEFAULT_TOKENSTORE


def main() -> None:
    parser = argparse.ArgumentParser(description="Log in to Garmin Connect and save tokens for garmin-mcp.")
    parser.add_argument("--tokenstore", default=os.getenv("GARMINTOKENS") or DEFAULT_TOKENSTORE,
                        help="Directory to save tokens (default ~/.garminconnect)")
    parser.add_argument("--print-tokens", action="store_true",
                        help="Also print the token JSON (for the GARMINTOKENS env var on a remote host)")
    args = parser.parse_args()

    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    store = str(Path(args.tokenstore).expanduser())
    Path(store).mkdir(parents=True, exist_ok=True, mode=0o700)

    g = Garmin(email=email, password=password, prompt_mfa=lambda: input("MFA code (from email/app): ").strip())
    try:
        g.login(store)
    except Exception as e:  # noqa: BLE001
        print(f"Login failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
    g.client.dump(store)
    print(f"Logged in as {g.get_full_name() or g.display_name}. Tokens saved to {store}")
    if args.print_tokens:
        print("\nGARMINTOKENS value (keep it secret – it grants access to your Garmin account):\n")
        print(g.client.dumps())


if __name__ == "__main__":
    main()
