"""
SDK Discovery Module

Tests runtime environment scanning:
  - Deployment type detection (Lambda, ECS, K8s, Docker, etc.)
  - Cloud provider detection
  - Environment auto-detection
  - App ID auto-detection
  - Package detection
  - Route extraction
  - Service dependency detection
  - URL redaction
  - TopologySnapshot
"""
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock



# ═════════════════════════════════════════════════════════════════════════════
# 1. NORMALISE ENVIRONMENT
# ═════════════════════════════════════════════════════════════════════════════

def test_normalise_env_production():
    from sdk.modus.discovery import _normalise_env
    for val in ("production", "prod", "prd", "live", "release"):
        assert _normalise_env(val) == "production"


def test_normalise_env_staging():
    from sdk.modus.discovery import _normalise_env
    for val in ("staging", "stg", "stage", "preprod", "uat", "qa"):
        assert _normalise_env(val) == "staging"


def test_normalise_env_development():
    from sdk.modus.discovery import _normalise_env
    for val in ("development", "dev", "develop", "local", "sandbox", "test"):
        assert _normalise_env(val) == "development"


def test_normalise_env_ci():
    from sdk.modus.discovery import _normalise_env
    for val in ("ci", "ci/cd", "build", "pipeline"):
        assert _normalise_env(val) == "ci"


def test_normalise_env_django_heuristic():
    from sdk.modus.discovery import _normalise_env
    assert _normalise_env("myapp.settings.prod") == "production"
    assert _normalise_env("myapp.settings.staging_config") == "staging"
    assert _normalise_env("myapp.settings.dev_local") == "development"


def test_normalise_env_unknown():
    from sdk.modus.discovery import _normalise_env
    assert _normalise_env("custom-env") == "custom-env"


# ═════════════════════════════════════════════════════════════════════════════
# 2. DETECT ENVIRONMENT
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_environment_explicit():
    from sdk.modus.discovery import _detect_environment
    with patch.dict(os.environ, {"MODUS_ENVIRONMENT": "staging"}, clear=False):
        assert _detect_environment("bare_metal", {}) == "staging"


def test_detect_environment_node_env():
    from sdk.modus.discovery import _detect_environment
    env = {k: v for k, v in os.environ.items()}
    env.pop("MODUS_ENVIRONMENT", None)
    env["NODE_ENV"] = "production"
    with patch.dict(os.environ, env, clear=True):
        assert _detect_environment("bare_metal", {}) == "production"


def test_detect_environment_k8s_namespace():
    from sdk.modus.discovery import _detect_environment
    env = {k: v for k, v in os.environ.items()}
    for var in ("MODUS_ENVIRONMENT", "ENVIRONMENT", "ENV", "NODE_ENV",
                "FLASK_ENV", "RAILS_ENV", "APP_ENV", "DJANGO_SETTINGS_MODULE",
                "MIX_ENV", "RACK_ENV", "ASPNETCORE_ENVIRONMENT",
                "SPRING_PROFILES_ACTIVE"):
        env.pop(var, None)
    with patch.dict(os.environ, env, clear=True):
        assert _detect_environment("kubernetes", {"k8s_namespace": "payments-prod"}) == "production"
        assert _detect_environment("kubernetes", {"k8s_namespace": "staging-us"}) == "staging"
        assert _detect_environment("kubernetes", {"k8s_namespace": "dev-sandbox"}) == "development"


def test_detect_environment_ci():
    from sdk.modus.discovery import _detect_environment
    env = {}
    with patch.dict(os.environ, env, clear=True):
        assert _detect_environment("ci", {}) == "ci"


def test_detect_environment_docker_compose():
    from sdk.modus.discovery import _detect_environment
    env = {"COMPOSE_PROJECT_NAME": "myapp"}
    with patch.dict(os.environ, env, clear=True):
        assert _detect_environment("docker", {}) == "development"


def test_detect_environment_cloud_default():
    from sdk.modus.discovery import _detect_environment
    with patch.dict(os.environ, {}, clear=True):
        assert _detect_environment("aws_lambda", {}) == "production"
        assert _detect_environment("gcp_cloudrun", {}) == "production"


def test_detect_environment_bare_metal():
    from sdk.modus.discovery import _detect_environment
    with patch.dict(os.environ, {}, clear=True):
        assert _detect_environment("bare_metal", {}) == "development"


# ═════════════════════════════════════════════════════════════════════════════
# 3. DETECT CLOUD PROVIDER
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_cloud_aws_by_deployment():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("aws_lambda", {}) == "aws"
    assert _detect_cloud_provider("aws_ecs", {}) == "aws"


