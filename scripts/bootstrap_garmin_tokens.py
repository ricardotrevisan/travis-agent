#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path

# Allow running this script directly from repo root without needing PYTHONPATH tweaks.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.garmin_tracking import bootstrap_token_login


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap Garmin tokens for Travis Agent.")
    parser.add_argument(
        "--token-dir",
        default=os.getenv("GARMINTOKENS_HOST_PATH") or os.getenv("GARMINTOKENS") or "~/.garminconnect",
        help="Token output directory (host path).",
    )
    parser.add_argument("--email", default=os.getenv("GARMIN_USER") or "", help="Garmin account email.")
    parser.add_argument("--password", default=os.getenv("GARMIN_PASS") or "", help="Garmin account password.")
    parser.add_argument("--mfa", default="", help="MFA code (if required).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    email = args.email.strip() or input("Garmin email: ").strip()
    password = args.password or getpass("Garmin password: ")
    mfa_code = args.mfa.strip() or None

    if not email or not password:
        raise SystemExit("Missing Garmin credentials.")

    token_dir = bootstrap_token_login(
        token_dir=args.token_dir,
        email=email,
        password=password,
        mfa_code=mfa_code,
    )
    print(f"OK: Garmin tokens saved to {token_dir}")


if __name__ == "__main__":
    main()
