from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from fastapi import Body, Depends, Request
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse

from mixapi.adapters import (
    AdapterResponse,
    ProviderDispatchError,
    ProviderStream,
    ProviderStreamEvent,
    count_tokens,
    embedding_text,
    extract_text,
)
from mixapi.adapter_factory import AdapterFactory
from mixapi.admin.providers import create_provider_router
from mixapi.admin.models import create_model_router
from mixapi.admin.publication import create_publication_router
from mixapi.api_contract import install_openapi_contract
from mixapi.auth import (
    AdminPrincipal,
    Principal,
    build_admin_authenticator,
    build_service_authenticator,
    require_scope,
)
from mixapi.auth_cache import RedisAuthCache
from mixapi.bootstrap import ReadinessService
from mixapi.budget import BudgetService
from mixapi.circuits import CircuitBreaker
from mixapi.errors import (
    MixAPIError,
    budget_exceeded,
    configuration_unavailable,
    error_response,
    provider_rate_limited,
    provider_unavailable,
    structured_output_error,
    upstream_timeout,
    validation_error,
)
from mixapi.models import ProviderModel
from mixapi.observability import InMemoryObservability, Observability, SafeObservability
from mixapi.postgres import PostgresPool
from mixapi.provider_testing import ProviderConnectionTester
from mixapi.publication import ConfigurationPublisher
from mixapi.quota import QuotaService, TokenReservation
from mixapi.redis_runtime import RedisRuntime
from mixapi.repositories.budgets import PostgresBudgetService
from mixapi.repositories.configuration import PostgresConfigurationRepository
from mixapi.repositories.control_plane import PostgresControlPlaneStore
from mixapi.repositories.route_decisions import PostgresRouteDecisionStore
from mixapi.repositories.usage import PostgresUsageLedger
from mixapi.runtime.budget import RedisBudgetService
from mixapi.runtime.circuits import RedisCircuitBreaker
from mixapi.runtime.context import RequestRuntimeContext
from mixapi.runtime.idempotency import RedisIdempotencyStore
from mixapi.runtime.quota import RedisQuotaService
from mixapi.runtime.sessions import RedisStickySessionStore
from mixapi.runtime.snapshots import RedisSnapshotStore
from mixapi.route_decisions import RouteDecisionRecord
from mixapi.routing import plan_route
from mixapi.routing_catalog import catalog_from_snapshot
from mixapi.streaming import encode_sse
from mixapi.structured_output import (
    CORRECTIVE_RETRY_TOKEN_RESERVE,
    ValidationFailure,
    corrective_request,
    response_schema,
    validate_output,
)
from mixapi.settings import Settings
from mixapi.secrets import CredentialCipher
from mixapi.usage import (
    UsageEvent,
    UsageQueryValidationError,
    UsageWriteIntent,
    build_usage_query,
    encode_usage_cursor,
    usage_csv,
    usage_jsonl,
    usage_response,
)
from mixapi.validation import validate_embedding_request, validate_response_request
from mixapi.workers import (
    ConfigurationOutboxWorker,
    ConfigurationRebuildWorker,
    ManagedWorkers,
    WorkerJob,
)


