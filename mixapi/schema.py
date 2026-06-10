from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


metadata = MetaData()


provider_connections = Table(
    "provider_connections",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False, unique=True),
    Column("protocol", String(32), nullable=False),
    Column("base_url", Text, nullable=False),
    Column("credential_ciphertext", LargeBinary, nullable=False),
    Column("credential_nonce", LargeBinary, nullable=False),
    Column("credential_key_version", Integer, nullable=False),
    Column("credential_fingerprint", String(16), nullable=False),
    Column("timeout_seconds", Numeric(8, 3), nullable=False, server_default="30"),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("weight", Integer, nullable=False, server_default="1"),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint(
        "protocol IN ('openai-compatible', 'anthropic', 'gemini', 'ollama')",
        name="ck_provider_connections_protocol",
    ),
    CheckConstraint(
        "status IN ('active', 'disabled', 'deleted')",
        name="ck_provider_connections_status",
    ),
    CheckConstraint("timeout_seconds > 0", name="ck_provider_connections_timeout"),
    CheckConstraint("weight > 0", name="ck_provider_connections_weight"),
)


logical_models = Table(
    "logical_models",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("description", Text, nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("deleted_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('active', 'disabled', 'deleted')",
        name="ck_logical_models_status",
    ),
)


logical_model_aliases = Table(
    "logical_model_aliases",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("alias", String(200), nullable=False),
    Column(
        "logical_model_id",
        String(200),
        ForeignKey("logical_models.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("alias", name="uq_logical_model_aliases_alias"),
)


model_candidates = Table(
    "model_candidates",
    metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "logical_model_id",
        String(200),
        ForeignKey("logical_models.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "provider_connection_id",
        String(64),
        ForeignKey("provider_connections.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("upstream_model_id", String(300), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("priority", Integer, nullable=False, server_default="100"),
    Column("weight", Integer, nullable=False, server_default="1"),
    Column("context_window_tokens", Integer, nullable=False),
    Column("max_output_tokens", Integer, nullable=False),
    Column("input_modalities", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("output_modalities", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("tool_modes", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("schema_support", String(32), nullable=False, server_default="none"),
    Column("streaming_support", Boolean, nullable=False, server_default=text("false")),
    Column("embeddings_support", Boolean, nullable=False, server_default=text("false")),
    Column("retention_class", String(64), nullable=False, server_default="standard"),
    Column("regions", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("pricing", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("native_features", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("unsupported_parameters", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("deleted_at", DateTime(timezone=True)),
    UniqueConstraint(
        "logical_model_id",
        "provider_connection_id",
        "upstream_model_id",
        name="uq_model_candidates_mapping",
    ),
    CheckConstraint(
        "status IN ('active', 'disabled', 'deleted')",
        name="ck_model_candidates_status",
    ),
    CheckConstraint("weight > 0", name="ck_model_candidates_weight"),
    CheckConstraint("context_window_tokens > 0", name="ck_model_candidates_context"),
    CheckConstraint("max_output_tokens >= 0", name="ck_model_candidates_output"),
    CheckConstraint(
        "schema_support IN ('none', 'json_mode', 'best_effort_schema', 'strict_json_schema')",
        name="ck_model_candidates_schema_support",
    ),
)


configuration_versions = Table(
    "configuration_versions",
    metadata,
    Column("version", BigInteger, primary_key=True, autoincrement=True),
    Column("status", String(16), nullable=False, server_default="pending"),
    Column("checksum", String(64)),
    Column("error_code", String(100)),
    Column("error_message", String(500)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("published_at", DateTime(timezone=True)),
    Column("failed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('pending', 'published', 'failed')",
        name="ck_configuration_versions_status",
    ),
)


configuration_outbox = Table(
    "configuration_outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "configuration_version",
        BigInteger,
        ForeignKey("configuration_versions.version", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("event_type", String(64), nullable=False),
    Column("payload", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("claimed_by", String(100)),
    Column("claimed_at", DateTime(timezone=True)),
    Column("claim_expires_at", DateTime(timezone=True)),
    Column("processed_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(500)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
Index(
    "ix_configuration_outbox_pending",
    configuration_outbox.c.processed_at,
    configuration_outbox.c.claim_expires_at,
    configuration_outbox.c.id,
)


api_keys = Table(
    "api_keys",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tenant_id", String(200), nullable=False),
    Column("project_id", String(200), nullable=False),
    Column("name", String(200)),
    Column("key_hash", String(64), nullable=False, unique=True),
    Column("key_prefix", String(16), nullable=False),
    Column("scopes", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("model_allowlist", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("budget_limit_usd", Numeric(20, 8)),
    Column("expires_at", DateTime(timezone=True)),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("status IN ('active', 'revoked')", name="ck_api_keys_status"),
)
Index("ix_api_keys_tenant_created", api_keys.c.tenant_id, api_keys.c.created_at)


tenant_policies = Table(
    "tenant_policies",
    metadata,
    Column("tenant_id", String(200), primary_key=True),
    Column("model_allowlist", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("routing_objective", String(32)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)


audit_events = Table(
    "audit_events",
    metadata,
    Column("sequence_id", BigInteger, primary_key=True, autoincrement=True),
    Column("id", String(64), nullable=False, unique=True),
    Column("actor_id", String(200), nullable=False),
    Column("tenant_id", String(200), nullable=False),
    Column("action", String(100), nullable=False),
    Column("target_type", String(100), nullable=False),
    Column("target_id", String(200), nullable=False),
    Column("before", JSONB),
    Column("after", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
Index("ix_audit_events_tenant_sequence", audit_events.c.tenant_id, audit_events.c.sequence_id)


usage_events = Table(
    "usage_events",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("request_id", String(100), nullable=False),
    Column("tenant_id", String(200), nullable=False),
    Column("project_id", String(200), nullable=False),
    Column("api_key_id", String(100), nullable=False),
    Column("endpoint", String(50), nullable=False),
    Column("logical_model", String(200), nullable=False),
    Column("provider_connection_id", String(64), nullable=False),
    Column("provider_protocol", String(32), nullable=False),
    Column("provider_model", String(300), nullable=False),
    Column("configuration_version", BigInteger, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Numeric(20, 8), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
Index("ix_usage_events_tenant_id", usage_events.c.tenant_id, usage_events.c.id)
Index(
    "ix_usage_events_tenant_created_id",
    usage_events.c.tenant_id,
    usage_events.c.created_at,
    usage_events.c.id,
)


route_decisions = Table(
    "route_decisions",
    metadata,
    Column("tenant_id", String(200), primary_key=True),
    Column("request_id", String(100), primary_key=True),
    Column("project_id", String(200), nullable=False),
    Column("endpoint", String(50), nullable=False),
    Column("logical_model", String(200), nullable=False),
    Column("configuration_version", BigInteger, nullable=False),
    Column("status", String(16), nullable=False),
    Column("selected_provider_connection_id", String(64)),
    Column("selected_provider_protocol", String(32)),
    Column("selected_provider_model", String(300)),
    Column("attempts", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("rejected_candidates", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)


budget_spend = Table(
    "budget_spend",
    metadata,
    Column("api_key_id", String(100), primary_key=True),
    Column("actual_spend_usd", Numeric(20, 8), nullable=False, server_default="0"),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)


usage_write_intents = Table(
    "usage_write_intents",
    metadata,
    Column("id", String(100), primary_key=True),
    Column("request_id", String(100), nullable=False, unique=True),
    Column("tenant_id", String(200), nullable=False),
    Column("project_id", String(200), nullable=False),
    Column("api_key_id", String(100), nullable=False),
    Column("endpoint", String(50), nullable=False),
    Column("logical_model", String(200), nullable=False),
    Column("configuration_version", BigInteger, nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"),
    Column("reservation_data", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('pending', 'completed', 'failed')",
        name="ck_usage_write_intents_status",
    ),
)
Index("ix_usage_write_intents_pending", usage_write_intents.c.status, usage_write_intents.c.created_at)


budget_reconciliation_outbox = Table(
    "budget_reconciliation_outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("reservation_id", String(100), nullable=False, unique=True),
    Column("api_key_id", String(100), nullable=False),
    Column("amount_usd", Numeric(20, 8), nullable=False),
    Column("claimed_by", String(100)),
    Column("claimed_at", DateTime(timezone=True)),
    Column("claim_expires_at", DateTime(timezone=True)),
    Column("processed_at", DateTime(timezone=True)),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", String(500)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)
Index(
    "ix_budget_reconciliation_pending",
    budget_reconciliation_outbox.c.processed_at,
    budget_reconciliation_outbox.c.claim_expires_at,
    budget_reconciliation_outbox.c.id,
)


sqlite_migration_runs = Table(
    "sqlite_migration_runs",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("source_fingerprint", String(64), nullable=False, unique=True),
    Column("source_path", Text, nullable=False),
    Column("status", String(20), nullable=False, server_default="pending"),
    Column("report", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "status IN ('pending', 'running', 'completed', 'failed')",
        name="ck_sqlite_migration_runs_status",
    ),
)


sqlite_migration_batches = Table(
    "sqlite_migration_batches",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "migration_run_id",
        String(64),
        ForeignKey("sqlite_migration_runs.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("entity", String(100), nullable=False),
    Column("source_cursor", String(200)),
    Column("rows_imported", Integer, nullable=False, server_default="0"),
    Column("status", String(20), nullable=False, server_default="pending"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
    UniqueConstraint("migration_run_id", "entity", "source_cursor", name="uq_migration_batch"),
)
