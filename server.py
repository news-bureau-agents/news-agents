"""Oasis login demo server.

The browser signs in directly with Supabase. This server only publishes the
public Supabase configuration and validates bearer tokens online with Supabase.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Flask, Response, jsonify, render_template, request

AUTH_TIMEOUT = (2.0, 5.0)
READY_TIMEOUT = (2.0, 3.0)
BEARER_PATTERN = re.compile(r"^Bearer ([^\s]+)$", re.IGNORECASE)


def _supabase_origin(url: str) -> str | None:
    """Return a CSP-safe HTTP(S) origin, or None for invalid configuration."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in url)
    ):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _public_key(key: str) -> bool:
    """Prevent accidental publication of privileged configuration, not JWT auth."""
    if key.startswith("sb_publishable_"):
        return True
    try:
        parts = key.split(".")
        if len(parts) != 3:
            return False
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return isinstance(payload, dict) and payload.get("role") == "anon"
    except (ValueError, UnicodeError, binascii.Error):
        return False


def _error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        SUPABASE_URL=os.environ.get("SUPABASE_URL", "").rstrip("/"),
        SUPABASE_PUBLISHABLE_KEY=os.environ.get("SUPABASE_PUBLISHABLE_KEY", ""),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    # Normalize injected test/runtime configuration in the same way as env data.
    app.config["SUPABASE_URL"] = str(app.config.get("SUPABASE_URL", "")).rstrip("/")
    app.config["SUPABASE_PUBLISHABLE_KEY"] = str(
        app.config.get("SUPABASE_PUBLISHABLE_KEY", "")
    )

    def configured() -> bool:
        return bool(
            _supabase_origin(app.config["SUPABASE_URL"])
            and _public_key(app.config["SUPABASE_PUBLISHABLE_KEY"])
        )

    @app.after_request
    def security_headers(response: Response) -> Response:
        origin = _supabase_origin(app.config["SUPABASE_URL"])
        connect_sources = "'self'" + (f" {origin}" if origin else "")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "base-uri 'none'; "
            f"connect-src {connect_sources}; "
            "font-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "img-src 'self' data:; "
            "object-src 'none'; "
            "script-src 'self'; "
            "style-src 'self'"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.is_secure:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/api/config")
    def public_config() -> tuple[Response, int] | Response:
        if not configured():
            return _error("Service unavailable", 503)
        return jsonify(
            {
                "supabaseUrl": app.config["SUPABASE_URL"],
                "supabasePublishableKey": app.config["SUPABASE_PUBLISHABLE_KEY"],
            }
        )

    @app.get("/healthz")
    def health() -> Response:
        # Liveness deliberately has no dependency on Supabase.
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readiness() -> tuple[Response, int] | Response:
        if not configured():
            return _error("Service unavailable", 503)
        try:
            upstream = requests.get(
                f"{app.config['SUPABASE_URL']}/auth/v1/health",
                headers={"apikey": app.config["SUPABASE_PUBLISHABLE_KEY"]},
                timeout=READY_TIMEOUT,
            )
        except requests.RequestException:
            return _error("Upstream unavailable", 503)
        if not 200 <= upstream.status_code < 300:
            return _error("Upstream unavailable", 503)
        return jsonify({"status": "ready"})

    @app.get("/api/me")
    def current_user() -> tuple[Response, int] | Response:
        authorization = request.headers.get("Authorization", "")
        match = BEARER_PATTERN.fullmatch(authorization)
        if not match or len(match.group(1)) > 8192:
            return _error("Unauthorized", 401)
        if not configured():
            return _error("Service unavailable", 503)

        token = match.group(1)
        try:
            upstream = requests.get(
                f"{app.config['SUPABASE_URL']}/auth/v1/user",
                headers={
                    "apikey": app.config["SUPABASE_PUBLISHABLE_KEY"],
                    "Authorization": f"Bearer {token}",
                },
                timeout=AUTH_TIMEOUT,
            )
        except requests.RequestException:
            return _error("Upstream unavailable", 503)

        # Authentication failures include invalid, expired, and legacy tokens
        # rejected by Supabase. Tokens are never decoded or trusted locally.
        if upstream.status_code in {401, 403}:
            return _error("Unauthorized", 401)
        if not 200 <= upstream.status_code < 300:
            return _error("Upstream unavailable", 503)
        try:
            user = upstream.json()
        except (ValueError, requests.JSONDecodeError):
            return _error("Upstream unavailable", 503)
        if not isinstance(user, dict):
            return _error("Upstream unavailable", 503)
        return jsonify({"user": user})

    return app


app = create_app()


if __name__ == "__main__":
    # Development convenience only. Production uses the documented Gunicorn unit.
    app.run(host="127.0.0.1", port=8000, debug=False)