def create_app(
    request_quota_limit: int | None = None,
    adapter_overrides: dict[str, Any] | None = None,
    budget_limit_usd: Decimal | str | None = None,
    circuit_failure_threshold: int | None = None,
    circuit_recovery_seconds: float | None = None,
    token_quota_limit: int | None = None,
    admin_api_key: str | None = None,
    observability: Observability | None = None,
    settings: Settings | None = None,
    provider_tester: ProviderConnectionTester | None = None,
) -> FastAPI:
    configured_settings = settings or Settings.from_env()
    postgres_pool = PostgresPool(configured_settings)
    redis_runtime = RedisRuntime(configured_settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        postgres_pool.open()
        try:
            redis_runtime.open()
        except Exception:
            postgres_pool.close()
            raise
        try:
            await asyncio.to_thread(configuration_rebuild_worker.run_once)
            await managed_workers.start()
            yield
        finally:
            await managed_workers.stop()
            redis_runtime.close()
            postgres_pool.close()

    app = FastAPI(title="MixAPI", version="0.1.0", lifespan=lifespan)
    configured_token_quota_limit = token_quota_limit
    if configured_token_quota_limit is None:
        raw_token_quota_limit = os.getenv("MIXAPI_TOKEN_QUOTA_LIMIT")
        if raw_token_quota_limit is not None:
            configured_token_quota_limit = int(raw_token_quota_limit)
    quota = RedisQuotaService(
        redis_runtime.client,
        namespace=configured_settings.redis_namespace,
        request_limit=request_quota_limit,
        token_limit=configured_token_quota_limit,
    )
    configured_budget_limit = budget_limit_usd or os.getenv("MIXAPI_BUDGET_LIMIT_USD")
    budget_limit = Decimal(str(configured_budget_limit)) if configured_budget_limit else None
    configured_circuit_failure_threshold = circuit_failure_threshold or int(
        os.getenv("MIXAPI_CIRCUIT_FAILURE_THRESHOLD", "3")
    )
    configured_circuit_recovery_seconds = circuit_recovery_seconds or float(
        os.getenv("MIXAPI_CIRCUIT_RECOVERY_SECONDS", "30")
    )
    auth_cache = RedisAuthCache(
        redis_runtime.client,
        ttl_seconds=configured_settings.auth_cache_ttl_seconds,
    )
    control_plane = PostgresControlPlaneStore(postgres_pool, auth_cache=auth_cache)
    configured_admin_api_key = admin_api_key or configured_settings.admin_api_key
    authenticate_admin = build_admin_authenticator(configured_admin_api_key)
    authenticate_service = build_service_authenticator(control_plane, auth_cache)
    durable_budget = PostgresBudgetService(postgres_pool)
    budget = RedisBudgetService(
        redis_runtime.client,
        durable_budget,
        namespace=configured_settings.redis_namespace,
        limit_usd=budget_limit,
    )
    circuits = RedisCircuitBreaker(
        redis_runtime.client,
        namespace=configured_settings.redis_namespace,
        failure_threshold=configured_circuit_failure_threshold,
        recovery_timeout_seconds=configured_circuit_recovery_seconds,
    )
    usage_ledger = PostgresUsageLedger(postgres_pool)
    idempotency_store = RedisIdempotencyStore(
        redis_runtime.client,
        namespace=configured_settings.redis_namespace,
        ttl_seconds=configured_settings.idempotency_ttl_seconds,
    )
    session_store = RedisStickySessionStore(
        redis_runtime.client,
        namespace=configured_settings.redis_namespace,
    )
    route_decision_store = PostgresRouteDecisionStore(postgres_pool)
    credential_cipher = CredentialCipher(
        configured_settings.master_key,
        active_version=configured_settings.master_key_version,
        previous_keys=configured_settings.previous_master_keys,
    )
    adapter_factory = AdapterFactory(
        credential_cipher,
        connection_overrides=adapter_overrides,
        retained_versions=configured_settings.snapshot_retention_count,
    )
    configuration_repository = PostgresConfigurationRepository(
        postgres_pool,
        credential_cipher,
    )
    connection_tester = provider_tester or ProviderConnectionTester(credential_cipher)
    snapshot_store = RedisSnapshotStore(
        redis_runtime.client,
        namespace=configured_settings.redis_namespace,
    )
    configuration_publisher = ConfigurationPublisher(
        configuration_repository,
        snapshot_store,
        retention_count=configured_settings.snapshot_retention_count,
        retention_ttl_seconds=configured_settings.snapshot_retention_ttl_seconds,
    )
    worker_id = f"instance_{uuid.uuid4().hex}"
    configuration_outbox_worker = ConfigurationOutboxWorker(
        configuration_repository,
        configuration_publisher,
        worker_id=worker_id,
        lease_seconds=configured_settings.outbox_claim_lease_seconds,
    )
    configuration_rebuild_worker = ConfigurationRebuildWorker(configuration_publisher)
    readiness = ReadinessService(
        postgres_pool,
        redis_runtime,
        snapshot_store,
        credential_cipher,
    )

    def active_catalog() -> dict[str, Any]:
        return catalog_from_snapshot(readiness.require_snapshot())

    managed_workers = ManagedWorkers(
        (
            WorkerJob(
                "configuration-publisher",
                configured_settings.configuration_publisher_interval_seconds,
                configuration_outbox_worker.run_once,
            ),
            WorkerJob(
                "configuration-rebuild",
                configured_settings.configuration_rebuild_interval_seconds,
                configuration_rebuild_worker.run_once,
                run_immediately=False,
            ),
            WorkerJob(
                "usage-intent-recovery",
                configured_settings.usage_intent_recovery_interval_seconds,
                lambda: usage_ledger.fail_expired_intents(
                    stale_seconds=configured_settings.usage_intent_stale_seconds
                ),
            ),
            WorkerJob(
                "budget-reconciliation",
                configured_settings.budget_reconciliation_interval_seconds,
                lambda: budget.reconcile_pending(
                    worker_id,
                    lease_seconds=configured_settings.outbox_claim_lease_seconds,
                ),
            ),
        ),
        shutdown_timeout_seconds=configured_settings.worker_shutdown_timeout_seconds,
    )
    if observability is None:
        observability = InMemoryObservability()
    instrumentation = SafeObservability(observability)
    app.state.usage_ledger = usage_ledger
    app.state.idempotency_store = idempotency_store
    app.state.session_store = session_store
    app.state.route_decision_store = route_decision_store
    app.state.quota = quota
    app.state.budget = budget
    app.state.circuits = circuits
    app.state.control_plane = control_plane
    app.state.auth_cache = auth_cache
    app.state.observability = observability
    app.state.settings = configured_settings
    app.state.postgres_pool = postgres_pool
    app.state.redis_runtime = redis_runtime
    app.state.configuration_repository = configuration_repository
    app.state.snapshot_store = snapshot_store
    app.state.configuration_publisher = configuration_publisher
    app.state.workers = managed_workers
    app.state.readiness = readiness
    app.state.provider_tester = connection_tester
    app.state.adapter_factory = adapter_factory
    app.include_router(
        create_provider_router(
            configuration_repository,
            authenticate_admin,
            connection_tester,
        )
    )
    app.include_router(create_model_router(configuration_repository, authenticate_admin))
    app.include_router(
        create_publication_router(
            configuration_repository,
            configuration_publisher,
            snapshot_store,
            authenticate_admin,
        )
    )

    @app.middleware("http")
    async def attach_request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", f"req_{uuid.uuid4().hex}")
        request.state.trace_id = request.headers.get("traceparent", f"trace_{uuid.uuid4().hex}")
        if request.url.path.startswith("/v1/"):
            try:
                snapshot = readiness.require_snapshot()
            except MixAPIError:
                return error_response(request, configuration_unavailable())
            request.state.runtime_context = RequestRuntimeContext.create(
                request_id=request.state.request_id,
                trace_id=request.state.trace_id,
                snapshot=snapshot,
                adapter_factory=adapter_factory,
                observability=instrumentation,
            )
        return await call_next(request)

    @app.exception_handler(MixAPIError)
    async def mixapi_error_handler(request: Request, error: MixAPIError):
        return error_response(request, error)

    @app.get("/health/live", include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def health_ready() -> JSONResponse:
        status = readiness.check()
        return JSONResponse(
            status_code=200 if status.ready else 503,
            content={
                "status": "ready" if status.ready else "not_ready",
                "checks": status.checks,
                "active_version": status.active_version,
            },
        )

    @app.get("/v1/models")
    def list_models(
        request: Request,
        principal: Principal = Depends(authenticate_service),
    ) -> dict[str, object]:
        require_scope(principal, "models:read")
        catalog = _request_runtime_context(request).catalog
        return {
            "object": "list",
            "data": [
                model.public_dict()
                for model in catalog.values()
                if principal.model_allowlist is None
                or model.id in principal.model_allowlist
            ],
        }

    @app.post("/v1/responses")
    def create_response(
        request: Request,
        request_body: dict[str, Any] = Body(default_factory=dict),
        principal: Principal = Depends(authenticate_service),
    ) -> Any:
        require_scope(principal, "responses:create")
        runtime = _request_runtime_context(request)
        snapshot = runtime.snapshot
        adapters = runtime.adapters
        request_observability = runtime.observability
        configuration_version = runtime.configuration_version
        idempotency_key = request.headers.get("idempotency-key")
        validate_response_request(request_body, idempotency_key=idempotency_key)
        if replay := idempotency_store.replay(principal, "responses", idempotency_key, request_body):
            return replay.response

        session_id = request.headers.get("x-mixapi-session-id")
        connection_pin = None
        if session_id:
            connection_pin = session_store.get_connection_id(principal.tenant_id, session_id)

        decision = plan_route(
            snapshot,
            request_body,
            endpoint="responses",
            model_allowlist=principal.model_allowlist,
            default_objective=principal.routing_objective,
            selection_seed=request.state.request_id,
            connection_pin=connection_pin,
        )
        circuit_candidates, circuit_rejections = _circuit_eligible_candidates(
            decision.candidates,
            circuits,
            request_observability,
            request.state.trace_id,
        )
        if not circuit_candidates:
            route_decision_store.record(
                RouteDecisionRecord(
                    request_id=request.state.request_id,
                    tenant_id=principal.tenant_id,
                    project_id=principal.project_id,
                    endpoint="responses",
                    logical_model=request_body["model"],
                    status="failed",
                    selected_provider=None,
                    selected_provider_model=None,
                    attempts=(),
                    rejected_candidates=(*decision.rejected, *circuit_rejections),
                    configuration_version=configuration_version,
                )
            )
            raise provider_unavailable(
                "all_provider_circuits_open",
                "All eligible provider circuits are open.",
            )
        candidates, budget_rejections, reservation = _reserve_budget(
            circuit_candidates,
            request_body,
            endpoint="responses",
            budget=budget,
            principal=principal,
            observability=request_observability,
            trace_id=request.state.trace_id,
        )
        rejected_candidates = (*decision.rejected, *circuit_rejections, *budget_rejections)
        try:
            token_reservation = _reserve_quotas(
                quota,
                principal,
                _estimate_request_tokens(request_body, candidates, endpoint="responses"),
            )
        except MixAPIError:
            budget.release(reservation)
            raise
        try:
            usage_ledger.begin_intent(
                _usage_write_intent(
                    request_id=request.state.request_id,
                    principal=principal,
                    endpoint="responses",
                    logical_model=request_body["model"],
                    reservation=reservation,
                    token_reservation=token_reservation,
                    configuration_version=configuration_version,
                )
            )
        except Exception:
            budget.release(reservation)
            quota.release_tokens(token_reservation)
            raise
        if request_body.get("stream", False):
            try:
                provider_stream, selected_candidate, failed_attempts = (
                    _dispatch_response_stream_with_fallback(
                        adapter=adapters,
                        request_body=request_body,
                        candidates=candidates,
                        circuits=circuits,
                        quota=quota,
                        observability=request_observability,
                        trace_id=request.state.trace_id,
                    )
                )
            except AllCandidatesFailed as error:
                _record_fallback_transitions(
                    request_observability,
                    request.state.trace_id,
                    principal,
                    request_body["model"],
                    error.failed_attempts,
                )
                budget.release(reservation)
                quota.release_tokens(token_reservation)
                usage_ledger.fail_intent(request.state.request_id)
                route_decision_store.record(
                    RouteDecisionRecord(
                        request_id=request.state.request_id,
                        tenant_id=principal.tenant_id,
                        project_id=principal.project_id,
                        endpoint="responses",
                        logical_model=request_body["model"],
                        status="failed",
                        selected_provider=None,
                        selected_provider_model=None,
                        attempts=tuple(error.failed_attempts),
                        rejected_candidates=rejected_candidates,
                        configuration_version=configuration_version,
                    )
                )
                raise _public_error_for_failed_attempts(error.failed_attempts)

            _record_fallback_transitions(
                request_observability,
                request.state.trace_id,
                principal,
                request_body["model"],
                failed_attempts,
                selected_candidate,
            )

            return StreamingResponse(
                _native_response_event_stream(
                    provider_stream=provider_stream,
                    selected_candidate=selected_candidate,
                    failed_attempts=failed_attempts,
                    rejected_candidates=rejected_candidates,
                    request_body=request_body,
                    request_id=request.state.request_id,
                    principal=principal,
                    reservation=reservation,
                    budget=budget,
                    quota=quota,
                    token_reservation=token_reservation,
                    circuits=circuits,
                    usage_ledger=usage_ledger,
                    route_decision_store=route_decision_store,
                    observability=request_observability,
                    configuration_version=configuration_version,
                    trace_id=request.state.trace_id,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            outcome = _dispatch_response_with_fallback(
                adapter=adapters,
                request_body=request_body,
                candidates=candidates,
                circuits=circuits,
                quota=quota,
                observability=request_observability,
                trace_id=request.state.trace_id,
            )
        except AllCandidatesFailed as error:
            _record_fallback_transitions(
                request_observability,
                request.state.trace_id,
                principal,
                request_body["model"],
                error.failed_attempts,
            )
            if error.billable_dispatches:
                input_tokens, output_tokens, cost = _billable_dispatch_totals(
                    error.billable_dispatches
                )
                usage_events = _billable_response_usage_events(
                    dispatches=error.billable_dispatches,
                    request_id=request.state.request_id,
                    principal=principal,
                    logical_model=request_body["model"],
                    configuration_version=configuration_version,
                )
                _settle_accounting(
                    budget,
                    reservation,
                    cost,
                    usage_ledger,
                    usage_events,
                )
                quota.reconcile_tokens(
                    token_reservation,
                    input_tokens + output_tokens,
                )
            else:
                budget.release(reservation)
                quota.release_tokens(token_reservation)
                usage_ledger.fail_intent(request.state.request_id)
            route_decision_store.record(
                RouteDecisionRecord(
                    request_id=request.state.request_id,
                    tenant_id=principal.tenant_id,
                    project_id=principal.project_id,
                    endpoint="responses",
                    logical_model=request_body["model"],
                    status="failed",
                    selected_provider=None,
                    selected_provider_model=None,
                    attempts=tuple(error.failed_attempts),
                    rejected_candidates=rejected_candidates,
                    configuration_version=configuration_version,
                )
            )
            raise _public_error_for_failed_attempts(
                error.failed_attempts,
                error.validation_failures,
            )

        adapter_response = outcome.response
        selected_candidate = outcome.selected_candidate
        failed_attempts = list(outcome.failed_attempts)

        _record_fallback_transitions(
            request_observability,
            request.state.trace_id,
            principal,
            request_body["model"],
            failed_attempts,
            selected_candidate,
        )

        if session_id and selected_candidate.provider_connection_id:
            session_store.set_connection_id(
                principal.tenant_id,
                session_id,
                selected_candidate.provider_connection_id,
            )

        response_id = f"resp_{uuid.uuid4().hex}"
        input_tokens, output_tokens, cost = _billable_dispatch_totals(
            outcome.billable_dispatches
        )
        usage_events = _billable_response_usage_events(
            dispatches=outcome.billable_dispatches,
            request_id=request.state.request_id,
            principal=principal,
            logical_model=request_body["model"],
            configuration_version=configuration_version,
        )
        _settle_accounting(
            budget,
            reservation,
            cost,
            usage_ledger,
            usage_events,
        )
        quota.reconcile_tokens(
            token_reservation,
            input_tokens + output_tokens,
        )
        attempts = _route_attempts(
            failed_attempts=failed_attempts,
            selected_provider=selected_candidate.provider,
            selected_provider_model=selected_candidate.provider_model_id,
        )
        route_decision_store.record(
            RouteDecisionRecord(
                request_id=request.state.request_id,
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
                endpoint="responses",
                logical_model=request_body["model"],
                status="succeeded",
                selected_provider=selected_candidate.provider,
                selected_provider_model=selected_candidate.provider_model_id,
                selected_provider_connection_id=selected_candidate.provider_connection_id,
                selected_provider_protocol=selected_candidate.protocol,
                attempts=tuple(attempts),
                rejected_candidates=rejected_candidates,
                configuration_version=configuration_version,
            )
        )

        response_payload = {
            "id": response_id,
            "object": "response",
            "model": request_body["model"],
            "provider": selected_candidate.provider,
            "output": _response_output(adapter_response),
            "output_text": adapter_response.output_text,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_input_tokens": 0,
                "billable_units": [
                    {"type": "input_tokens", "quantity": input_tokens},
                    {"type": "output_tokens", "quantity": output_tokens},
                ],
            },
            "cost": {
                "provider_cost_usd": _format_money(cost),
                "platform_cost_usd": _format_money(cost),
            },
            "route": {
                "attempts": len(failed_attempts) + 1,
                "fallback_used": bool(failed_attempts),
                "configuration_version": configuration_version,
                "provider_connection_id": selected_candidate.provider_connection_id,
                "provider_name": selected_candidate.provider,
                "provider_protocol": selected_candidate.protocol,
                "selected_provider_model": selected_candidate.provider_model_id,
                "failed_attempts": failed_attempts,
                "decision_trace_id": request.state.request_id,
                "rejected_candidates": list(rejected_candidates),
            },
        }
        idempotency_store.store(principal, "responses", idempotency_key, request_body, response_payload)
        return response_payload

    @app.post("/v1/embeddings")
    def create_embedding(
        request: Request,
        request_body: dict[str, Any] = Body(default_factory=dict),
        principal: Principal = Depends(authenticate_service),
    ) -> dict[str, Any]:
        require_scope(principal, "embeddings:create")
        runtime = _request_runtime_context(request)
        snapshot = runtime.snapshot
        adapters = runtime.adapters
        request_observability = runtime.observability
        configuration_version = runtime.configuration_version
        idempotency_key = request.headers.get("idempotency-key")
        validate_embedding_request(request_body)
        if replay := idempotency_store.replay(principal, "embeddings", idempotency_key, request_body):
            return replay.response

        session_id = request.headers.get("x-mixapi-session-id")
        connection_pin = None
        if session_id:
            connection_pin = session_store.get_connection_id(principal.tenant_id, session_id)

        decision = plan_route(
            snapshot,
            request_body,
            endpoint="embeddings",
            model_allowlist=principal.model_allowlist,
            default_objective=principal.routing_objective,
            selection_seed=request.state.request_id,
            connection_pin=connection_pin,
        )
        circuit_candidates, circuit_rejections = _circuit_eligible_candidates(
            decision.candidates,
            circuits,
            request_observability,
            request.state.trace_id,
        )
        if not circuit_candidates:
            route_decision_store.record(
                RouteDecisionRecord(
                    request_id=request.state.request_id,
                    tenant_id=principal.tenant_id,
                    project_id=principal.project_id,
                    endpoint="embeddings",
                    logical_model=request_body["model"],
                    status="failed",
                    selected_provider=None,
                    selected_provider_model=None,
                    attempts=(),
                    rejected_candidates=(*decision.rejected, *circuit_rejections),
                    configuration_version=configuration_version,
                )
            )
            raise provider_unavailable(
                "all_provider_circuits_open",
                "All eligible provider circuits are open.",
            )
        candidates, budget_rejections, reservation = _reserve_budget(
            circuit_candidates,
            request_body,
            endpoint="embeddings",
            budget=budget,
            principal=principal,
            observability=request_observability,
            trace_id=request.state.trace_id,
        )
        rejected_candidates = (*decision.rejected, *circuit_rejections, *budget_rejections)
        try:
            token_reservation = _reserve_quotas(
                quota,
                principal,
                _estimate_request_tokens(request_body, candidates, endpoint="embeddings"),
            )
        except MixAPIError:
            budget.release(reservation)
            raise
        try:
            usage_ledger.begin_intent(
                _usage_write_intent(
                    request_id=request.state.request_id,
                    principal=principal,
                    endpoint="embeddings",
                    logical_model=request_body["model"],
                    reservation=reservation,
                    token_reservation=token_reservation,
                    configuration_version=configuration_version,
                )
            )
        except Exception:
            budget.release(reservation)
            quota.release_tokens(token_reservation)
            raise
        try:
            adapter_response, selected_candidate, failed_attempts = _dispatch_embedding_with_fallback(
                adapter=adapters,
                request_body=request_body,
                candidates=candidates,
                circuits=circuits,
                quota=quota,
                observability=request_observability,
                trace_id=request.state.trace_id,
            )
        except AllCandidatesFailed as error:
            _record_fallback_transitions(
                request_observability,
                request.state.trace_id,
                principal,
                request_body["model"],
                error.failed_attempts,
            )
            budget.release(reservation)
            quota.release_tokens(token_reservation)
            usage_ledger.fail_intent(request.state.request_id)
            route_decision_store.record(
                RouteDecisionRecord(
                    request_id=request.state.request_id,
                    tenant_id=principal.tenant_id,
                    project_id=principal.project_id,
                    endpoint="embeddings",
                    logical_model=request_body["model"],
                    status="failed",
                    selected_provider=None,
                    selected_provider_model=None,
                    attempts=tuple(error.failed_attempts),
                    rejected_candidates=rejected_candidates,
                    configuration_version=configuration_version,
                )
            )
            raise _public_error_for_failed_attempts(error.failed_attempts)

        _record_fallback_transitions(
            request_observability,
            request.state.trace_id,
            principal,
            request_body["model"],
            failed_attempts,
            selected_candidate,
        )

        embedding = adapter_response.embedding or []
        cost = _estimate_cost(
            input_tokens=adapter_response.input_tokens,
            output_tokens=0,
            input_price=selected_candidate.pricing.get("input_per_million", "0"),
            output_price="0",
        )
        usage_event = UsageEvent(
            request_id=request.state.request_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            api_key_id=principal.api_key_id,
            endpoint="embeddings",
            logical_model=request_body["model"],
            configuration_version=configuration_version,
            provider=selected_candidate.provider,
            provider_model=selected_candidate.provider_model_id,
            input_tokens=adapter_response.input_tokens,
            output_tokens=0,
            cost_usd=cost,
            provider_connection_id=selected_candidate.provider_connection_id,
            provider_protocol=selected_candidate.protocol,
        )
        _settle_accounting(
            budget,
            reservation,
            cost,
            usage_ledger,
            [usage_event],
        )
        quota.reconcile_tokens(token_reservation, adapter_response.input_tokens)
        attempts = _route_attempts(
            failed_attempts=failed_attempts,
            selected_provider=selected_candidate.provider,
            selected_provider_model=selected_candidate.provider_model_id,
        )
        route_decision_store.record(
            RouteDecisionRecord(
                request_id=request.state.request_id,
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
                endpoint="embeddings",
                logical_model=request_body["model"],
                status="succeeded",
                selected_provider=selected_candidate.provider,
                selected_provider_model=selected_candidate.provider_model_id,
                selected_provider_connection_id=selected_candidate.provider_connection_id,
                selected_provider_protocol=selected_candidate.protocol,
                attempts=tuple(attempts),
                rejected_candidates=rejected_candidates,
                configuration_version=configuration_version,
            )
        )

        response_payload = {
            "object": "list",
            "model": request_body["model"],
            "provider": selected_candidate.provider,
            "data": [
                {
                    "object": "embedding",
                    "index": 0,
                    "embedding": embedding,
                }
            ],
            "usage": {
                "input_tokens": adapter_response.input_tokens,
                "output_tokens": 0,
                "billable_units": [
                    {"type": "embedding_tokens", "quantity": adapter_response.input_tokens}
                ],
            },
            "cost": {
                "provider_cost_usd": _format_money(cost),
                "platform_cost_usd": _format_money(cost),
            },
            "route": {
                "attempts": len(failed_attempts) + 1,
                "fallback_used": bool(failed_attempts),
                "configuration_version": configuration_version,
                "provider_connection_id": selected_candidate.provider_connection_id,
                "provider_name": selected_candidate.provider,
                "provider_protocol": selected_candidate.protocol,
                "selected_provider_model": selected_candidate.provider_model_id,
                "failed_attempts": failed_attempts,
                "decision_trace_id": request.state.request_id,
                "rejected_candidates": list(rejected_candidates),
            },
        }
        idempotency_store.store(principal, "embeddings", idempotency_key, request_body, response_payload)
        return response_payload

    @app.get("/v1/route-decisions/{request_id}")
    def get_route_decision(
        request_id: str,
        principal: Principal = Depends(authenticate_service),
    ) -> dict[str, Any]:
        require_scope(principal, "route-decisions:read")
        return route_decision_store.get_public(request_id, tenant_id=principal.tenant_id)

    @app.get("/v1/usage")
    def get_usage(
        start_time: str | None = None,
        end_time: str | None = None,
        limit: str = "50",
        cursor: str | None = None,
        principal: Principal = Depends(authenticate_service),
    ) -> dict[str, Any]:
        require_scope(principal, "usage:read")
        try:
            query = build_usage_query(
                tenant_id=principal.tenant_id,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                cursor=cursor,
            )
        except UsageQueryValidationError as error:
            raise validation_error(error.code, error.message) from error
        page = usage_ledger.query(query)
        next_cursor = None
        if page.has_more and page.events:
            next_cursor = encode_usage_cursor(
                query,
                page.events[-1].sequence_id or 0,
                after_created_at=page.events[-1].created_at,
            )
        return usage_response(
            page.events,
            has_more=page.has_more,
            next_cursor=next_cursor,
        )

    @app.get("/v1/usage/export")
    def export_usage(
        format: str = "csv",
        start_time: str | None = None,
        end_time: str | None = None,
        principal: Principal = Depends(authenticate_service),
    ) -> Response:
        require_scope(principal, "usage:read")
        if format not in {"csv", "jsonl"}:
            raise validation_error(
                "unsupported_usage_export_format",
                "Only CSV and JSONL usage export are supported.",
            )
        try:
            query = build_usage_query(
                tenant_id=principal.tenant_id,
                start_time=start_time,
                end_time=end_time,
                limit=None,
                cursor=None,
            )
        except UsageQueryValidationError as error:
            raise validation_error(error.code, error.message) from error
        events = usage_ledger.query(query).events
        if format == "jsonl":
            return Response(
                content=usage_jsonl(events),
                media_type="application/x-ndjson",
                headers={"Content-Disposition": 'attachment; filename="mixapi-usage.jsonl"'},
            )
        return Response(
            content=usage_csv(events),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="mixapi-usage.csv"'},
        )

    @app.post("/admin/v1/api-keys", status_code=201)
    def create_api_key(
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        values = _parse_api_key_create(request_body, active_catalog())
        created = control_plane.create_api_key(**values, actor_id=admin.actor_id)
        return {**created.record.public_dict(), "secret": created.secret}

    @app.get("/admin/v1/api-keys")
    def list_api_keys(
        tenant_id: str | None = None,
        _admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        if tenant_id is not None:
            _validate_identifier(tenant_id, "tenant_id")
        return {
            "object": "list",
            "data": [
                record.public_dict()
                for record in control_plane.list_api_keys(tenant_id=tenant_id)
            ],
        }

    @app.patch("/admin/v1/api-keys/{api_key_id}")
    def update_api_key(
        api_key_id: str,
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        changes = _parse_api_key_patch(request_body, active_catalog())
        return control_plane.update_api_key(
            api_key_id,
            changes,
            actor_id=admin.actor_id,
        ).public_dict()

    @app.delete("/admin/v1/api-keys/{api_key_id}")
    def revoke_api_key(
        api_key_id: str,
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        return control_plane.revoke_api_key(
            api_key_id,
            actor_id=admin.actor_id,
        ).public_dict()

    @app.put("/admin/v1/tenants/{tenant_id}/model-allowlist")
    def set_tenant_model_allowlist(
        tenant_id: str,
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        _validate_identifier(tenant_id, "tenant_id")
        if set(request_body) != {"models"}:
            raise validation_error(
                "invalid_model_allowlist",
                "Model allowlist payload must contain only `models`.",
            )
        models = _parse_model_allowlist(request_body.get("models"), active_catalog())
        return control_plane.set_tenant_model_allowlist(
            tenant_id,
            models,
            actor_id=admin.actor_id,
        ).public_dict()

    @app.put("/admin/v1/tenants/{tenant_id}/routing-policy")
    def set_tenant_routing_policy(
        tenant_id: str,
        request_body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        _validate_identifier(tenant_id, "tenant_id")
        if set(request_body) != {"objective"}:
            raise validation_error(
                "invalid_routing_objective",
                "Routing policy payload must contain only `objective`.",
            )
        objective = _parse_routing_objective(request_body.get("objective"))
        return control_plane.set_tenant_routing_policy(
            tenant_id,
            objective,
            actor_id=admin.actor_id,
        ).public_dict()

    @app.get("/admin/v1/audit-events")
    def list_audit_events(
        tenant_id: str | None = None,
        _admin: AdminPrincipal = Depends(authenticate_admin),
    ) -> dict[str, Any]:
        if tenant_id is not None:
            _validate_identifier(tenant_id, "tenant_id")
        return {
            "object": "list",
            "data": [
                event.public_dict()
                for event in control_plane.list_audit_events(tenant_id=tenant_id)
            ],
        }

    install_openapi_contract(app)
    return app


def _request_runtime_context(request: Request) -> RequestRuntimeContext:
    context = getattr(request.state, "runtime_context", None)
    if not isinstance(context, RequestRuntimeContext):
        raise configuration_unavailable()
    return context


@dataclass(frozen=True)
class BillableDispatch:
    candidate: ProviderModel
    response: AdapterResponse


@dataclass(frozen=True)
class ResponseDispatchOutcome:
    response: AdapterResponse
    selected_candidate: ProviderModel
    failed_attempts: tuple[dict[str, str], ...]
    billable_dispatches: tuple[BillableDispatch, ...]


class AllCandidatesFailed(Exception):
    def __init__(
        self,
        failed_attempts: list[dict[str, str]],
        billable_dispatches: list[BillableDispatch] | None = None,
        validation_failures: tuple[ValidationFailure, ...] = (),
    ) -> None:
        super().__init__("all candidates failed")
        self.failed_attempts = failed_attempts
        self.billable_dispatches = billable_dispatches or []
        self.validation_failures = validation_failures


def _public_error_for_failed_attempts(
    failed_attempts: list[dict[str, str]],
    validation_failures: tuple[ValidationFailure, ...] = (),
) -> MixAPIError:
    reasons = {attempt["reason"] for attempt in failed_attempts}
    if reasons == {"schema_validation_failed"}:
        providers = []
        for attempt in failed_attempts:
            provider_model = f"{attempt['provider']}/{attempt['provider_model']}"
            if provider_model not in providers:
                providers.append(provider_model)
        attempt_summary = ", ".join(providers[:4])
        details = "; ".join(
            f"{failure.path}: {failure.message}" for failure in validation_failures
        )
        message = (
            "All eligible providers returned output that failed JSON Schema validation. "
            f"Attempts: {attempt_summary}."
        )
        if details:
            message = f"{message} {details}."
        return structured_output_error("schema_validation_failed", message)
    if reasons == {"upstream_http_429"}:
        return provider_rate_limited(
            "all_candidates_rate_limited",
            "All eligible providers were rate limited.",
        )
    if reasons == {"upstream_timeout"}:
        return upstream_timeout(
            "all_candidates_timed_out",
            "All eligible providers timed out.",
        )
    return provider_unavailable("all_candidates_failed", "All eligible providers failed.")


def _budget_eligible_candidates(
    candidates,
    request_body: dict[str, Any],
    endpoint: str,
) -> tuple[tuple[Any, ...], tuple[dict[str, str], ...], Decimal]:
    routing = request_body.get("routing")
    request_limit = None
    if isinstance(routing, dict) and routing.get("max_cost_usd") is not None:
        request_limit = Decimal(str(routing["max_cost_usd"]))

    eligible: list[Any] = []
    rejected: list[dict[str, str]] = []
    estimates: list[Decimal] = []
    structured = endpoint == "responses" and response_schema(request_body) is not None
    for candidate in candidates:
        estimate = (
            _estimate_structured_candidate_request_cost(request_body, candidate)
            if structured
            else _estimate_candidate_request_cost(request_body, candidate, endpoint)
        )
        if request_limit is not None and not structured and estimate > request_limit:
            rejected.append(
                {
                    "provider": candidate.provider,
                    "model": candidate.provider_model_id,
                    "reason": "request_budget_exceeded",
                }
            )
            continue
        eligible.append(candidate)
        estimates.append(estimate)

    if not eligible:
        raise budget_exceeded(
            "request_budget_exceeded",
            "No eligible provider fits the request cost limit.",
        )
    estimated_cost = (
        sum(estimates, Decimal("0"))
        if structured
        else max(estimates, default=Decimal("0"))
    )
    if request_limit is not None and structured and estimated_cost > request_limit:
        raise budget_exceeded(
            "request_budget_exceeded",
            "No eligible provider fits the request cost limit.",
        )
    return tuple(eligible), tuple(rejected), estimated_cost


def _circuit_eligible_candidates(
    candidates,
    circuits: CircuitBreaker,
    observability: Observability,
    trace_id: str,
) -> tuple[tuple[Any, ...], tuple[dict[str, str], ...]]:
    eligible: list[Any] = []
    rejected: list[dict[str, str]] = []
    for candidate in candidates:
        is_open = circuits.is_open(candidate.provider, candidate.provider_model_id)
        _record_circuit_observation(
            observability,
            trace_id,
            candidate.provider,
            candidate.provider_model_id,
            is_open,
        )
        if is_open:
            rejected.append(
                {
                    "provider": candidate.provider,
                    "model": candidate.provider_model_id,
                    "reason": "provider_circuit_open",
                }
            )
            continue
        eligible.append(candidate)
    return tuple(eligible), tuple(rejected)


def _reserve_budget(
    candidates,
    request_body: dict[str, Any],
    endpoint: str,
    budget: BudgetService,
    principal: Principal,
    observability: Observability,
    trace_id: str,
):
    started_at = monotonic()
    attributes = {
        "tenant": principal.tenant_id,
        "project": principal.project_id,
        "endpoint": endpoint,
    }
    try:
        eligible, rejected, estimated_cost = _budget_eligible_candidates(
            candidates,
            request_body,
            endpoint=endpoint,
        )
        reservation = budget.reserve(
            principal,
            estimated_cost,
            limit_usd=principal.budget_limit_usd,
        )
    except MixAPIError as error:
        if error.type == "budget_exceeded":
            duration_ms = max((monotonic() - started_at) * 1000, 0)
            observability.increment_counter(
                "mixapi_budget_denials_total",
                {"tenant": principal.tenant_id, "project": principal.project_id},
            )
            observability.record_span(
                "budget.reserve",
                trace_id=trace_id,
                status="error",
                duration_ms=duration_ms,
                attributes={**attributes, "error_class": error.code},
            )
        raise
    observability.record_span(
        "budget.reserve",
        trace_id=trace_id,
        status="ok",
        duration_ms=max((monotonic() - started_at) * 1000, 0),
        attributes=attributes,
    )
    return eligible, rejected, reservation


def _estimate_candidate_request_cost(request_body: dict[str, Any], candidate, endpoint: str) -> Decimal:
    if endpoint == "embeddings":
        input_tokens = count_tokens(embedding_text(request_body.get("input", "")))
        output_tokens = 0
    else:
        input_tokens = count_tokens(extract_text(request_body.get("input", "")))
        output_tokens = request_body.get("max_output_tokens", candidate.max_output_tokens)
    return _estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_price=candidate.pricing.get("input_per_million", "0"),
        output_price=candidate.pricing.get("output_per_million", "0"),
    )


def _estimate_structured_candidate_request_cost(
    request_body: dict[str, Any],
    candidate: ProviderModel,
) -> Decimal:
    input_tokens = count_tokens(extract_text(request_body.get("input", "")))
    output_tokens = request_body.get("max_output_tokens", candidate.max_output_tokens)
    first_dispatch = _estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_price=candidate.pricing.get("input_per_million", "0"),
        output_price=candidate.pricing.get("output_per_million", "0"),
    )
    corrective_dispatch = _estimate_cost(
        input_tokens=input_tokens + CORRECTIVE_RETRY_TOKEN_RESERVE,
        output_tokens=output_tokens,
        input_price=candidate.pricing.get("input_per_million", "0"),
        output_price=candidate.pricing.get("output_per_million", "0"),
    )
    return first_dispatch + corrective_dispatch


def _estimate_request_tokens(request_body: dict[str, Any], candidates, endpoint: str) -> int:
    if endpoint == "embeddings":
        return count_tokens(embedding_text(request_body.get("input", "")))

    input_tokens = count_tokens(extract_text(request_body.get("input", "")))
    candidate_tokens = [
        input_tokens + request_body.get("max_output_tokens", candidate.max_output_tokens)
        for candidate in candidates
    ]
    if response_schema(request_body) is not None:
        return sum(
            candidate_token_count * 2 + CORRECTIVE_RETRY_TOKEN_RESERVE
            for candidate_token_count in candidate_tokens
        )
    return max(candidate_tokens, default=input_tokens)


def _reserve_quotas(
    quota: QuotaService,
    principal: Principal,
    estimated_tokens: int,
) -> TokenReservation:
    token_reservation = quota.reserve_tokens(principal, estimated_tokens)
    try:
        quota.reserve_request(principal)
    except MixAPIError:
        quota.release_tokens(token_reservation)
        raise
    return token_reservation


def _dispatch_response_with_fallback(
    adapter: Any,
    request_body: dict[str, Any],
    candidates,
    circuits: CircuitBreaker,
    quota: RedisQuotaService,
    observability: Observability,
    trace_id: str,
) -> ResponseDispatchOutcome:
    failed_attempts: list[dict[str, str]] = []
    billable_dispatches: list[BillableDispatch] = []
    schema = response_schema(request_body)
    validation_failures: tuple[ValidationFailure, ...] = ()

    for candidate in candidates:
        if candidate.provider_connection_id and candidate.concurrency_limit:
            if not quota.reserve_concurrency(candidate.provider_connection_id, candidate.concurrency_limit):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "concurrency_limit_exceeded",
                    }
                )
                continue

        if candidate.provider_connection_id and candidate.request_quota:
            if not quota.reserve_connection_requests(candidate.provider_connection_id, candidate.request_quota):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "request_quota_exceeded",
                    }
                )
                continue

        concurrency_released = False
        def _release_concurrency() -> None:
            nonlocal concurrency_released
            if not concurrency_released and candidate.provider_connection_id and candidate.concurrency_limit:
                quota.release_concurrency(candidate.provider_connection_id)
                concurrency_released = True

        dispatch_request = request_body
        dispatch_count = 2 if schema is not None else 1
        for dispatch_index in range(dispatch_count):
            started_at = monotonic()
            try:
                response = _adapter_for(adapter, candidate).dispatch_response(
                    dispatch_request,
                    candidate,
                )
                _record_provider_attempt(
                    observability,
                    trace_id,
                    endpoint="responses",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model_id,
                    status="ok",
                    started_at=started_at,
                )
                circuits.record_success(candidate.provider, candidate.provider_model_id)
                _record_circuit_state(
                    observability,
                    circuits,
                    trace_id,
                    candidate.provider,
                    candidate.provider_model_id,
                )
            except ProviderDispatchError as error:
                _record_provider_attempt(
                    observability,
                    trace_id,
                    endpoint="responses",
                    provider=error.provider,
                    provider_model=error.provider_model_id,
                    status="error",
                    started_at=started_at,
                    error_class=error.reason,
                )
                circuits.record_failure(error.provider, error.provider_model_id, error.reason)
                _record_circuit_state(
                    observability,
                    circuits,
                    trace_id,
                    error.provider,
                    error.provider_model_id,
                )
                failed_attempts.append(
                    {
                        "provider": error.provider,
                        "provider_model": error.provider_model_id,
                        "status": "failed",
                        "reason": error.reason,
                    }
                )
                break

            billable_dispatches.append(BillableDispatch(candidate, response))
            if schema is None:
                return ResponseDispatchOutcome(
                    response,
                    candidate,
                    tuple(failed_attempts),
                    tuple(billable_dispatches),
                )

            validation_started_at = monotonic()
            validation = validate_output(response.output_text, schema)
            _record_structured_output_validation(
                observability=observability,
                trace_id=trace_id,
                candidate=candidate,
                retry_number=dispatch_index,
                valid=validation.valid,
                started_at=validation_started_at,
            )
            if validation.valid:
                return ResponseDispatchOutcome(
                    response,
                    candidate,
                    tuple(failed_attempts),
                    tuple(billable_dispatches),
                )

            validation_failures = validation.failures
            failed_attempts.append(
                {
                    "provider": candidate.provider,
                    "provider_model": candidate.provider_model_id,
                    "status": "failed",
                    "reason": "schema_validation_failed",
                }
            )
            if dispatch_index == 0:
                dispatch_request = corrective_request(request_body, validation.failures)

        _release_concurrency()

    raise AllCandidatesFailed(
        failed_attempts,
        billable_dispatches,
        validation_failures,
    )


def _dispatch_response_stream_with_fallback(
    adapter: Any,
    request_body: dict[str, Any],
    candidates,
    circuits: CircuitBreaker,
    quota: RedisQuotaService,
    observability: Observability,
    trace_id: str,
) -> tuple[ProviderStream, Any, list[dict[str, str]]]:
    failed_attempts: list[dict[str, str]] = []

    for candidate in candidates:
        if candidate.provider_connection_id and candidate.concurrency_limit:
            if not quota.reserve_concurrency(candidate.provider_connection_id, candidate.concurrency_limit):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "concurrency_limit_exceeded",
                    }
                )
                continue

        if candidate.provider_connection_id and candidate.request_quota:
            if not quota.reserve_connection_requests(candidate.provider_connection_id, candidate.request_quota):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "request_quota_exceeded",
                    }
                )
                continue

        concurrency_released = False
        def _release_concurrency(conn_id=candidate.provider_connection_id, limit=candidate.concurrency_limit) -> None:
            nonlocal concurrency_released
            if not concurrency_released and conn_id and limit:
                quota.release_concurrency(conn_id)
                concurrency_released = True

        started_at = monotonic()
        try:
            stream = _adapter_for(adapter, candidate).start_response_stream(
                request_body,
                candidate,
            )
            original_close = stream._close
            def wrapped_close():
                try:
                    original_close()
                finally:
                    _release_concurrency()
            stream._close = wrapped_close

            _record_provider_attempt(
                observability,
                trace_id,
                endpoint="responses",
                provider=candidate.provider,
                provider_model=candidate.provider_model_id,
                status="ok",
                started_at=started_at,
            )
            return stream, candidate, failed_attempts
        except ProviderDispatchError as error:
            _release_concurrency()
            _record_provider_attempt(
                observability,
                trace_id,
                endpoint="responses",
                provider=error.provider,
                provider_model=error.provider_model_id,
                status="error",
                started_at=started_at,
                error_class=error.reason,
            )
            circuits.record_failure(error.provider, error.provider_model_id, error.reason)
            _record_circuit_state(
                observability,
                circuits,
                trace_id,
                error.provider,
                error.provider_model_id,
            )
            failed_attempts.append(
                {
                    "provider": error.provider,
                    "provider_model": error.provider_model_id,
                    "status": "failed",
                    "reason": error.reason,
                }
            )
        except Exception:
            _release_concurrency()
            raise

    raise AllCandidatesFailed(failed_attempts)


