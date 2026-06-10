from __future__ import annotations

import argparse
import sys

from mixapi.bootstrap import BootstrapService, load_bootstrap_document
from mixapi.postgres import PostgresPool
from mixapi.publication import ConfigurationPublisher
from mixapi.redis_runtime import RedisRuntime
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.secrets import CredentialCipher
from mixapi.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import and publish a MixAPI provider/model configuration document."
    )
    parser.add_argument("document", help="Path to a JSON or YAML seed document")
    parser.add_argument("--actor-id", default="bootstrap-cli")
    args = parser.parse_args()

    pool: PostgresPool | None = None
    redis_runtime: RedisRuntime | None = None
    try:
        settings = Settings.from_env()
        document = load_bootstrap_document(args.document)
        pool = PostgresPool(settings)
        redis_runtime = RedisRuntime(settings)
        pool.open()
        redis_runtime.open()
        cipher = CredentialCipher(
            settings.master_key,
            active_version=settings.master_key_version,
            previous_keys=settings.previous_master_keys,
        )
        repository = PostgresConfigurationRepository(pool, cipher)
        snapshots = RedisSnapshotStore(
            redis_runtime.client,
            namespace=settings.redis_namespace,
        )
        publisher = ConfigurationPublisher(
            repository,
            snapshots,
            retention_count=settings.snapshot_retention_count,
            retention_ttl_seconds=settings.snapshot_retention_ttl_seconds,
        )
        result = BootstrapService(repository, publisher, snapshots).apply(
            document,
            actor_id=args.actor_id,
        )
    except Exception as error:
        print(f"bootstrap failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    finally:
        if redis_runtime is not None:
            redis_runtime.close()
        if pool is not None:
            pool.close()

    print(
        f"active_version={result.active_version} changed={str(result.changed).lower()} "
        f"published_versions={','.join(str(item) for item in result.published_versions)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
