import hashlib
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from mixapi.control_plane import ApiKeyRecord, generate_api_key, hash_api_key


class ControlPlaneDomainTest(unittest.TestCase):
    def test_generated_keys_are_prefixed_and_hash_deterministically(self) -> None:
        secret = generate_api_key()

        self.assertTrue(secret.startswith("mxapi_"))
        self.assertEqual(
            hash_api_key(secret),
            hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        )

    def test_api_key_public_record_is_redacted_and_currency_is_stable(self) -> None:
        now = datetime.now(timezone.utc)
        record = ApiKeyRecord(
            id="key_1",
            tenant_id="tenant_acme",
            project_id="project_chat",
            name="chat service",
            key_hash="secret-hash",
            key_prefix="mxapi_prefix",
            scopes=("models:read",),
            model_allowlist=("gpt-5.5",),
            budget_limit_usd=Decimal("2.50000000"),
            expires_at=None,
            status="active",
            created_at=now,
            updated_at=now,
        )

        public = record.public_dict()

        self.assertNotIn("key_hash", public)
        self.assertNotIn("secret", public)
        self.assertEqual(public["budget_limit_usd"], "2.50")


if __name__ == "__main__":
    unittest.main()
