from __future__ import annotations

from dataclasses import replace

import pytest

from mixapi.secrets import (
    CredentialAAD,
    CredentialCipher,
    InvalidCredentialCiphertext,
)


ACTIVE_KEY = bytes.fromhex("11" * 32)
OLD_KEY = bytes.fromhex("22" * 32)
AAD = CredentialAAD(
    provider_id="provider_1",
    protocol="gemini",
    field="api_key",
)


def test_credentials_round_trip_with_random_nonces_and_redacted_public_metadata() -> None:
    cipher = CredentialCipher(ACTIVE_KEY, active_version=2, previous_keys={1: OLD_KEY})

    first = cipher.encrypt("provider-secret", AAD)
    second = cipher.encrypt("provider-secret", AAD)

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert cipher.decrypt(first, AAD) == "provider-secret"
    assert first.public_dict() == {
        "credential_configured": True,
        "credential_key_version": 2,
        "credential_fingerprint": first.fingerprint,
    }
    assert "provider-secret" not in repr(first)
    assert "ciphertext" not in first.public_dict()


def test_credentials_are_bound_to_provider_protocol_and_field() -> None:
    cipher = CredentialCipher(ACTIVE_KEY, active_version=2)
    encrypted = cipher.encrypt("provider-secret", AAD)

    for changed_aad in (
        replace(AAD, provider_id="provider_2"),
        replace(AAD, protocol="anthropic"),
        replace(AAD, field="access_token"),
    ):
        with pytest.raises(InvalidCredentialCiphertext):
            cipher.decrypt(encrypted, changed_aad)


def test_credentials_reject_unknown_or_wrong_keys() -> None:
    encrypted = CredentialCipher(ACTIVE_KEY, active_version=2).encrypt("secret", AAD)

    with pytest.raises(InvalidCredentialCiphertext, match="key version"):
        CredentialCipher(OLD_KEY, active_version=1).decrypt(encrypted, AAD)

    with pytest.raises(InvalidCredentialCiphertext):
        CredentialCipher(bytes.fromhex("33" * 32), active_version=2).decrypt(encrypted, AAD)


def test_previous_key_versions_remain_decryptable_during_rotation() -> None:
    old_envelope = CredentialCipher(OLD_KEY, active_version=1).encrypt("old-secret", AAD)
    rotated = CredentialCipher(ACTIVE_KEY, active_version=2, previous_keys={1: OLD_KEY})

    assert rotated.decrypt(old_envelope, AAD) == "old-secret"