def _native_response_event_stream(
    provider_stream: ProviderStream,
    selected_candidate: Any,
    failed_attempts: list[dict[str, str]],
    rejected_candidates,
    request_body: dict[str, Any],
    request_id: str,
    principal: Principal,
    reservation: Any,
    budget: BudgetService,
    quota: QuotaService,
    token_reservation: TokenReservation,
    circuits: CircuitBreaker,
    usage_ledger: Any,
    route_decision_store: Any,
    observability: Observability,
    configuration_version: int,
    trace_id: str,
):
    response_id = f"resp_{uuid.uuid4().hex}"
    output_chunks: list[str] = []
    try:
        yield encode_sse(
            "response.created",
            {
                "id": response_id,
                "model": request_body["model"],
                "provider": selected_candidate.provider,
            },
        )
        completion: ProviderStreamEvent | None = None
        for event in provider_stream:
            if event.delta is not None:
                output_chunks.append(event.delta)
                yield encode_sse("response.output_text.delta", {"delta": event.delta})
            if event.completed:
                completion = event
                break
        if completion is None:
            raise ProviderDispatchError(
                selected_candidate.provider,
                selected_candidate.provider_model_id,
                "invalid_upstream_response",
            )
    except GeneratorExit:
        provider_stream.close()
        _finalize_interrupted_stream(
            selected_candidate=selected_candidate,
            failed_attempts=failed_attempts,
            rejected_candidates=rejected_candidates,
            request_body=request_body,
            request_id=request_id,
            principal=principal,
            reservation=reservation,
            budget=budget,
            quota=quota,
            token_reservation=token_reservation,
            circuits=circuits,
            usage_ledger=usage_ledger,
            route_decision_store=route_decision_store,
            observability=observability,
            configuration_version=configuration_version,
            trace_id=trace_id,
            output_text="".join(output_chunks),
            reason="client_disconnected",
        )
        raise
    except ProviderDispatchError as error:
        _finalize_interrupted_stream(
            selected_candidate=selected_candidate,
            failed_attempts=failed_attempts,
            rejected_candidates=rejected_candidates,
            request_body=request_body,
            request_id=request_id,
            principal=principal,
            reservation=reservation,
            budget=budget,
            quota=quota,
            token_reservation=token_reservation,
            circuits=circuits,
            usage_ledger=usage_ledger,
            route_decision_store=route_decision_store,
            observability=observability,
            configuration_version=configuration_version,
            trace_id=trace_id,
            output_text="".join(output_chunks),
            reason=error.reason,
        )
        yield encode_sse(
            "response.failed",
            {
                "error": {
                    "type": "stream_interrupted",
                    "code": "provider_stream_interrupted",
                    "message": "The provider stream ended before completion.",
                }
            },
        )
        return

    output_text = "".join(output_chunks)
    input_tokens = completion.input_tokens
    if input_tokens is None:
        input_tokens = count_tokens(extract_text(request_body.get("input", "")))
    output_tokens = completion.output_tokens
    if output_tokens is None:
        output_tokens = count_tokens(output_text)
    cost = _estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_price=selected_candidate.pricing.get("input_per_million", "0"),
        output_price=selected_candidate.pricing.get("output_per_million", "0"),
    )
    usage_event = _response_usage_event(
        request_id=request_id,
        principal=principal,
        request_body=request_body,
        selected_candidate=selected_candidate,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        configuration_version=configuration_version,
    )
    _settle_accounting(
        budget,
        reservation,
        cost,
        usage_ledger,
        [usage_event],
    )
    quota.reconcile_tokens(token_reservation, input_tokens + output_tokens)
    circuits.record_success(selected_candidate.provider, selected_candidate.provider_model_id)
    _record_circuit_state(
        observability,
        circuits,
        trace_id,
        selected_candidate.provider,
        selected_candidate.provider_model_id,
    )
    attempts = _route_attempts(
        failed_attempts=failed_attempts,
        selected_provider=selected_candidate.provider,
        selected_provider_model=selected_candidate.provider_model_id,
    )
    route_decision_store.record(
        RouteDecisionRecord(
            request_id=request_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            endpoint="responses",
            logical_model=request_body["model"],
            status="succeeded",
            selected_provider=selected_candidate.provider,
            selected_provider_model=selected_candidate.provider_model_id,
            selected_provider_connection_id=selected_candidate.provider_connection_id,
            selected_provider_protocol=selected_candidate.protocol,
            attempts=tuple(attempts),
            rejected_candidates=tuple(rejected_candidates),
            configuration_version=configuration_version,
        )
    )
    response_payload = _response_payload(
        response_id=response_id,
        request_body=request_body,
        selected_candidate=selected_candidate,
        output_text=output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        failed_attempts=failed_attempts,
        rejected_candidates=rejected_candidates,
        request_id=request_id,
        configuration_version=configuration_version,
    )
    yield encode_sse("response.completed", {"response": response_payload})