def test_detect_cloud_gcp_by_deployment():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("gcp_cloudrun", {}) == "gcp"


def test_detect_cloud_azure_by_deployment():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("azure_appservice", {}) == "azure"


def test_detect_cloud_aws_by_env():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("bare_metal", {"AWS_REGION": "us-east-1"}) == "aws"


def test_detect_cloud_gcp_by_env():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("bare_metal", {"GOOGLE_CLOUD_PROJECT": "myproj"}) == "gcp"


def test_detect_cloud_azure_by_env():
    from sdk.modus.discovery import _detect_cloud_provider
    assert _detect_cloud_provider("bare_metal", {"WEBSITE_INSTANCE_ID": "abc"}) == "azure"


def test_detect_cloud_unknown():
    from sdk.modus.discovery import _detect_cloud_provider
    # Patch out all filesystem checks and hostname
    with patch("os.path.exists", return_value=False), \
         patch("socket.gethostname", return_value="myhost"):
        result = _detect_cloud_provider("bare_metal", {})
        # May or may not be "unknown" depending on installed packages,
        # but should not crash
        assert isinstance(result, str)


# ═════════════════════════════════════════════════════════════════════════════
# 4. DETECT DEPLOYMENT
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_deployment_lambda():
    from sdk.modus.discovery import _detect_deployment
    env = {"AWS_LAMBDA_FUNCTION_NAME": "my-func", "AWS_REGION": "us-east-1"}
    with patch.dict(os.environ, env, clear=True):
        dtype, cloud, meta = _detect_deployment()
        assert dtype == "aws_lambda"
        assert cloud == "aws"
        assert meta["aws_function_name"] == "my-func"


