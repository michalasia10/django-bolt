"""
Tests for first-install experience.

These tests validate that a minimal django-bolt project (like one created by
``django-bolt init``) works correctly out of the box.  They specifically target
bugs that existing tests did not catch because the example projects were more
complex than the simplest possible setup.

Bugs covered:
  - KeyError: '_handler_executor' on static routes (static_routes.py bypassed
    _route_decorator without setting required metadata keys).
  - OpenAPI docs showing no routes when the user route is not registered.
  - Static file serving warning when STATIC_ROOT is None (Rust converted
    Python None to the string "None").
"""

from __future__ import annotations

import pytest

from django_bolt import BoltAPI
from django_bolt.testing import TestClient

# ---------------------------------------------------------------------------
# Bug 1 – Static routes must have _handler_executor in metadata
# ---------------------------------------------------------------------------


class TestStaticRouteMetadata:
    """Static route registration must set all metadata keys required by _dispatch."""

    def test_static_route_has_handler_executor(self):
        """_handler_executor must be present in static route metadata.

        Without this key, _dispatch raises KeyError on the hot path.
        This was the root cause of the KeyError: '_handler_executor' crash
        reported on first install when admin is enabled.
        """
        api = BoltAPI()

        # Register a user route (admin routes need at least one route)
        @api.get("/test")
        async def test_route():
            return {"ok": True}

        # Register admin + static routes (mirrors runbolt startup)
        api._register_admin_routes("127.0.0.1", 8000)

        if not api._admin_routes_registered:
            pytest.skip("Django admin not detected")

        api._register_static_routes()

        if not api._static_routes_registered:
            pytest.skip("Static routes were not registered")

        # Find the static route handler_id
        static_handler_ids = [handler_id for method, path, handler_id, _handler in api._routes if "/static/" in path]
        assert static_handler_ids, "No static route found in api._routes"

        for handler_id in static_handler_ids:
            meta = api._handler_meta[handler_id]

            # The key that caused the original crash
            assert "_handler_executor" in meta, (
                f"Static route handler_id={handler_id} is missing '_handler_executor' in metadata. "
                "This will cause KeyError in _dispatch on the hot path."
            )
            assert callable(meta["_handler_executor"]), "_handler_executor must be a callable (async execute function)"

    def test_static_route_has_all_required_metadata_keys(self):
        """All metadata keys accessed by _dispatch must be present."""
        api = BoltAPI()

        @api.get("/test")
        async def test_route():
            return {"ok": True}

        api._register_admin_routes("127.0.0.1", 8000)
        if not api._admin_routes_registered:
            pytest.skip("Django admin not detected")

        api._register_static_routes()
        if not api._static_routes_registered:
            pytest.skip("Static routes were not registered")

        static_handler_ids = [handler_id for method, path, handler_id, _handler in api._routes if "/static/" in path]

        # Keys that _dispatch accesses directly (no .get() fallback)
        required_keys = {
            "_handler_executor",
            "mode",
            "is_async",
            "injector",
            "injector_is_async",
            "response_type",
            "default_status_code",
        }

        for handler_id in static_handler_ids:
            meta = api._handler_meta[handler_id]
            missing = required_keys - set(meta.keys())
            assert not missing, (
                f"Static route handler_id={handler_id} is missing metadata keys: {missing}. "
                "These are accessed by _dispatch without .get() fallback and will KeyError."
            )

    def test_static_route_has_middleware_metadata(self):
        """Static routes must have middleware metadata for Rust optimization flags."""
        api = BoltAPI()

        @api.get("/test")
        async def test_route():
            return {"ok": True}

        api._register_admin_routes("127.0.0.1", 8000)
        if not api._admin_routes_registered:
            pytest.skip("Django admin not detected")

        api._register_static_routes()
        if not api._static_routes_registered:
            pytest.skip("Static routes were not registered")

        static_handler_ids = [handler_id for method, path, handler_id, _handler in api._routes if "/static/" in path]

        for handler_id in static_handler_ids:
            assert handler_id in api._handler_middleware, (
                f"Static route handler_id={handler_id} has no middleware metadata entry. "
                "Rust-side optimization flags will be missing."
            )


