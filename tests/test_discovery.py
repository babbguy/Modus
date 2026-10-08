"""
Tests for sdk.modus.discovery — Environment Discovery.
"""
from __future__ import annotations

import os
import platform
import socket
from unittest.mock import patch

import pytest

from modus.discovery import (
    EnvironmentScanner,
    TopologySnapshot,
    _detect_app_id,
    _detect_cloud_provider,
    _detect_deployment,
    _detect_environment,
    _detect_packages,
    _detect_service_dependencies,
    _extract_fastapi_routes,
    _get_process_info,
    _is_ai_route,
    _normalise_env,
    _redact_url,
)


# ── TopologySnapshot ─────────────────────────────────────────────────────────


class TestTopologySnapshot:
    def test_to_dict_returns_dict(self):
        snap = TopologySnapshot(app_id="test-app", app_name="Test App", environment="dev")
        d = snap.to_dict()
        assert isinstance(d, dict)
        assert d["app_id"] == "test-app"
        assert d["runtime"] == "python"

    def test_content_hash_stable(self):
        snap = TopologySnapshot(app_id="a", app_name="A", environment="dev")
        h1 = snap.content_hash()
        h2 = snap.content_hash()
        assert h1 == h2
        assert len(h1) == 16

    def test_content_hash_ignores_volatile_fields(self):
        snap1 = TopologySnapshot(app_id="a", app_name="A", environment="dev", pid=1, scanned_at="t1")
        snap2 = TopologySnapshot(app_id="a", app_name="A", environment="dev", pid=2, scanned_at="t2")
        assert snap1.content_hash() == snap2.content_hash()

    def test_content_hash_differs_for_different_content(self):
        snap1 = TopologySnapshot(app_id="a", app_name="A", environment="dev")
        snap2 = TopologySnapshot(app_id="b", app_name="B", environment="prod")
        assert snap1.content_hash() != snap2.content_hash()


# ── AI route detection ───────────────────────────────────────────────────────


class TestIsAiRoute:
    def test_ai_path_detected(self):
        assert _is_ai_route("/api/v1/chat/completions") is True
        assert _is_ai_route("/api/v1/embeddings") is True

    def test_ai_name_detected(self):
        assert _is_ai_route("/api/v1/something", name="generate_response") is True

    def test_non_ai_route(self):
        assert _is_ai_route("/api/v1/users") is False
        assert _is_ai_route("/health") is False


# ── Environment normalisation ────────────────────────────────────────────────


class TestNormaliseEnv:
    @pytest.mark.parametrize("val,expected", [
        ("production", "production"),
        ("prod", "production"),
        ("live", "production"),
        ("staging", "staging"),
        ("stg", "staging"),
        ("uat", "staging"),
        ("development", "development"),
        ("dev", "development"),
        ("local", "development"),
        ("test", "development"),
        ("ci", "ci"),
        ("build", "ci"),
    ])
    def test_normalise(self, val, expected):
        assert _normalise_env(val) == expected

    def test_django_settings_heuristic(self):
        assert _normalise_env("myapp.settings.production") == "production"
        assert _normalise_env("myapp.settings.staging") == "staging"
        assert _normalise_env("myapp.settings.development") == "development"


# ── Environment detection ────────────────────────────────────────────────────


