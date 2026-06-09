import unittest

from fastapi.testclient import TestClient

from mixapi.app import create_app


class AuthAndErrorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_app())

    def test_missing_api_key_returns_normalized_error(self) -> None:
        response = self.client.get("/v1/models")

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["error"]["type"], "authentication_failed")
        self.assertEqual(body["error"]["code"], "missing_api_key")
        self.assertIn("request_id", body["error"])
        self.assertIn("trace_id", body["error"])

    def test_invalid_api_key_returns_normalized_error(self) -> None:
        response = self.client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body["error"]["type"], "authentication_failed")
        self.assertEqual(body["error"]["code"], "invalid_api_key")
        self.assertIn("request_id", body["error"])
        self.assertIn("trace_id", body["error"])


if __name__ == "__main__":
    unittest.main()