def _finalize_interrupted_stream(
    selected_candidate: Any,
    failed_attempts: list[dict[str, str]],
    rejected_candidates,
    request_body: dict[str, Any],
    request_id: str,
    principal: Principal,
    reservation: Any,
    budget: BudgetService,
    quota: QuotaService,
    token_reservation: TokenReservation,
    circuits: CircuitBreaker,
    usage_ledger: Any,
    route_decision_store: Any,
    observability: Observability,
    configuration_version: int,
    trace_id: str,
    output_text: str,
    reason: str,
) -> None:
    input_tokens = count_tokens(extract_text(request_body.get("input", "")))
    output_tokens = count_tokens(output_text)
    cost = _estimate_cost(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_price=selected_candidate.pricing.get("input_per_million", "0"),
        output_price=selected_candidate.pricing.get("output_per_million", "0"),
    )
    usage_event = _response_usage_event(
        request_id=request_id,
        principal=principal,
        request_body=request_body,
        selected_candidate=selected_candidate,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=cost,
        configuration_version=configuration_version,
    )
    _settle_accounting(
        budget,
        reservation,
        cost,
        usage_ledger,
        [usage_event],
    )
    quota.reconcile_tokens(token_reservation, input_tokens + output_tokens)
    circuits.record_failure(
        selected_candidate.provider,
        selected_candidate.provider_model_id,
        reason,
    )
    _record_circuit_state(
        observability,
        circuits,
        trace_id,
        selected_candidate.provider,
        selected_candidate.provider_model_id,
    )
    if reason != "client_disconnected":
        observability.increment_counter(
            "mixapi_provider_errors_total",
            {"provider": selected_candidate.provider, "error_class": reason},
        )
        observability.record_span(
            "provider.stream",
            trace_id=trace_id,
            status="error",
            duration_ms=0,
            attributes={
                "endpoint": "responses",
                "provider": selected_candidate.provider,
                "provider_model": selected_candidate.provider_model_id,
                "error_class": reason,
            },
        )
    attempts = [
        *failed_attempts,
        {
            "provider": selected_candidate.provider,
            "provider_model": selected_candidate.provider_model_id,
            "status": "failed",
            "reason": reason,
        },
    ]
    route_decision_store.record(
        RouteDecisionRecord(
            request_id=request_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            endpoint="responses",
            logical_model=request_body["model"],
            status="failed",
            selected_provider=selected_candidate.provider,
            selected_provider_model=selected_candidate.provider_model_id,
            selected_provider_connection_id=selected_candidate.provider_connection_id,
            selected_provider_protocol=selected_candidate.protocol,
            attempts=tuple(attempts),
            rejected_candidates=tuple(rejected_candidates),
            configuration_version=configuration_version,
        )
    )


