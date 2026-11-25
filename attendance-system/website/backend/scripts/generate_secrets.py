"""Write missing application secrets directly to website/.env without printing them."""

import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values, set_key


ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
GENERATORS = {
    "SECRET_KEY": lambda: secrets.token_urlsafe(48),
    "ENCRYPTION_KEY": lambda: Fernet.generate_key().decode("ascii"),
    "DEVICE_API_KEY_PEPPER": lambda: secrets.token_urlsafe(48),
}


def main():
    if not ENV_FILE.exists():
        print("website/.env does not exist. Copy .env.example to .env first.", file=sys.stderr)
        return 1
    current = dotenv_values(ENV_FILE)
    written = []
    for key, generate in GENERATORS.items():
        if not (current.get(key) or "").strip():
            set_key(ENV_FILE, key, generate(), quote_mode="always")
            written.append(key)
    if written:
        print(f"Generated and wrote {', '.join(written)} to website/.env. Secret values were not displayed.")
        print("Back up ENCRYPTION_KEY securely. Do not regenerate it after face data has been encrypted.")
    else:
        print("All application secrets already exist in website/.env; no values were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
