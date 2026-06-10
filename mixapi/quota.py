from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from mixapi.auth import Principal


@dataclass(frozen=True)
class TokenReservation:
    api_key_id: str
    amount_tokens: int
    reservation_id: str = field(default_factory=lambda: uuid4().hex)


class QuotaService(Protocol):
    def reserve_request(self, principal: Principal) -> None: ...

    def reserve_tokens(self, principal: Principal, estimated_tokens: int) -> TokenReservation: ...

    def reconcile_tokens(self, reservation: TokenReservation, actual_tokens: int) -> None: ...

    def release_tokens(self, reservation: TokenReservation) -> None: ...
