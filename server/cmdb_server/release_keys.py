"""One-time offline setup. Never prints the private signing key."""
import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .release_manifest import key_id


def generate(directory):
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes_raw()
    files = {"signing-key.txt": base64.b64encode(key.private_bytes_raw()).decode(),
             "public-keys.json": json.dumps({key_id(public): base64.b64encode(public).decode()})}
    for name, value in files.items():
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Utworz nowy katalog kluczy publikacji; nie nadpisuje plikow")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    generate(args.directory)
    print("Utworzono signing-key.txt (sekret CI) i public-keys.json (konfiguracja serwera).")
    print("Zabezpiecz klucz prywatny; nie dodawaj go do repozytorium ani konfiguracji CMDB.")
