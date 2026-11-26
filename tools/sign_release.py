#!/usr/bin/env python3
"""Release signing: an Ed25519 key pair, kept off GitHub, signs each release's SHA256SUMS.txt.

    sign_release.py keygen [--key PATH]            make the key pair (asks for a password)
    sign_release.py public [--key PATH]            print the public key again
    sign_release.py sign SHA256SUMS.txt [--key PATH]    write SHA256SUMS.txt.sig
    sign_release.py verify SHA256SUMS.txt PUBLIC_KEY    check SHA256SUMS.txt.sig

The private key is a password-encrypted PEM file, by default
~/.config/jobalerts-release/release-signing.key (mode 0600). Never put it in the repository or in
GitHub secrets. The public key (base64 of the 32 raw bytes) goes into updater.py.
The signature file holds the base64 Ed25519 signature of the file's exact bytes.
Needs the `cryptography` package.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import os
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

DEFAULT_KEY = Path.home() / ".config" / "jobalerts-release" / "release-signing.key"
MIN_PASSWORD = 12


def public_text(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def fingerprint(public_b64: str) -> str:
    """A short check value to compare the key in two places by eye."""
    return hashlib.sha256(base64.b64decode(public_b64)).hexdigest()[:16]


def load_private(path: Path) -> Ed25519PrivateKey:
    password = getpass.getpass(f"Password for {path}: ").encode()
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=password)
    except ValueError:
        raise SystemExit("Wrong password, or not a key file.") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} isn't an Ed25519 key")
    return key


def keygen(path: Path) -> int:
    if path.exists():
        raise SystemExit(f"{path} exists; not replacing it. A new key means installed apps reject new releases.")
    password = getpass.getpass("New password for the signing key: ")
    if len(password) < MIN_PASSWORD:
        raise SystemExit(f"Use at least {MIN_PASSWORD} characters.")
    if getpass.getpass("The same password again: ") != password:
        raise SystemExit("The two passwords don't match.")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.BestAvailableEncryption(password.encode()))
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    public = public_text(key.public_key())
    print(f"Private key: {path} (password-encrypted, mode 0600)")
    print(f"Public key:  {public}")
    print(f"Fingerprint: {fingerprint(public)}")
    return 0


def show_public(path: Path) -> int:
    public = public_text(load_private(path).public_key())
    print(f"Public key:  {public}")
    print(f"Fingerprint: {fingerprint(public)}")
    return 0


def sign(target: Path, path: Path) -> int:
    key = load_private(path)
    data = target.read_bytes()
    signature = base64.b64encode(key.sign(data)).decode()
    out = target.with_name(target.name + ".sig")
    out.write_text(signature + "\n")
    key.public_key().verify(base64.b64decode(signature), data)
    print(f"Wrote {out} (key {fingerprint(public_text(key.public_key()))})")
    return 0


def verify(target: Path, public_b64: str) -> int:
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
    signature = base64.b64decode(target.with_name(target.name + ".sig").read_text().strip())
    try:
        public.verify(signature, target.read_bytes())
    except InvalidSignature:
        print(f"BAD signature on {target}")
        return 1
    print(f"Good signature on {target} (key {fingerprint(public_b64)})")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sign_release.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("keygen", "public"):
        p = sub.add_parser(name)
        p.add_argument("--key", type=Path, default=DEFAULT_KEY)
    p = sub.add_parser("sign")
    p.add_argument("file", type=Path)
    p.add_argument("--key", type=Path, default=DEFAULT_KEY)
    p = sub.add_parser("verify")
    p.add_argument("file", type=Path)
    p.add_argument("public_key")
    args = parser.parse_args(argv)
    if args.cmd == "keygen":
        return keygen(args.key.expanduser())
    if args.cmd == "public":
        return show_public(args.key.expanduser())
    if args.cmd == "sign":
        return sign(args.file, args.key.expanduser())
    return verify(args.file, args.public_key)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
