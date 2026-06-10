import json
import unittest
from pathlib import Path

from mixapi.api_contract import CONTRACT_MODELS, OPERATION_CONTRACTS


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "sdk-fixtures"


class SDKFixturesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads((FIXTURE_ROOT / "manifest.json").read_text())

    def test_manifest_entries_resolve_and_validate(self) -> None:
        operation_ids = {
            contract.operation_id for contract in OPERATION_CONTRACTS.values()
        }

        for entry in self.manifest["fixtures"]:
            with self.subTest(entry=entry):
                self.assertIn(entry["operation_id"], operation_ids)
                self.assertIn(entry["direction"], {"request", "response", "event", "error"})
                model = CONTRACT_MODELS[entry["model"]]
                fixture_path = FIXTURE_ROOT / entry["file"]
                self.assertTrue(fixture_path.is_file())
                model.model_validate_json(fixture_path.read_text())

    def test_every_json_operation_model_has_a_fixture(self) -> None:
        coverage = {
            (entry["operation_id"], entry["direction"], entry["model"])
            for entry in self.manifest["fixtures"]
        }

        for contract in OPERATION_CONTRACTS.values():
            if contract.request_model is not None:
                self.assertIn(
                    (contract.operation_id, "request", contract.request_model),
                    coverage,
                )
            if contract.response_model is not None:
                self.assertIn(
                    (contract.operation_id, "response", contract.response_model),
                    coverage,
                )

    def test_streaming_and_normalized_error_fixtures_are_covered(self) -> None:
        coverage = {
            (entry["operation_id"], entry["direction"], entry["model"])
            for entry in self.manifest["fixtures"]
        }
        for model_name in (
            "ResponseCreatedEvent",
            "ResponseOutputTextDeltaEvent",
            "ResponseCompletedEvent",
            "ResponseErrorEvent",
        ):
            self.assertIn(("createResponse", "event", model_name), coverage)
        self.assertIn(("createResponse", "error", "ErrorEnvelope"), coverage)


if __name__ == "__main__":
    unittest.main()