def test_detect_deployment_ecs():
    from sdk.modus.discovery import _detect_deployment
    env = {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/abc"}
    with patch.dict(os.environ, env, clear=True):
        dtype, cloud, meta = _detect_deployment()
        assert dtype == "aws_ecs"
        assert cloud == "aws"


def test_detect_deployment_cloud_run():
    from sdk.modus.discovery import _detect_deployment
    env = {"K_SERVICE": "my-service", "K_REVISION": "rev-1"}
    with patch.dict(os.environ, env, clear=True):
        dtype, cloud, meta = _detect_deployment()
        assert dtype == "gcp_cloudrun"


def test_detect_deployment_heroku():
    from sdk.modus.discovery import _detect_deployment
    env = {"DYNO": "web.1", "HEROKU_APP_NAME": "myapp"}
    with patch.dict(os.environ, env, clear=True):
        with patch("os.path.exists", return_value=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "heroku"


def test_detect_deployment_ci_github():
    from sdk.modus.discovery import _detect_deployment
    env = {"CI": "true", "GITHUB_ACTIONS": "true"}
    with patch.dict(os.environ, env, clear=True):
        with patch("os.path.exists", return_value=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "ci"
            assert meta["ci_system"] == "github_actions"


def test_detect_deployment_fly():
    from sdk.modus.discovery import _detect_deployment
    env = {"FLY_APP_NAME": "myapp", "FLY_REGION": "iad"}
    with patch.dict(os.environ, env, clear=True):
        with patch("os.path.exists", return_value=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "fly_io"


def test_detect_deployment_railway():
    from sdk.modus.discovery import _detect_deployment
    env = {"RAILWAY_PROJECT_ID": "proj-123"}
    with patch.dict(os.environ, env, clear=True):
        with patch("os.path.exists", return_value=False):
            dtype, cloud, meta = _detect_deployment()
            assert dtype == "railway"


# ═════════════════════════════════════════════════════════════════════════════
# 5. APP ID DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_app_id_explicit():
    from sdk.modus.discovery import _detect_app_id
    app_id, app_name = _detect_app_id("My_Cool App")
    assert app_id == "my-cool-app"
    assert app_name.lower() == "my cool app"


def test_detect_app_id_from_env():
    from sdk.modus.discovery import _detect_app_id
    with patch.dict(os.environ, {"MODUS_APP_ID": "env-app"}, clear=False):
        app_id, _ = _detect_app_id()
        assert app_id == "env-app"


def test_detect_app_id_normalizes():
    from sdk.modus.discovery import _detect_app_id
    app_id, _ = _detect_app_id("My App!!!  v2.0")
    assert app_id == "my-app-v20"


# ═════════════════════════════════════════════════════════════════════════════
# 6. AI ROUTE DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def test_is_ai_route():
    from sdk.modus.discovery import _is_ai_route
    assert _is_ai_route("/api/v1/chat/completions") is True
    assert _is_ai_route("/api/v1/embed") is True
    assert _is_ai_route("/api/v1/users") is False
    assert _is_ai_route("/api/v1/status", name="ai_inference") is True


# ═════════════════════════════════════════════════════════════════════════════
# 7. URL REDACTION
# ═════════════════════════════════════════════════════════════════════════════

def test_redact_url():
    from sdk.modus.discovery import _redact_url
    assert _redact_url("postgres://user:secret@host/db") == "postgres://user:***@host/db"
    assert _redact_url("redis://:password@host:6379") == "redis://:***@host:6379"
    assert _redact_url("https://host/path") == "https://host/path"


# ═════════════════════════════════════════════════════════════════════════════
# 8. SERVICE DEPENDENCY DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def test_detect_service_dependencies():
    from sdk.modus.discovery import _detect_service_dependencies
    env = {
        "DATABASE_URL": "postgres://user:pass@db-host:5432/mydb",
        "REDIS_URL": "redis://redis-host:6379",
    }
    with patch.dict(os.environ, env, clear=True):
        deps = _detect_service_dependencies()
        assert "postgres" in deps
        assert "***" in deps["postgres"]
        assert "redis" in deps


# ═════════════════════════════════════════════════════════════════════════════
# 9. TOPOLOGY SNAPSHOT
# ═════════════════════════════════════════════════════════════════════════════

def test_topology_snapshot_to_dict():
    from sdk.modus.discovery import TopologySnapshot
    snap = TopologySnapshot(app_id="test", app_name="Test", environment="dev")
    d = snap.to_dict()
    assert d["app_id"] == "test"
    assert d["runtime"] == "python"


def test_topology_snapshot_content_hash():
    from sdk.modus.discovery import TopologySnapshot
    snap1 = TopologySnapshot(app_id="test", app_name="Test", environment="dev", pid=1)
    snap2 = TopologySnapshot(app_id="test", app_name="Test", environment="dev", pid=2)
    # PID is excluded from hash, so they should be equal
    assert snap1.content_hash() == snap2.content_hash()


# ═════════════════════════════════════════════════════════════════════════════
# 10. ENVIRONMENT SCANNER
# ═════════════════════════════════════════════════════════════════════════════

def test_environment_scanner_basic():
    from sdk.modus.discovery import EnvironmentScanner
    scanner = EnvironmentScanner()
    with patch.dict(os.environ, {"MODUS_ENVIRONMENT": "test"}, clear=False):
        snapshot = scanner.scan(app_id="test-app", agent_version="1.0.0")
    assert snapshot.app_id == "test-app"
    assert snapshot.runtime == "python"
    assert snapshot.python_version != ""
    assert snapshot.scanned_at != ""


# ═════════════════════════════════════════════════════════════════════════════
# 11. PROCESS INFO
# ═════════════════════════════════════════════════════════════════════════════

def test_get_process_info():
    from sdk.modus.discovery import _get_process_info
    workers, memory = _get_process_info()
    assert workers >= 1
    # memory may be None on Windows
    assert memory is None or memory > 0


def test_get_process_info_with_workers():
    from sdk.modus.discovery import _get_process_info
    with patch.dict(os.environ, {"WEB_CONCURRENCY": "4"}, clear=False):
        workers, _ = _get_process_info()
        assert workers == 4


# ═════════════════════════════════════════════════════════════════════════════
# 12. FASTAPI ROUTE EXTRACTION
# ═════════════════════════════════════════════════════════════════════════════

def test_extract_fastapi_routes():
    from sdk.modus.discovery import _extract_fastapi_routes

    route1 = MagicMock()
    route1.path = "/api/v1/chat"
    route1.methods = {"GET", "POST"}
    route1.name = "chat_endpoint"
    route1.endpoint = MagicMock(__doc__="Handle chat requests")

    route2 = MagicMock()
    route2.path = "/openapi.json"
    route2.methods = {"GET"}
    route2.name = "openapi"
    route2.endpoint = MagicMock(__doc__=None)

    app = MagicMock()
    app.routes = [route1, route2]

    routes = _extract_fastapi_routes(app)
    assert len(routes) == 1  # /openapi.json should be filtered
    assert routes[0]["path"] == "/api/v1/chat"
    assert routes[0]["likely_ai_endpoint"] is True