def _response_usage_event(
    request_id: str,
    principal: Principal,
    request_body: dict[str, Any],
    selected_candidate: Any,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
    configuration_version: int,
) -> UsageEvent:
    return UsageEvent(
        request_id=request_id,
        tenant_id=principal.tenant_id,
        project_id=principal.project_id,
        api_key_id=principal.api_key_id,
        endpoint="responses",
        logical_model=request_body["model"],
        provider=selected_candidate.provider,
        provider_model=selected_candidate.provider_model_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        provider_connection_id=selected_candidate.provider_connection_id,
        provider_protocol=selected_candidate.protocol,
        configuration_version=configuration_version,
    )


def _billable_dispatch_totals(
    dispatches: list[BillableDispatch] | tuple[BillableDispatch, ...],
) -> tuple[int, int, Decimal]:
    input_tokens = sum(dispatch.response.input_tokens for dispatch in dispatches)
    output_tokens = sum(dispatch.response.output_tokens for dispatch in dispatches)
    cost = sum(
        (
            _estimate_cost(
                input_tokens=dispatch.response.input_tokens,
                output_tokens=dispatch.response.output_tokens,
                input_price=dispatch.candidate.pricing.get("input_per_million", "0"),
                output_price=dispatch.candidate.pricing.get("output_per_million", "0"),
            )
            for dispatch in dispatches
        ),
        Decimal("0"),
    )
    return input_tokens, output_tokens, cost


