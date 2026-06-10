from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import httpx

from mixapi.configuration import ProviderConnection
from mixapi.secrets import CredentialAAD, CredentialCipher, InvalidCredentialCiphertext


@dataclass(frozen=True)
class ProviderTestResult:
    status: str
    latency_ms: int
    error_class: str | None

    def public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "latency_ms": self.latency_ms,
            "error_class": self.error_class,
        }


class ProviderConnectionTester:
    def __init__(
        self,
        cipher: CredentialCipher,
        *,
        max_timeout_seconds: float = 5.0,
        verify_tls: bool = True,
    ) -> None:
        self._cipher = cipher
        self._max_timeout_seconds = max_timeout_seconds
        self._verify_tls = verify_tls

    def test(self, provider: ProviderConnection) -> ProviderTestResult:
        started = monotonic()
        try:
            credential = self._cipher.decrypt(
                provider.credential,
                CredentialAAD(
                    provider_id=provider.id,
                    protocol=provider.protocol,
                    field="api_key",
                ),
            )
            path, headers = _probe_request(provider.protocol, credential)
            timeout = min(float(provider.timeout_seconds), self._max_timeout_seconds)
            with httpx.Client(verify=self._verify_tls, timeout=timeout) as client:
                response = client.get(f"{provider.base_url.rstrip('/')}{path}", headers=headers)
            error_class = _http_error_class(response.status_code)
        except InvalidCredentialCiphertext:
            error_class = "credential_invalid"
        except httpx.TimeoutException:
            error_class = "timeout"
        except httpx.HTTPError:
            error_class = "network"
        latency_ms = max(0, round((monotonic() - started) * 1000))
        return ProviderTestResult(
            status="ok" if error_class is None else "error",
            latency_ms=latency_ms,
            error_class=error_class,
        )


def _probe_request(protocol: str, credential: str) -> tuple[str, dict[str, str]]:
    if protocol == "openai-compatible":
        return "/models", {"authorization": f"Bearer {credential}"}
    if protocol == "anthropic":
        return "/v1/models", {
            "x-api-key": credential,
            "anthropic-version": "2023-06-01",
        }
    if protocol == "gemini":
        return "/v1beta/models", {"x-goog-api-key": credential}
    if protocol == "ollama":
        headers = {"authorization": f"Bearer {credential}"} if credential else {}
        return "/api/tags", headers
    return "/", {}


def _http_error_class(status_code: int) -> str | None:
    if 200 <= status_code < 300:
        return None
    if status_code in (401, 403):
        return "authentication"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "upstream"
    return "http_error"
