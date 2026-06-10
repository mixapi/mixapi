from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mixapi.app import create_app  # noqa: E402


def main() -> None:
    destination = ROOT / "openapi" / "openapi.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    contract = create_app().openapi()
    destination.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