class TestDetectEnvironment:
    def test_explicit_modus_env(self):
        with patch.dict(os.environ, {"MODUS_ENVIRONMENT": "staging"}, clear=False):
            assert _detect_environment("bare_metal", {}) == "staging"

    def test_standard_env_vars(self):
        with patch.dict(os.environ, {"NODE_ENV": "production", "MODUS_ENVIRONMENT": ""}, clear=False):
            assert _detect_environment("bare_metal", {}) == "production"

    def test_k8s_namespace_prod(self):
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ("MODUS_ENVIRONMENT", "ENVIRONMENT", "ENV", "NODE_ENV",
                                  "FLASK_ENV", "RAILS_ENV", "APP_ENV", "DJANGO_SETTINGS_MODULE",
                                  "MIX_ENV", "RACK_ENV", "ASPNETCORE_ENVIRONMENT",
                                  "SPRING_PROFILES_ACTIVE")}
        with patch.dict(os.environ, env_clean, clear=True):
            result = _detect_environment("kubernetes", {"k8s_namespace": "payments-prod"})
            assert result == "production"

    def test_ci_detection(self):
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ("MODUS_ENVIRONMENT", "ENVIRONMENT", "ENV", "NODE_ENV",
                                  "FLASK_ENV", "RAILS_ENV", "APP_ENV", "DJANGO_SETTINGS_MODULE",
                                  "MIX_ENV", "RACK_ENV", "ASPNETCORE_ENVIRONMENT",
                                  "SPRING_PROFILES_ACTIVE")}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _detect_environment("ci", {}) == "ci"

    def test_bare_metal_default(self):
        env_clean = {k: v for k, v in os.environ.items()
                     if k not in ("MODUS_ENVIRONMENT", "ENVIRONMENT", "ENV", "NODE_ENV",
                                  "FLASK_ENV", "RAILS_ENV", "APP_ENV", "DJANGO_SETTINGS_MODULE",
                                  "MIX_ENV", "RACK_ENV", "ASPNETCORE_ENVIRONMENT",
                                  "SPRING_PROFILES_ACTIVE")}
        with patch.dict(os.environ, env_clean, clear=True):
            assert _detect_environment("bare_metal", {}) == "development"


# ── Cloud provider detection ─────────────────────────────────────────────────


class TestDetectCloudProvider:
    def test_aws_lambda_type(self):
        assert _detect_cloud_provider("aws_lambda", {}) == "aws"

    def test_gcp_cloudrun_type(self):
        assert _detect_cloud_provider("gcp_cloudrun", {}) == "gcp"

    def test_azure_appservice_type(self):
        assert _detect_cloud_provider("azure_appservice", {}) == "azure"

    def test_aws_region_env(self):
        assert _detect_cloud_provider("bare_metal", {"AWS_REGION": "us-east-1"}) == "aws"

    def test_gcp_project_env(self):
        assert _detect_cloud_provider("bare_metal", {"GOOGLE_CLOUD_PROJECT": "my-proj"}) == "gcp"

    def test_azure_env(self):
        assert _detect_cloud_provider("bare_metal", {"WEBSITE_INSTANCE_ID": "abc"}) == "azure"


# ── Deployment detection ─────────────────────────────────────────────────────


