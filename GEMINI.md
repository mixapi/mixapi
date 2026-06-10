# MixAPI Unified LLM Gateway

MixAPI is a unified LLM gateway providing a stable, OpenAI-compatible API edge that routes requests across multiple LLM providers (OpenAI, Anthropic, Gemini, Ollama). It manages model metadata, provider credentials, routing policies, quotas, budgets, and observability.

## Technical Stack

- **Language:** Python 3.14+
- **Framework:** FastAPI, Pydantic v2
- **Database:** PostgreSQL (SQLAlchemy 2.0, Alembic migrations)
- **Caching/Hot Path:** Redis (quotas, counters, config snapshots, circuit breakers)
- **HTTP Client:** HTTPX
- **Schema Validation:** JSON Schema (`jsonschema`)
- **Web Server:** Uvicorn

## Core Architecture

The project is structured as a **modular monolith** with clear boundaries designed for future service extraction.

### Components

1.  **Public API Edge (`mixapi/app.py`):** Handles auth, validation, normalization, and streaming.
2.  **Control Plane (`mixapi/admin/`):** Manages configuration (tenants, providers, models) and publishes snapshots to Redis.
3.  **Route Planner (`mixapi/routing.py`):** Filters and scores candidate models based on cost, latency, and reliability.
4.  **Adapter Runtime (`mixapi/adapters.py`, `mixapi/adapter_factory.py`):** Translates between internal requests and provider-native APIs.
5.  **Quota & Budget Service (`mixapi/quota.py`, `mixapi/budget.py`):** Enforces request, token, and spend limits.
6.  **Usage Ledger (`mixapi/usage.py`):** Append-only record of request economics and routing decisions.
7.  **Observability (`mixapi/observability.py`):** Structured logs, metrics, and traces for every request.

## Development Workflow

### Prerequisites

- Python 3.14+
- PostgreSQL
- Redis

### Setup and Running

1.  **Install Dependencies:**
    ```bash
    pip install -e .
    ```
2.  **Environment Variables:**
    Copy `.env.example` to `.env` and configure `MIXAPI_DATABASE_URL` and `MIXAPI_REDIS_URL`.
3.  **Run Migrations:**
    ```bash
    python scripts/migrate.py upgrade
    ```
4.  **Run Server:**
    ```bash
    uvicorn mixapi.app:app --reload --port 8000
    ```

### Testing

The project uses `pytest` for all levels of testing.

- **Run all tests:**
  ```bash
  pytest
  ```
- **Run specific test file:**
  ```bash
  pytest tests/test_routing.py
  ```

### Key Commands

- **Generate OpenAPI Spec:** `python scripts/generate_openapi.py`
- **Wait for Dependencies:** `python scripts/wait_for_dependencies.py`
- **Bootstrap Configuration:** `python scripts/bootstrap_configuration.py --seed <path>`

## Core Concepts & Rules

- **Stateless Data Path:** The hot path (`/v1/responses`, `/v1/embeddings`) must remain stateless, reading from Redis-backed snapshots.
- **Provider Consistency, Not Equivalence:** MixAPI provides transport and operational consistency. It does NOT attempt to mask all semantic differences between models.
- **Capability Gating:** Always check model capabilities (context window, tool support, etc.) before dispatching to a provider.
- **Fail-Closed:** Production defaults for auth, policy, and budget checks should be fail-closed.
- **No Prompt Persistence:** By default, do not persist prompt or completion bodies in logs or the database.

## Directory Structure

- `mixapi/`: Core application logic.
  - `adapters/`: Provider-specific implementation.
  - `repositories/`: Persistence layer (Postgres/Redis).
  - `runtime/`: Hot-path logic.
  - `admin/`: Control-plane API routers.
- `alembic/`: Database schema migrations.
- `docs/plans/`: Detailed design documents.
- `sdk-fixtures/`: JSON fixtures for contract testing.
- `tests/`: Comprehensive test suite.
