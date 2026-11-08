"""Creates the secret files read by docker-compose.endterm.yml (never committed).

    python -m lab.init_secrets          # keeps files that already exist
    python -m lab.init_secrets --rotate # writes a new JWT secret, keeping the old one as a
                                        # verification-only second line

Needs only the standard library.
"""

import argparse
import secrets
from pathlib import Path

SECRETS = Path(__file__).resolve().parent.parent / "secrets"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    SECRETS.mkdir(exist_ok=True)
    jwt_file = SECRETS / "jwt_secret"
    new = secrets.token_urlsafe(48)
    if args.rotate and jwt_file.exists():
        old = jwt_file.read_text(encoding="utf-8").splitlines()[0]
        jwt_file.write_text(f"{new}\n{old}\n", encoding="utf-8", newline="\n")
        print("JWT secret rotated; the previous one still verifies tokens")
    elif not jwt_file.exists():
        jwt_file.write_text(new + "\n", encoding="utf-8", newline="\n")
        print("created secrets/jwt_secret")
    bank_file = SECRETS / "bank_api_key"
    if not bank_file.exists():
        bank_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8", newline="\n")
        print("created secrets/bank_api_key")


if __name__ == "__main__":
    main()
