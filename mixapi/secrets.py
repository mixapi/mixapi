from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class InvalidCredentialCiphertext(ValueError):
    pass


@dataclass(frozen=True)
class CredentialAAD:
    provider_id: str
    protocol: str
    field: str

    def encode(self) -> bytes:
        return json.dumps(
            {
                "field": self.field,
                "protocol": self.protocol,
                "provider_id": self.provider_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True)
class EncryptedCredential:
    key_version: int
    nonce: bytes
    ciphertext: bytes
    fingerprint: str

    def public_dict(self) -> dict[str, object]:
        return {
            "credential_configured": True,
            "credential_key_version": self.key_version,
            "credential_fingerprint": self.fingerprint,
        }


class CredentialCipher:
    def __init__(
        self,
        active_key: bytes,
        *,
        active_version: int,
        previous_keys: dict[int, bytes] | None = None,
    ) -> None:
        keys = dict(previous_keys or {})
        if active_version <= 0:
            raise ValueError("Active key version must be positive")
        if active_version in keys:
            raise ValueError("Active key version cannot also be previous")
        _validate_key(active_key)
        for key in keys.values():
            _validate_key(key)
        keys[active_version] = active_key
        self._keys = keys
        self._active_version = active_version

    def encrypt(self, credential: str, aad: CredentialAAD) -> EncryptedCredential:
        encoded = credential.encode("utf-8")
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[self._active_version]).encrypt(
            nonce,
            encoded,
            aad.encode(),
        )
        return EncryptedCredential(
            key_version=self._active_version,
            nonce=nonce,
            ciphertext=ciphertext,
            fingerprint=hashlib.sha256(encoded).hexdigest()[:16],
        )

    def decrypt(self, envelope: EncryptedCredential, aad: CredentialAAD) -> str:
        key = self._keys.get(envelope.key_version)
        if key is None:
            raise InvalidCredentialCiphertext(
                f"Unknown credential key version: {envelope.key_version}"
            )
        try:
            plaintext = AESGCM(key).decrypt(
                envelope.nonce,
                envelope.ciphertext,
                aad.encode(),
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise InvalidCredentialCiphertext("Credential ciphertext is invalid") from error


def _validate_key(key: bytes) -> None:
    if len(key) != 32:
        raise ValueError("Credential encryption keys must contain 32 bytes")