def _billable_response_usage_events(
    dispatches: list[BillableDispatch] | tuple[BillableDispatch, ...],
    request_id: str,
    principal: Principal,
    logical_model: str,
    configuration_version: int,
) -> list[UsageEvent]:
    return [
        UsageEvent(
            request_id=request_id,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            api_key_id=principal.api_key_id,
            endpoint="responses",
            logical_model=logical_model,
            provider=dispatch.candidate.provider,
            provider_model=dispatch.candidate.provider_model_id,
            input_tokens=dispatch.response.input_tokens,
            output_tokens=dispatch.response.output_tokens,
            cost_usd=_estimate_cost(
                input_tokens=dispatch.response.input_tokens,
                output_tokens=dispatch.response.output_tokens,
                input_price=dispatch.candidate.pricing.get("input_per_million", "0"),
                output_price=dispatch.candidate.pricing.get("output_per_million", "0"),
            ),
            provider_connection_id=dispatch.candidate.provider_connection_id,
            provider_protocol=dispatch.candidate.protocol,
            configuration_version=configuration_version,
        )
        for dispatch in dispatches
    ]


def _settle_accounting(
    budget: BudgetService,
    reservation: Any,
    actual_cost_usd: Decimal,
    usage_ledger: Any,
    events: list[UsageEvent],
) -> None:
    budget.reconcile_with_usage(
        reservation,
        actual_cost_usd,
        usage_ledger,
        events,
    )