class TestDetectDeployment:
    def test_lambda(self):
        with patch.dict(os.environ, {
            "AWS_LAMBDA_FUNCTION_NAME": "my-func",
            "AWS_REGION": "us-east-1",
        }, clear=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "aws_lambda"
            assert cloud == "aws"
            assert meta["aws_function_name"] == "my-func"

    def test_ecs(self):
        with patch.dict(os.environ, {
            "ECS_CONTAINER_METADATA_URI": "http://meta",
            "AWS_LAMBDA_FUNCTION_NAME": "",
        }, clear=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "aws_ecs"
            assert cloud == "aws"

    def test_cloud_run(self):
        with patch.dict(os.environ, {
            "K_SERVICE": "my-svc",
            "AWS_LAMBDA_FUNCTION_NAME": "",
            "ECS_CONTAINER_METADATA_URI": "",
            "ECS_CONTAINER_METADATA_URI_V4": "",
            "KUBERNETES_SERVICE_HOST": "",
        }, clear=False):
            with patch("os.path.exists", return_value=False):
                dtype, cloud, meta = _detect_deployment()
                assert dtype == "gcp_cloudrun"
                assert cloud == "gcp"

    def test_github_actions(self):
        env = {
            "CI": "true",
            "GITHUB_ACTIONS": "true",
            "AWS_LAMBDA_FUNCTION_NAME": "",
            "ECS_CONTAINER_METADATA_URI": "",
            "ECS_CONTAINER_METADATA_URI_V4": "",
            "KUBERNETES_SERVICE_HOST": "",
            "K_SERVICE": "",
            "GAE_APPLICATION": "",
            "WEBSITE_INSTANCE_ID": "",
            "CONTAINER_APP_NAME": "",
            "AZURE_FUNCTIONS_ENVIRONMENT": "",
            "NOMAD_ALLOC_ID": "",
            "NOMAD_JOB_NAME": "",
            "FLY_APP_NAME": "",
            "RAILWAY_PROJECT_ID": "",
            "RENDER_SERVICE_ID": "",
            "DYNO": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("os.path.exists", return_value=False):
                with patch("pathlib.Path.read_text", side_effect=OSError):
                    dtype, cloud, meta = _detect_deployment()
                    assert dtype == "ci"
                    assert meta["ci_system"] == "github_actions"


# ── App identity detection ───────────────────────────────────────────────────


class TestDetectAppId:
    def test_explicit_override(self):
        app_id, app_name = _detect_app_id("My Custom App")
        assert app_id == "my-custom-app"

    def test_env_var(self):
        with patch.dict(os.environ, {"MODUS_APP_ID": "EnvApp_123"}, clear=False):
            app_id, app_name = _detect_app_id()
            assert app_id == "envapp-123"

    def test_normalisation(self):
        app_id, _ = _detect_app_id("My App_Name 123!")
        assert app_id == "my-app-name-123"

    def test_unnamed_fallback(self):
        app_id, _ = _detect_app_id("!!!")
        assert app_id == "unnamed-app"


# ── URL redaction ────────────────────────────────────────────────────────────


class TestRedactUrl:
    def test_standard_url(self):
        assert _redact_url("postgres://user:secret@host/db") == "postgres://user:***@host/db"

    def test_no_username(self):
        assert _redact_url("redis://:secret@host/0") == "redis://:***@host/0"

    def test_no_password(self):
        result = _redact_url("http://host/path")
        assert result == "http://host/path"


# ── Service dependency detection ─────────────────────────────────────────────


class TestDetectServiceDependencies:
    def test_detects_postgres(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://u:p@host/db"}, clear=False):
            deps = _detect_service_dependencies()
            assert "postgres" in deps

    def test_detects_redis(self):
        with patch.dict(os.environ, {"REDIS_URL": "redis://host:6379"}, clear=False):
            deps = _detect_service_dependencies()
            assert "redis" in deps


# ── Package detection ────────────────────────────────────────────────────────


class TestDetectPackages:
    def test_returns_four_dicts(self):
        ai_p, ai_f, web_f, infra = _detect_packages()
        assert isinstance(ai_p, dict)
        assert isinstance(ai_f, dict)
        assert isinstance(web_f, dict)
        assert isinstance(infra, dict)


# ── Process info ─────────────────────────────────────────────────────────────


class TestGetProcessInfo:
    def test_returns_tuple(self):
        workers, memory = _get_process_info()
        assert isinstance(workers, int)
        assert workers >= 1

    def test_web_concurrency(self):
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "4"}, clear=False):
            workers, _ = _get_process_info()
            assert workers == 4


# ── FastAPI route extraction ─────────────────────────────────────────────────


class TestExtractFastapiRoutes:
    def test_extracts_from_mock_app(self):
        class FakeRoute:
            path = "/api/v1/chat"
            methods = {"GET", "POST"}
            name = "chat_endpoint"
            endpoint = None

        class FakeApp:
            routes = [FakeRoute()]

        routes = _extract_fastapi_routes(FakeApp())
        assert len(routes) == 1
        assert routes[0]["path"] == "/api/v1/chat"
        assert routes[0]["likely_ai_endpoint"] is True

    def test_skips_openapi_routes(self):
        class FakeRoute:
            path = "/openapi.json"
            methods = {"GET"}
            name = None
            endpoint = None

        class FakeApp:
            routes = [FakeRoute()]

        routes = _extract_fastapi_routes(FakeApp())
        assert len(routes) == 0


# ── Full scanner ─────────────────────────────────────────────────────────────


class TestEnvironmentScanner:
    def test_scan_returns_topology_snapshot(self):
        scanner = EnvironmentScanner()
        snap = scanner.scan(app_id="test-app", environment="testing")
        assert isinstance(snap, TopologySnapshot)
        assert snap.app_id == "test-app"
        assert snap.runtime == "python"
        assert snap.python_version == platform.python_version()
        assert snap.hostname == socket.gethostname()
        assert snap.scanned_at != ""

    def test_scan_default_app_id(self):
        scanner = EnvironmentScanner()
        snap = scanner.scan()
        assert snap.app_id != ""
        assert snap.app_name != ""
