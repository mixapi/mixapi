# OpenAPI Contract And SDK Fixtures Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Publish and validate an explicit OpenAPI 3.1 contract plus language-neutral SDK conformance fixtures for all exposed MixAPI endpoints.

**Architecture:** Define contract-only Pydantic models and assemble them into FastAPI's route document without changing handler validation. Check in the deterministic OpenAPI artifact and validate manifest-driven JSON fixtures against the same model registry.

**Tech Stack:** Python 3.14, FastAPI, Pydantic v2, standard-library `json`, `pathlib`, and `unittest`.

### Task 1: Contract Test Harness

**Files:**
- Create: `tests/test_openapi_contract.py`
- Create: `mixapi/api_contract.py`

1. Write failing tests asserting stable operation IDs, explicit request/response component references, bearer security schemes, documented SSE/export media types, and a normalized error component.
2. Run `python -m unittest tests.test_openapi_contract -v` and confirm failures are caused by the current generic OpenAPI document.
3. Add the minimal contract model registry and OpenAPI builder needed to pass the focused assertions.
4. Install the builder in `create_app()` and rerun the focused tests.
5. Commit with `feat: define explicit OpenAPI contract`.

### Task 2: Published Contract Artifact

**Files:**
- Create: `scripts/generate_openapi.py`
- Create: `openapi/openapi.json`
- Modify: `tests/test_openapi_contract.py`

1. Add a failing test that compares deterministic JSON generated from `create_app().openapi()` with `openapi/openapi.json`.
2. Run the test and confirm it fails because the artifact does not exist.
3. Add a small generation script and produce the artifact.
4. Rerun the focused tests and verify the artifact is byte-for-byte current.
5. Commit with `build: publish OpenAPI contract`.

### Task 3: SDK Fixture Corpus

**Files:**
- Create: `sdk-fixtures/manifest.json`
- Create: `sdk-fixtures/**/*.json`
- Create: `tests/test_sdk_fixtures.py`
- Modify: `mixapi/api_contract.py`

1. Add failing tests that load every manifest entry, resolve its contract model, validate its JSON payload, and require fixture coverage for every JSON request/response model referenced by an operation.
2. Run `python -m unittest tests.test_sdk_fixtures -v` and confirm the missing corpus fails.
3. Add representative request, response, error, and streaming-event fixtures plus a public model-validation registry.
4. Rerun fixture and contract tests until all examples validate.
5. Commit with `test: add SDK contract fixtures`.

### Task 4: Completion And Regression Verification

**Files:**
- Modify: `TASKS.md`

1. Check off the OpenAPI and SDK fixtures backlog item.
2. Run `python -m unittest discover -s tests -v` and confirm all tests pass.
3. Run `python -m compileall -q mixapi scripts tests` and `git diff --check`.
4. Review the branch diff for accidental runtime behavior changes and contract omissions.
5. Commit with `docs: complete OpenAPI contract task`.
