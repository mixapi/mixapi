from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def encode_sse(event: str, data: dict[str, Any]) -> str:
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    return f"event: {event}\ndata: {encoded}\n\n"


def _text_deltas(text: str) -> Iterator[str]:
    if not text:
        return
    words = text.split(" ")
    for index, word in enumerate(words):
        suffix = " " if index < len(words) - 1 else ""
        yield f"{word}{suffix}"
