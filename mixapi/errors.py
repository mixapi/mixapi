from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class MixAPIError(Exception):
    type: str
    code: str
    message: str
    status_code: int


def error_response(request: Request, error: MixAPIError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "type": error.type,
                "code": error.code,
                "message": error.message,
                "request_id": getattr(request.state, "request_id", "req_unknown"),
                "trace_id": getattr(request.state, "trace_id", "trace_unknown"),
            }
        },
    )


def authentication_failed(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="authentication_failed",
        code=code,
        message=message,
        status_code=401,
    )


def permission_denied(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="permission_denied",
        code=code,
        message=message,
        status_code=403,
    )


def validation_error(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="validation_error",
        code=code,
        message=message,
        status_code=400,
    )


def capability_unsupported(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="capability_unsupported",
        code=code,
        message=message,
        status_code=400,
    )


def structured_output_error(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="structured_output_error",
        code=code,
        message=message,
        status_code=422,
    )


def quota_exceeded(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="quota_exceeded",
        code=code,
        message=message,
        status_code=429,
    )


def budget_exceeded(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="budget_exceeded",
        code=code,
        message=message,
        status_code=402,
    )


def provider_unavailable(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="provider_unavailable",
        code=code,
        message=message,
        status_code=503,
    )


def control_plane_unavailable(
    code: str = "control_plane_unavailable",
    message: str = "The control plane is temporarily unavailable.",
) -> MixAPIError:
    return MixAPIError(
        type="control_plane_unavailable",
        code=code,
        message=message,
        status_code=503,
    )


def configuration_unavailable(
    code: str = "configuration_unavailable",
    message: str = "The active routing configuration is unavailable.",
) -> MixAPIError:
    return MixAPIError(
        type="configuration_unavailable",
        code=code,
        message=message,
        status_code=503,
    )


def provider_rate_limited(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="provider_rate_limited",
        code=code,
        message=message,
        status_code=429,
    )


def upstream_timeout(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="upstream_timeout",
        code=code,
        message=message,
        status_code=504,
    )


def not_found(code: str, message: str) -> MixAPIError:
    return MixAPIError(
        type="not_found",
        code=code,
        message=message,
        status_code=404,
    )
