import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Barrier

from fastapi.testclient import TestClient

from mixapi import persistence
from mixapi.app import create_app
from mixapi.auth import Principal
from mixapi.errors import MixAPIError


AUTH_HEADERS = {"Authorization": "Bearer dev-key"}


class PersistenceTest(unittest.TestCase):
    def test_circuit_failures_are_shared_between_app_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            first_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=1,
            )
            second_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=1,
            )

            first_app.state.circuits.record_failure(
                "openai",
                "gpt-shared",
                "upstream_timeout",
            )

            self.assertTrue(second_app.state.circuits.is_open("openai", "gpt-shared"))

    def test_circuit_failures_accumulate_across_app_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            first_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=2,
            )
            second_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=2,
            )

            first_app.state.circuits.record_failure(
                "openai",
                "gpt-shared",
                "upstream_timeout",
            )
            self.assertFalse(second_app.state.circuits.is_open("openai", "gpt-shared"))

            second_app.state.circuits.record_failure(
                "openai",
                "gpt-shared",
                "upstream_timeout",
            )

            self.assertTrue(first_app.state.circuits.is_open("openai", "gpt-shared"))

    def test_circuit_success_clears_state_for_other_app_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            first_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=1,
            )
            second_app = create_app(
                database_path=database_path,
                circuit_failure_threshold=1,
            )
            first_app.state.circuits.record_failure(
                "openai",
                "gpt-shared",
                "upstream_timeout",
            )

            second_app.state.circuits.record_success("openai", "gpt-shared")

            self.assertFalse(first_app.state.circuits.is_open("openai", "gpt-shared"))

    def test_persistent_circuit_recovers_after_timeout(self) -> None:
        breaker_type = getattr(persistence, "SQLiteCircuitBreaker", None)
        self.assertIsNotNone(breaker_type)
        with tempfile.TemporaryDirectory() as directory:
            database = persistence.SQLiteDatabase(Path(directory) / "mixapi.db")
            current_time = [100.0]
            first = breaker_type(
                database,
                failure_threshold=1,
                recovery_timeout_seconds=10,
                now=lambda: current_time[0],
            )
            second = breaker_type(
                database,
                failure_threshold=1,
                recovery_timeout_seconds=10,
                now=lambda: current_time[0],
            )
            first.record_failure("openai", "gpt-shared", "upstream_timeout")
            self.assertTrue(second.is_open("openai", "gpt-shared"))

            current_time[0] = 111.0

            self.assertFalse(second.is_open("openai", "gpt-shared"))
            self.assertFalse(first.is_open("openai", "gpt-shared"))

    def test_budget_reservations_are_shared_between_app_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            principal = Principal(
                tenant_id="tenant_dev",
                project_id="project_dev",
                api_key_id="key_dev",
                scopes=("responses:create",),
            )
            first_app = create_app(database_path=database_path, budget_limit_usd="1.00")
            second_app = create_app(database_path=database_path, budget_limit_usd="1.00")

            first_app.state.budget.reserve(principal, Decimal("0.60"))

            with self.assertRaises(MixAPIError) as raised:
                second_app.state.budget.reserve(principal, Decimal("0.50"))

            self.assertEqual(raised.exception.code, "api_key_budget_exceeded")

    def test_concurrent_budget_reservations_do_not_oversubscribe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            principal = Principal(
                tenant_id="tenant_dev",
                project_id="project_dev",
                api_key_id="key_dev",
                scopes=("responses:create",),
            )
            first_budget = create_app(
                database_path=database_path,
                budget_limit_usd="1.00",
            ).state.budget
            second_budget = create_app(
                database_path=database_path,
                budget_limit_usd="1.00",
            ).state.budget
            barrier = Barrier(2)

            def reserve(budget) -> str:
                barrier.wait()
                try:
                    budget.reserve(principal, Decimal("0.60"))
                except MixAPIError:
                    return "denied"
                return "reserved"

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, (first_budget, second_budget)))

            self.assertCountEqual(results, ["reserved", "denied"])

    def test_releasing_one_budget_reservation_preserves_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            principal = Principal(
                tenant_id="tenant_dev",
                project_id="project_dev",
                api_key_id="key_dev",
                scopes=("responses:create",),
            )
            first_budget = create_app(
                database_path=database_path,
                budget_limit_usd="1.00",
            ).state.budget
            released = first_budget.reserve(principal, Decimal("0.40"))
            first_budget.reserve(principal, Decimal("0.40"))

            second_budget = create_app(
                database_path=database_path,
                budget_limit_usd="1.00",
            ).state.budget
            second_budget.release(released)
            second_budget.reserve(principal, Decimal("0.50"))

            with self.assertRaises(MixAPIError) as raised:
                second_budget.reserve(principal, Decimal("0.20"))

            self.assertEqual(raised.exception.code, "api_key_budget_exceeded")

    def test_budget_reconciliation_survives_application_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            principal = Principal(
                tenant_id="tenant_dev",
                project_id="project_dev",
                api_key_id="key_dev",
                scopes=("responses:create",),
            )
            first_app = create_app(database_path=database_path, budget_limit_usd="1.00")
            reservation = first_app.state.budget.reserve(principal, Decimal("0.60"))

            second_app = create_app(database_path=database_path, budget_limit_usd="1.00")
            second_app.state.budget.reconcile(reservation, Decimal("0.50"))

            third_app = create_app(database_path=database_path, budget_limit_usd="1.00")
            with self.assertRaises(MixAPIError) as raised:
                third_app.state.budget.reserve(principal, Decimal("0.60"))

            self.assertEqual(raised.exception.code, "api_key_budget_exceeded")

    def test_usage_events_survive_application_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            first_app = create_app(database_path=database_path)
            first_client = TestClient(first_app)

            response = first_client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_persist_usage"},
                json={"model": "mixapi/balanced-chat", "input": "Persist usage"},
            )

            second_app = create_app(database_path=database_path)

            self.assertEqual(response.status_code, 200)
            events = second_app.state.usage_ledger.events()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].request_id, "req_persist_usage")
            self.assertEqual(events[0].tenant_id, "tenant_dev")

    def test_idempotency_replays_after_application_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            headers = {**AUTH_HEADERS, "Idempotency-Key": "persist-idem"}
            payload = {"model": "mixapi/balanced-chat", "input": "Persist response"}

            first_client = TestClient(create_app(database_path=database_path))
            first = first_client.post("/v1/responses", headers=headers, json=payload)

            second_app = create_app(database_path=database_path)
            second_client = TestClient(second_app)
            second = second_client.post("/v1/responses", headers=headers, json=payload)

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.json(), first.json())
            self.assertEqual(len(second_app.state.usage_ledger.events()), 1)

    def test_route_decision_survives_application_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "mixapi.db"
            first_client = TestClient(create_app(database_path=database_path))

            created = first_client.post(
                "/v1/responses",
                headers={**AUTH_HEADERS, "X-Request-ID": "req_persist_route"},
                json={"model": "mixapi/balanced-chat", "input": "Persist route"},
            )

            second_client = TestClient(create_app(database_path=database_path))
            route = second_client.get(
                "/v1/route-decisions/req_persist_route",
                headers=AUTH_HEADERS,
            )

            self.assertEqual(created.status_code, 200)
            self.assertEqual(route.status_code, 200)
            self.assertEqual(route.json()["request_id"], "req_persist_route")
            self.assertEqual(route.json()["status"], "succeeded")

    def test_persistent_idempotency_record_is_first_write_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(database_path=Path(directory) / "mixapi.db")
            store = app.state.idempotency_store
            principal = Principal(
                tenant_id="tenant_dev",
                project_id="project_dev",
                api_key_id="key_dev",
                scopes=("responses:create",),
            )
            first_body = {"model": "mixapi/balanced-chat", "input": "First"}
            second_body = {"model": "mixapi/balanced-chat", "input": "Second"}
            first_response = {"id": "resp_first"}

            store.store(principal, "responses", "same-key", first_body, first_response)
            store.store(principal, "responses", "same-key", second_body, {"id": "resp_second"})

            replay = store.replay(principal, "responses", "same-key", first_body)

            self.assertIsNotNone(replay)
            self.assertEqual(replay.response, first_response)


if __name__ == "__main__":
    unittest.main()
