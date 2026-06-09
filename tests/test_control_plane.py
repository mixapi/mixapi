import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from mixapi.control_plane import InMemoryControlPlaneStore


class InMemoryControlPlaneStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryControlPlaneStore()

    def test_created_key_is_returned_once_and_stored_as_hash(self) -> None:
        created = self.store.create_api_key(
            tenant_id="tenant_acme",
            project_id="project_chat",
            name="chat service",
            scopes=("models:read", "responses:create"),
            model_allowlist=("mixapi/balanced-chat",),
            budget_limit_usd=Decimal("1.25"),
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            actor_id="admin",
        )

        self.assertTrue(created.secret.startswith("mxapi_"))
        self.assertEqual(
            created.record.key_hash,
            hashlib.sha256(created.secret.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(self.store.resolve_api_key(created.secret), created.record)
        self.assertIsNone(self.store.resolve_api_key("mxapi_wrong"))

        public = created.record.public_dict()
        self.assertNotIn("key_hash", public)
        self.assertNotIn("secret", public)
        self.assertEqual(public["key_prefix"], created.secret[:12])
        self.assertEqual(public["budget_limit_usd"], "1.25")

    def test_key_can_be_updated_and_revoked(self) -> None:
        created = self.store.create_api_key(
            tenant_id="tenant_acme",
            project_id="project_chat",
            name=None,
            scopes=("models:read",),
            model_allowlist=(),
            budget_limit_usd=None,
            expires_at=None,
            actor_id="admin",
        )

        updated = self.store.update_api_key(
            created.record.id,
            {
                "name": "renamed",
                "scopes": ("models:read", "embeddings:create"),
                "model_allowlist": ("mixapi/embedding-small",),
                "budget_limit_usd": Decimal("2.00"),
            },
            actor_id="admin",
        )

        self.assertEqual(updated.name, "renamed")
        self.assertEqual(updated.scopes, ("models:read", "embeddings:create"))
        self.assertEqual(updated.model_allowlist, ("mixapi/embedding-small",))
        self.assertEqual(updated.budget_limit_usd, Decimal("2.00"))
        self.assertEqual(self.store.resolve_api_key(created.secret), updated)

        revoked = self.store.revoke_api_key(created.record.id, actor_id="admin")

        self.assertEqual(revoked.status, "revoked")
        self.assertIsNone(self.store.resolve_api_key(created.secret))

    def test_tenant_policy_updates_are_merged(self) -> None:
        first = self.store.set_tenant_model_allowlist(
            "tenant_acme",
            ("mixapi/balanced-chat",),
            actor_id="admin",
        )
        second = self.store.set_tenant_routing_policy(
            "tenant_acme",
            "lowest-cost",
            actor_id="admin",
        )

        self.assertEqual(first.model_allowlist, ("mixapi/balanced-chat",))
        self.assertIsNone(first.routing_objective)
        self.assertEqual(second.model_allowlist, ("mixapi/balanced-chat",))
        self.assertEqual(second.routing_objective, "lowest-cost")
        self.assertEqual(self.store.get_tenant_policy("tenant_acme"), second)

    def test_audit_events_are_append_only_and_secret_free(self) -> None:
        created = self.store.create_api_key(
            tenant_id="tenant_acme",
            project_id="project_chat",
            name="chat service",
            scopes=("models:read",),
            model_allowlist=(),
            budget_limit_usd=None,
            expires_at=None,
            actor_id="admin",
        )
        self.store.revoke_api_key(created.record.id, actor_id="admin")

        events = self.store.list_audit_events(tenant_id="tenant_acme")

        self.assertEqual([event.action for event in events], ["api_key.created", "api_key.revoked"])
        serialized = json.dumps([event.public_dict() for event in events])
        self.assertNotIn(created.secret, serialized)
        self.assertNotIn(created.record.key_hash, serialized)
        self.assertNotIn("key_hash", serialized)


if __name__ == "__main__":
    unittest.main()
