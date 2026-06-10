from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mixapi.app import create_app  # noqa: E402
from mixapi.settings import Settings  # noqa: E402


def main() -> None:
    destination = ROOT / "openapi" / "openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url="postgresql://mixapi:mixapi@127.0.0.1:5432/mixapi",
        redis_url="redis://127.0.0.1:6379/0",
        master_key=bytes(32),
        master_key_version=1,
        previous_master_keys={},
    )
    contract = create_app(settings=settings).openapi()
    destination.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
