from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Run MixAPI PostgreSQL migrations")
    parser.add_argument("command", choices=("upgrade", "downgrade", "current"))
    parser.add_argument("revision", nargs="?", default="head")
    arguments = parser.parse_args()

    database_url = os.getenv("MIXAPI_DATABASE_URL")
    if not database_url:
        parser.error("MIXAPI_DATABASE_URL is required")

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url)
    if arguments.command == "upgrade":
        command.upgrade(config, arguments.revision)
    elif arguments.command == "downgrade":
        command.downgrade(config, arguments.revision)
    else:
        command.current(config)


if __name__ == "__main__":
    main()
