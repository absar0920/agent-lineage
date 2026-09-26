"""Encryption at rest for connector credentials (AES-256-GCM).

Values of secret-looking fields (api keys, tokens, passwords) are stored as
`enc:v1:<base64(nonce|ciphertext)>`; non-secret settings (store URL, subdomain)
stay readable. Credentials are never returned by the API.
"""

import base64
import os
import re
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PREFIX = "enc:v1:"
SECRET_FIELD = re.compile(r"(key|secret|token|password)", re.IGNORECASE)


def derive_key(material: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"agent-lineage connector secrets").derive(
        material.encode()
    )


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, *, context: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode(), context.encode())
        return PREFIX + base64.b64encode(nonce + ct).decode()

    def decrypt(self, token: str, *, context: str) -> str:
        raw = base64.b64decode(token.removeprefix(PREFIX))
        return self._aead.decrypt(raw[:12], raw[12:], context.encode()).decode()

    def seal_settings(self, connector: str, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            k: self.encrypt(v, context=f"{connector}:{k}") if SECRET_FIELD.search(k) and isinstance(v, str) else v
            for k, v in settings.items()
        }
