from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from mixapi.migration.sqlite_import import SQLiteMigrator
from mixapi.settings import Settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mixapi")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate-sqlite",
        help="import a legacy SQLite installation into PostgreSQL and Redis",
    )
    migrate.add_argument("--sqlite-path", required=True, type=Path)
    migrate.add_argument("--postgres-dsn", required=True)
    migrate.add_argument("--apply", action="store_true")
    migrate.add_argument("--batch-size", type=int, default=500)
    migrate.add_argument("--report", type=Path)
    migrate.add_argument("--allow-disabled-providers", action="store_true")
    arguments = parser.parse_args(argv)

    try:
        settings = Settings.from_env(database_url=arguments.postgres_dsn)
        report = SQLiteMigrator(
            settings,
            arguments.sqlite_path,
            batch_size=arguments.batch_size,
            allow_disabled_providers=arguments.allow_disabled_providers,
        ).run(apply=arguments.apply)
    except Exception as error:
        print(f"mixapi: {error}", file=sys.stderr)
        return 1

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.report is not None:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

