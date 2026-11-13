#!/usr/bin/env python3
"""
Print a password hash for the [auth] section of the Streamlit secrets.

    python tools/make_password_hash.py alice

It asks for the password twice and prints the line to paste into the secrets.
Nothing is written to disk and the password never leaves the terminal.
"""
from __future__ import annotations

import getpass
import hashlib
import os
import sys

ITERATIONS = 260_000


def make_hash(password: str, iterations: int = ITERATIONS) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived.hex()}"


def main() -> int:
    username = sys.argv[1] if len(sys.argv) > 1 else input("Username: ").strip()
    if not username:
        print("A username is required.", file=sys.stderr)
        return 1

    password = getpass.getpass("Password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if password != getpass.getpass("Repeat: "):
        print("The two entries do not match.", file=sys.stderr)
        return 1

    print()
    print("Add this under [auth] in the Streamlit secrets:")
    print()
    print(f'{username} = "{make_hash(password)}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
