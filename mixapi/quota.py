from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol
from uuid import uuid4

from mixapi.auth import Principal
from mixapi.errors import quota_exceeded


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


@dataclass
class InMemoryQuotaService:
    request_limit: int | None = None
    token_limit: int | None = None
    _request_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _actual_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _reserved_tokens: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _token_reservations: dict[str, TokenReservation] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def reserve_request(self, principal: Principal) -> None:
        if self.request_limit is None:
            return

        with self._lock:
            current = self._request_counts[principal.api_key_id]
            if current >= self.request_limit:
                raise quota_exceeded("request_quota_exceeded", "Request quota exceeded.")

            self._request_counts[principal.api_key_id] = current + 1

    def reserve_tokens(self, principal: Principal, estimated_tokens: int) -> TokenReservation:
        reservation = TokenReservation(principal.api_key_id, estimated_tokens)
        if self.token_limit is None:
            return reservation

        with self._lock:
            committed = self._actual_tokens[principal.api_key_id]
            reserved = self._reserved_tokens[principal.api_key_id]
            if committed + reserved + estimated_tokens > self.token_limit:
                raise quota_exceeded("token_quota_exceeded", "Token quota exceeded.")

            self._reserved_tokens[principal.api_key_id] = reserved + estimated_tokens
            self._token_reservations[reservation.reservation_id] = reservation
        return reservation

    def reconcile_tokens(self, reservation: TokenReservation, actual_tokens: int) -> None:
        if self.token_limit is None:
            return

        with self._lock:
            active = self._token_reservations.pop(reservation.reservation_id, None)
            if active is None:
                return
            self._reserved_tokens[active.api_key_id] -= active.amount_tokens
            self._actual_tokens[active.api_key_id] += actual_tokens

    def release_tokens(self, reservation: TokenReservation) -> None:
        if self.token_limit is None:
            return

        with self._lock:
            active = self._token_reservations.pop(reservation.reservation_id, None)
            if active is None:
                return
            self._reserved_tokens[active.api_key_id] -= active.amount_tokens