def _usage_write_intent(
    *,
    request_id: str,
    principal: Principal,
    endpoint: str,
    logical_model: str,
    reservation: Any,
    token_reservation: TokenReservation,
    configuration_version: int,
) -> UsageWriteIntent:
    return UsageWriteIntent(
        request_id=request_id,
        tenant_id=principal.tenant_id,
        project_id=principal.project_id,
        api_key_id=principal.api_key_id,
        endpoint=endpoint,
        logical_model=logical_model,
        configuration_version=configuration_version,
        reservation_data={
            "budget_reservation_id": reservation.reservation_id,
            "token_reservation_id": token_reservation.reservation_id,
        },
    )


def _response_payload(
    response_id: str,
    request_body: dict[str, Any],
    selected_candidate: Any,
    output_text: str,
    input_tokens: int,
    output_tokens: int,
    cost: Decimal,
    failed_attempts: list[dict[str, str]],
    rejected_candidates,
    request_id: str,
    configuration_version: int,
) -> dict[str, Any]:
    adapter_response = AdapterResponse(
        output_text=output_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return {
        "id": response_id,
        "object": "response",
        "model": request_body["model"],
        "provider": selected_candidate.provider,
        "output": _response_output(adapter_response),
        "output_text": output_text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": 0,
            "billable_units": [
                {"type": "input_tokens", "quantity": input_tokens},
                {"type": "output_tokens", "quantity": output_tokens},
            ],
        },
        "cost": {
            "provider_cost_usd": _format_money(cost),
            "platform_cost_usd": _format_money(cost),
        },
        "route": {
            "attempts": len(failed_attempts) + 1,
            "fallback_used": bool(failed_attempts),
            "configuration_version": configuration_version,
            "provider_connection_id": selected_candidate.provider_connection_id,
            "provider_name": selected_candidate.provider,
            "provider_protocol": selected_candidate.protocol,
            "selected_provider_model": selected_candidate.provider_model_id,
            "failed_attempts": failed_attempts,
            "decision_trace_id": request_id,
            "rejected_candidates": list(rejected_candidates),
        },
    }


def _dispatch_embedding_with_fallback(
    adapter: Any,
    request_body: dict[str, Any],
    candidates,
    circuits: CircuitBreaker,
    quota: RedisQuotaService,
    observability: Observability,
    trace_id: str,
) -> tuple[AdapterResponse, Any, list[dict[str, str]]]:
    failed_attempts: list[dict[str, str]] = []

    for candidate in candidates:
        if candidate.provider_connection_id and candidate.concurrency_limit:
            if not quota.reserve_concurrency(candidate.provider_connection_id, candidate.concurrency_limit):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "concurrency_limit_exceeded",
                    }
                )
                continue

        if candidate.provider_connection_id and candidate.request_quota:
            if not quota.reserve_connection_requests(candidate.provider_connection_id, candidate.request_quota):
                failed_attempts.append(
                    {
                        "provider": candidate.provider,
                        "model": candidate.provider_model_id,
                        "reason": "request_quota_exceeded",
                    }
                )
                continue

        concurrency_released = False
        def _release_concurrency() -> None:
            nonlocal concurrency_released
            if not concurrency_released and candidate.provider_connection_id and candidate.concurrency_limit:
                quota.release_concurrency(candidate.provider_connection_id)
                concurrency_released = True

        try:
            started_at = monotonic()
            try:
                response = _adapter_for(adapter, candidate).dispatch_embedding(
                    request_body,
                    candidate,
                )
                _record_provider_attempt(
                    observability,
                    trace_id,
                    endpoint="embeddings",
                    provider=candidate.provider,
                    provider_model=candidate.provider_model_id,
                    status="ok",
                    started_at=started_at,
                )
                circuits.record_success(candidate.provider, candidate.provider_model_id)
                _record_circuit_state(
                    observability,
                    circuits,
                    trace_id,
                    candidate.provider,
                    candidate.provider_model_id,
                )
                return response, candidate, failed_attempts
            except ProviderDispatchError as error:
                _record_provider_attempt(
                    observability,
                    trace_id,
                    endpoint="embeddings",
                    provider=error.provider,
                    provider_model=error.provider_model_id,
                    status="error",
                    started_at=started_at,
                    error_class=error.reason,
                )
                circuits.record_failure(error.provider, error.provider_model_id, error.reason)
                _record_circuit_state(
                    observability,
                    circuits,
                    trace_id,
                    error.provider,
                    error.provider_model_id,
                )
                failed_attempts.append(
                    {
                        "provider": error.provider,
                        "provider_model": error.provider_model_id,
                        "status": "failed",
                        "reason": error.reason,
                    }
                )
        finally:
            _release_concurrency()

    raise AllCandidatesFailed(failed_attempts)


def _adapter_for(adapter: Any, candidate: ProviderModel) -> Any:
    resolver = getattr(adapter, "for_candidate", None)
    return resolver(candidate) if resolver is not None else adapter