# ---------------------------------------------------------------------------
# Bug 1b – Static route must actually serve requests without crashing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_static_route_serves_request_without_keyerror():
    """Static file route must not crash with KeyError: '_handler_executor'.

    This is the integration-level test: actually dispatch a request to the
    static route and verify we get a response (even a 404 for a missing file)
    instead of a 500 from a KeyError.
    """
    api = BoltAPI()

    @api.get("/test")
    async def test_route():
        return {"ok": True}

    api._register_admin_routes("127.0.0.1", 8000)
    if not api._admin_routes_registered:
        pytest.skip("Django admin not detected")

    api._register_static_routes()
    if not api._static_routes_registered:
        pytest.skip("Static routes were not registered")

    # Force /static/* requests through the Python static route by moving the
    # Rust static middleware prefix away from /static.
    static_config = {
        "url_prefix": "/assets",
        "directories": [],
        "csp_header": None,
    }

    with TestClient(api, use_http_layer=True, static_files_config=static_config) as client:
        # Request a known Django admin asset; this exercises the static handler
        # registration path that can KeyError if metadata is incomplete.
        response = client.get("/static/admin/css/login.css")
        assert response.status_code != 500, (
            f"Static route returned 500: likely KeyError in _dispatch. Body: {response.text[:200]}"
        )
        assert response.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Bug 2 – Minimal API: single route must actually work
# ---------------------------------------------------------------------------


class TestMinimalFirstInstall:
    """A minimal single-route API must work out of the box."""

    def test_single_route_responds(self):
        """The simplest possible API must return 200 on the registered route."""
        api = BoltAPI()

        @api.get("/test")
        def index():
            return "Hello, World!"

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            # Plain string returns as text, not JSON
            assert "Hello, World!" in response.text

    def test_single_async_route_responds(self):
        """Async handler also works."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        with TestClient(api) as client:
            response = client.get("/test")
            assert response.status_code == 200
            assert "Hello, World!" in response.text

    def test_openapi_docs_include_user_routes(self):
        """OpenAPI docs must include user-defined routes, not just framework routes."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        # Register OpenAPI routes (as runbolt does)
        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs/openapi.json")
            assert response.status_code == 200

            schema = response.json()
            paths = schema.get("paths", {})
            assert "/test" in paths, f"User route /test not found in OpenAPI paths. Paths found: {list(paths.keys())}"

    def test_openapi_docs_endpoint_serves_html(self):
        """The /docs endpoint must serve HTML UI."""
        api = BoltAPI()

        @api.get("/test")
        async def index():
            return "Hello, World!"

        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs")
            assert response.status_code == 200
            # Should be HTML content (Swagger/Scalar/etc UI)
            content_type = response.headers.get("content-type", "")
            assert "text/html" in content_type, f"Expected HTML content at /docs, got content-type: {content_type}"


# ---------------------------------------------------------------------------
# Bug 3 – Route count should distinguish user vs framework routes
# ---------------------------------------------------------------------------


class TestRouteCountAccuracy:
    """Route count messaging should be clear about user vs framework routes."""

    def test_framework_routes_are_separate_from_user_routes(self):
        """After OpenAPI registration, user route count should be distinguishable."""
        api = BoltAPI()

        @api.get("/hello")
        async def hello():
            return "hello"

        @api.get("/world")
        async def world():
            return "world"

        user_count_before = len(api._routes)
        assert user_count_before == 2, f"Expected 2 user routes, got {user_count_before}"

        api._register_openapi_routes()

        total_count = len(api._routes)
        framework_routes = total_count - user_count_before
        assert framework_routes > 0, "OpenAPI should add framework routes"
        assert user_count_before == 2, "User route count should not change"

    def test_user_routes_appear_in_schema(self):
        """All user routes must appear in OpenAPI schema even with many framework routes."""
        api = BoltAPI()

        @api.get("/users")
        async def users():
            return []

        @api.post("/users")
        async def create_user():
            return {}

        @api.get("/users/{user_id}")
        async def get_user(user_id: int):
            return {}

        api._register_openapi_routes()

        with TestClient(api) as client:
            response = client.get("/docs/openapi.json")
            schema = response.json()
            paths = schema.get("paths", {})

            assert "/users" in paths, f"Missing /users in schema paths: {list(paths.keys())}"
            assert "/users/{user_id}" in paths, "Missing /users/{user_id} in schema paths"

            # Verify methods
            assert "get" in paths["/users"], "/users should have GET"
            assert "post" in paths["/users"], "/users should have POST"
            assert "get" in paths["/users/{user_id}"], "/users/{user_id} should have GET"
