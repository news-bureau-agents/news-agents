import unittest
from unittest.mock import Mock, patch

import requests

from server import AUTH_TIMEOUT, READY_TIMEOUT, create_app


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
            }
        )
        self.client = self.app.test_client()

    @patch("server.requests.get")
    def test_missing_authorization_is_rejected_without_upstream_call(self, get):
        response = self.client.get("/api/me")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Unauthorized"})
        get.assert_not_called()

    @patch("server.requests.get")
    def test_malformed_authorization_is_rejected_without_upstream_call(self, get):
        for value in ("Basic abc", "Bearer", "Bearer token with spaces"):
            with self.subTest(value=value):
                response = self.client.get(
                    "/api/me", headers={"Authorization": value}
                )
                self.assertEqual(response.status_code, 401)
        get.assert_not_called()

    @patch("server.requests.get")
    def test_invalid_token_rejected_by_supabase_is_unauthorized(self, get):
        get.return_value = Mock(status_code=401)

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer invalid-token"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Unauthorized"})

    @patch("server.requests.get")
    def test_expired_token_rejected_by_supabase_is_unauthorized(self, get):
        get.return_value = Mock(status_code=403)

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer expired-token"}
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Unauthorized"})

    @patch("server.requests.get")
    def test_unexpected_upstream_rejection_is_service_unavailable(self, get):
        get.return_value = Mock(status_code=500)

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer opaque-token"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Upstream unavailable"})

    @patch("server.requests.get")
    def test_upstream_network_failure_is_service_unavailable(self, get):
        get.side_effect = requests.Timeout("timed out")

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer opaque-token"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Upstream unavailable"})

    @patch("server.requests.get")
    def test_valid_token_returns_supabase_user(self, get):
        upstream = Mock(status_code=200)
        upstream.json.return_value = {
            "id": "user-123",
            "email": "member@example.com",
            "user_metadata": {"display_name": "Oasis member"},
        }
        get.return_value = upstream

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer valid-token"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["id"], "user-123")
        get.assert_called_once_with(
            "https://project.supabase.co/auth/v1/user",
            headers={
                "apikey": "sb_publishable_test",
                "Authorization": "Bearer valid-token",
            },
            timeout=AUTH_TIMEOUT,
        )

    @patch("server.requests.get")
    def test_non_json_success_is_treated_as_upstream_failure(self, get):
        upstream = Mock(status_code=200)
        upstream.json.side_effect = ValueError("not json")
        get.return_value = upstream

        response = self.client.get(
            "/api/me", headers={"Authorization": "Bearer opaque-token"}
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Upstream unavailable"})


class ServiceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test",
            }
        )
        self.client = self.app.test_client()

    def test_public_config_and_security_headers(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "supabaseUrl": "https://project.supabase.co",
                "supabasePublishableKey": "sb_publishable_test",
            },
        )
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn(
            "connect-src 'self' https://project.supabase.co",
            response.headers["Content-Security-Policy"],
        )
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")

    def test_privileged_and_malformed_keys_are_never_published(self):
        import base64
        import json
        service_role = base64.urlsafe_b64encode(json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        for key in ("sb_secret_example", f"header.{service_role}.signature", "invalid", "x.@@@.x"):
            with self.subTest(key_type=key.split(".")[0]):
                self.app.config["SUPABASE_PUBLISHABLE_KEY"] = key
                response = self.client.get("/api/config")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn(key, response.get_data(as_text=True))

    def test_legacy_anon_key_is_allowed(self):
        import base64
        import json
        payload = base64.urlsafe_b64encode(json.dumps({"role": "anon"}).encode()).decode().rstrip("=")
        self.app.config["SUPABASE_PUBLISHABLE_KEY"] = f"header.{payload}.signature"
        self.assertEqual(self.client.get("/api/config").status_code, 200)

    def test_unsafe_supabase_urls_are_not_published(self):
        for url in ("http://project.supabase.co", "https://user:pass@project.supabase.co", "https://project.supabase.co/path", "https://project.supabase.co?bad", "https://project.supabase.co; evil"):
            self.app.config["SUPABASE_URL"] = url
            self.assertEqual(self.client.get("/api/config").status_code, 503)

    def test_form_never_falls_back_to_credentials_in_query_string(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('<form id="login-form" method="post" action="/">', html)
        self.assertEqual(self.client.post("/", data={"password": "test"}).status_code, 405)

    def test_https_sets_hsts(self):
        response = self.client.get("/healthz", base_url="https://oasis.example.com")
        self.assertIn("max-age=31536000", response.headers["Strict-Transport-Security"])

    def test_liveness_does_not_call_supabase(self):
        with patch("server.requests.get") as get:
            response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        get.assert_not_called()

    @patch("server.requests.get")
    def test_readiness_checks_supabase_with_bounded_timeout(self, get):
        get.return_value = Mock(status_code=200)

        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ready"})
        get.assert_called_once_with(
            "https://project.supabase.co/auth/v1/health",
            headers={"apikey": "sb_publishable_test"},
            timeout=READY_TIMEOUT,
        )

    @patch("server.requests.get")
    def test_readiness_reports_upstream_failure(self, get):
        get.side_effect = requests.ConnectionError("offline")

        response = self.client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Upstream unavailable"})

    def test_missing_configuration_is_not_ready(self):
        app = create_app(
            {
                "TESTING": True,
                "SUPABASE_URL": "",
                "SUPABASE_PUBLISHABLE_KEY": "",
            }
        )

        response = app.test_client().get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Service unavailable"})


if __name__ == "__main__":
    unittest.main()