def _record_provider_attempt(
    observability: Observability,
    trace_id: str,
    endpoint: str,
    provider: str,
    provider_model: str,
    status: str,
    started_at: float,
    error_class: str | None = None,
) -> None:
    duration_ms = max((monotonic() - started_at) * 1000, 0)
    labels = {"provider": provider, "provider_model": provider_model}
    observability.observe_histogram("mixapi_provider_latency_ms", duration_ms, labels)
    attributes = {"endpoint": endpoint, **labels}
    if error_class is not None:
        attributes["error_class"] = error_class
        observability.increment_counter(
            "mixapi_provider_errors_total",
            {"provider": provider, "error_class": error_class},
        )
    observability.record_span(
        "provider.dispatch",
        trace_id=trace_id,
        status=status,
        duration_ms=duration_ms,
        attributes=attributes,
    )


def _record_structured_output_validation(
    observability: Observability,
    trace_id: str,
    candidate: ProviderModel,
    retry_number: int,
    valid: bool,
    started_at: float,
) -> None:
    attributes: dict[str, str | int] = {
        "endpoint": "responses",
        "provider": candidate.provider,
        "provider_model": candidate.provider_model_id,
        "retry_number": retry_number,
    }
    if not valid:
        attributes["error_class"] = "schema_validation_failed"
        observability.increment_counter(
            "mixapi_structured_output_failures_total",
            {
                "provider": candidate.provider,
                "provider_model": candidate.provider_model_id,
            },
        )
    observability.record_span(
        "structured_output.validate",
        trace_id=trace_id,
        status="ok" if valid else "error",
        duration_ms=max((monotonic() - started_at) * 1000, 0),
        attributes=attributes,
    )


def _record_fallback_transitions(
    observability: Observability,
    trace_id: str,
    principal: Principal,
    logical_model: str,
    failed_attempts: list[dict[str, str]],
    selected_candidate: Any | None = None,
) -> None:
    destinations = [attempt["provider"] for attempt in failed_attempts[1:]]
    if selected_candidate is not None:
        destinations.append(selected_candidate.provider)
    for failed_attempt, destination in zip(failed_attempts, destinations, strict=False):
        attributes = {
            "tenant": principal.tenant_id,
            "model": logical_model,
            "from_provider": failed_attempt["provider"],
            "to_provider": destination,
            "reason": failed_attempt["reason"],
        }
        observability.increment_counter("mixapi_fallbacks_total", attributes)
        observability.record_span(
            "routing.fallback",
            trace_id=trace_id,
            status="ok",
            duration_ms=0,
            attributes=attributes,
        )


def _record_circuit_state(
    observability: Observability,
    circuits: CircuitBreaker,
    trace_id: str,
    provider: str,
    provider_model: str,
) -> None:
    is_open = circuits.is_open(provider, provider_model)
    _record_circuit_observation(
        observability,
        trace_id,
        provider,
        provider_model,
        is_open,
    )


def _record_circuit_observation(
    observability: Observability,
    trace_id: str,
    provider: str,
    provider_model: str,
    is_open: bool,
) -> None:
    observability.set_gauge(
        "mixapi_circuit_state",
        int(is_open),
        {"provider": provider, "provider_model": provider_model},
    )
    observability.record_span(
        "circuit.state",
        trace_id=trace_id,
        status="error" if is_open else "ok",
        duration_ms=0,
        attributes={
            "provider": provider,
            "provider_model": provider_model,
            "state": "open" if is_open else "closed",
        },
    )


def _route_attempts(
    failed_attempts: list[dict[str, str]],
    selected_provider: str,
    selected_provider_model: str,
) -> list[dict[str, str]]:
    return [
        *failed_attempts,
        {
            "provider": selected_provider,
            "provider_model": selected_provider_model,
            "status": "succeeded",
        },
    ]


def _response_output(adapter_response: AdapterResponse) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if adapter_response.output_text:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": adapter_response.output_text}],
            }
        )
    output.extend(adapter_response.tool_calls)
    return output


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_price: object,
    output_price: object,
) -> Decimal:
    input_cost = (Decimal(str(input_price)) * Decimal(input_tokens)) / Decimal(1_000_000)
    output_cost = (Decimal(str(output_price)) * Decimal(output_tokens)) / Decimal(1_000_000)
    return (input_cost + output_cost).quantize(Decimal("0.00000001"))


def _format_money(value: Decimal) -> str:
    return format(value, "f")


_PUBLIC_SCOPES = {
    "models:read",
    "responses:create",
    "embeddings:create",
    "usage:read",
    "route-decisions:read",
}

_ROUTING_OBJECTIVES = {
    "balanced",
    "lowest-cost",
    "lowest-latency",
    "highest-reliability",
}


def _parse_api_key_create(
    request_body: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    allowed_fields = {
        "tenant_id",
        "project_id",
        "name",
        "scopes",
        "model_allowlist",
        "budget_limit_usd",
        "expires_at",
    }
    if unknown := set(request_body).difference(allowed_fields):
        raise validation_error(
            "invalid_api_key_payload",
            f"Unsupported API key fields: {', '.join(sorted(unknown))}.",
        )
    return {
        "tenant_id": _validate_identifier(request_body.get("tenant_id"), "tenant_id"),
        "project_id": _validate_identifier(request_body.get("project_id"), "project_id"),
        "name": _parse_optional_name(request_body.get("name")),
        "scopes": _parse_scopes(
            request_body.get(
                "scopes",
                ["models:read", "responses:create", "embeddings:create"],
            )
        ),
        "model_allowlist": _parse_model_allowlist(
            request_body.get("model_allowlist", []),
            catalog,
        ),
        "budget_limit_usd": _parse_budget_limit(request_body.get("budget_limit_usd")),
        "expires_at": _parse_expiry(request_body.get("expires_at")),
    }


def _parse_api_key_patch(
    request_body: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    allowed_fields = {
        "name",
        "scopes",
        "model_allowlist",
        "budget_limit_usd",
        "expires_at",
        "status",
    }
    if not request_body or set(request_body).difference(allowed_fields):
        raise validation_error(
            "invalid_api_key_payload",
            "API key patch contains no supported fields.",
        )
    changes: dict[str, Any] = {}
    if "name" in request_body:
        changes["name"] = _parse_optional_name(request_body["name"])
    if "scopes" in request_body:
        changes["scopes"] = _parse_scopes(request_body["scopes"])
    if "model_allowlist" in request_body:
        changes["model_allowlist"] = _parse_model_allowlist(
            request_body["model_allowlist"],
            catalog,
        )
    if "budget_limit_usd" in request_body:
        changes["budget_limit_usd"] = _parse_budget_limit(
            request_body["budget_limit_usd"]
        )
    if "expires_at" in request_body:
        changes["expires_at"] = _parse_expiry(request_body["expires_at"])
    if "status" in request_body:
        if request_body["status"] not in {"active", "revoked"}:
            raise validation_error("invalid_api_key_status", "Unsupported API key status.")
        changes["status"] = request_body["status"]
    return changes


def _validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise validation_error(
            f"invalid_{field}",
            f"`{field}` must be a non-empty string.",
        )
    return value.strip()


def _parse_optional_name(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise validation_error("invalid_api_key_name", "API key name must be a string.")
    return value.strip()


def _parse_scopes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise validation_error("invalid_scopes", "Scopes must be a list of strings.")
    scopes = tuple(dict.fromkeys(value))
    if unknown := set(scopes).difference(_PUBLIC_SCOPES):
        raise validation_error(
            "invalid_scopes",
            f"Unsupported scopes: {', '.join(sorted(unknown))}.",
        )
    return scopes


def _parse_model_allowlist(
    value: object,
    catalog: dict[str, Any],
) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise validation_error(
            "invalid_model_allowlist",
            "Model allowlist must be a list of model IDs.",
        )
    models = tuple(dict.fromkeys(value))
    if unknown := set(models).difference(catalog):
        raise validation_error(
            "invalid_model_allowlist",
            f"Unknown logical models: {', '.join(sorted(unknown))}.",
        )
    return models


def _parse_budget_limit(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise validation_error(
            "invalid_budget_limit_usd",
            "Budget limit must be a positive decimal amount.",
        )
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        amount = Decimal("NaN")
    if not amount.is_finite() or amount <= 0:
        raise validation_error(
            "invalid_budget_limit_usd",
            "Budget limit must be a positive decimal amount.",
        )
    return amount


def _parse_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise validation_error("invalid_expires_at", "Expiry must be an RFC3339 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise validation_error(
            "invalid_expires_at",
            "Expiry must be an RFC3339 timestamp.",
        ) from error
    if parsed.tzinfo is None:
        raise validation_error("invalid_expires_at", "Expiry must include a timezone.")
    normalized = parsed.astimezone(timezone.utc)
    if normalized <= datetime.now(timezone.utc):
        raise validation_error("invalid_expires_at", "Expiry must be in the future.")
    return normalized


def _parse_routing_objective(value: object) -> str | None:
    if value is None:
        return None
    if value not in _ROUTING_OBJECTIVES:
        raise validation_error(
            "invalid_routing_objective",
            "Unsupported routing objective.",
        )
    return str(value)
