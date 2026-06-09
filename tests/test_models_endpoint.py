import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app


class ModelsEndpointTest(unittest.TestCase):
    def test_authenticated_client_lists_capability_rich_models(self) -> None:
        client = TestClient(create_app())

        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer dev-key"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "list")
        self.assertGreaterEqual(len(body["data"]), 1)

        model = body["data"][0]
        self.assertIn("id", model)
        self.assertIn("context_window_tokens", model)
        self.assertIn("input_modalities", model)
        self.assertIn("output_modalities", model)
        self.assertIn("schema_support", model)
        self.assertIn("providers", model)


if __name__ == "__main__":
    unittest.main()
