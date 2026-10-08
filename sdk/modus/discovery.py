"""
Modus — Environment Discovery
======================================
Zero-dependency environment scanner. Discovers everything about the runtime:
  - Python version, platform, hostname
  - Deployment type: Kubernetes, ECS, Lambda, Docker, GCP, Azure, bare metal
  - Cloud provider and region
  - Container metadata
  - Installed AI provider SDKs (anthropic, openai, boto3, cohere, etc.)
  - Installed AI frameworks (langchain, llama-index, etc.)
  - Web framework detection (FastAPI, Flask, Django, etc.)
  - API route extraction from live framework objects
  - Service dependencies from environment variables (DATABASE_URL, REDIS_URL, etc.)
  - Process info (worker count, PID, memory)
  - Git repo name (if available) for auto app-ID detection

This module has ZERO external dependencies — it uses only stdlib.
It is imported at agent startup and runs in < 100ms even on cold Lambda starts.

The scan result is sent to POST /api/v1/topology on the orchestrator, which
stores it and triggers an AI-powered summary via Claude.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class RouteInfo:
    path: str
    methods: list[str]
    name: Optional[str] = None
    likely_ai_endpoint: bool = False
    docstring: Optional[str] = None


@dataclass
class TopologySnapshot:
    """
    Full environment snapshot. Serializable to JSON.
    Sent to the orchestrator on startup and periodically.
    """
    # Identity
    app_id: str
    app_name: str
    environment: str

    # Runtime
    runtime: str = "python"
    python_version: str = ""
    platform_name: str = ""
    hostname: str = ""
    pid: int = 0

    # Deployment
    deployment_type: str = "unknown"
    cloud_provider: str = "unknown"
    detected_environment: str = ""  # auto-detected: production, staging, development, ci
    container_name: str = ""
    k8s_namespace: str = ""
    k8s_deployment: str = ""
    k8s_pod_name: str = ""
    k8s_flavor: str = ""           # eks, gke, aks, openshift, or empty
    aws_region: str = ""
    aws_function_name: str = ""
    ecs_cluster: str = ""
    in_container: bool = False

    # Installed packages
    ai_providers: dict[str, str] = field(default_factory=dict)
    ai_frameworks: dict[str, str] = field(default_factory=dict)
    web_frameworks: dict[str, str] = field(default_factory=dict)
    infrastructure: dict[str, str] = field(default_factory=dict)

    # Framework-specific discovery
    web_framework: str = ""
    api_routes: list[dict] = field(default_factory=list)
    route_count: int = 0

    # Service dependencies (from env vars)
    service_dependencies: dict[str, str] = field(default_factory=dict)

    # Process info
    worker_processes: int = 1
    memory_mb: Optional[float] = None

    # Agent info
    agent_version: str = ""
    sdk_versions: dict[str, str] = field(default_factory=dict)

    # Metadata
    scanned_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def content_hash(self) -> str:
        """
        SHA-256 of the topology content, excluding volatile fields.
        Used to detect meaningful changes without re-running AI summarization.
        """
        stable = {
            k: v for k, v in self.to_dict().items()
            if k not in ("pid", "scanned_at", "agent_version", "k8s_pod_name")
        }
        return hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]


# ── AI endpoint detection ──────────────────────────────────────────────────────

_AI_ROUTE_PATTERNS = re.compile(
    r"(chat|completion|embed|infer|generat|predict|llm|ai|claude|gpt|"
    r"gemini|cohere|model|stream|prompt|rag|agent|synthesiz|transcrib)",
    re.IGNORECASE,
)


def _is_ai_route(path: str, name: Optional[str] = None) -> bool:
    return bool(
        _AI_ROUTE_PATTERNS.search(path)
        or (name and _AI_ROUTE_PATTERNS.search(name))
    )


# ── Package detection ──────────────────────────────────────────────────────────

_AI_PROVIDERS = {
    # Direct provider SDKs — each entry auto-instrumented when installed
    "anthropic", "openai", "boto3", "cohere", "mistralai",
    "google-generativeai", "google-genai", "groq", "together",
    "replicate", "ai21", "aleph-alpha-client", "voyageai",
    "fireworks-ai", "nvidia-ai-endpoints", "perplexity",
    # xAI/Grok uses openai SDK (openai already listed)
    # Azure OpenAI uses openai SDK (openai already listed)
}

_AI_FRAMEWORKS = {
    "langchain", "langchain-core", "langchain-community",
    "llama-index", "llama_index", "llama-index-core",
    "haystack-ai", "semantic-kernel", "autogen", "pyautogen",
    "crewai", "dspy-ai", "dspy", "guidance", "outlines",
    "instructor", "marvin", "mirascope", "aisuite",
    "embedchain", "txtai", "chromadb", "weaviate-client",
    "pinecone-client", "qdrant-client", "milvus", "faiss-cpu", "faiss-gpu",
}

_WEB_FRAMEWORKS = {
    "fastapi", "flask", "django", "starlette", "aiohttp",
    "tornado", "sanic", "litestar", "grpcio", "bottle",
    "falcon", "quart", "blacksheep", "robyn",
}

_INFRASTRUCTURE = {
    "sqlalchemy", "alembic", "psycopg2", "psycopg", "asyncpg",
    "pymongo", "motor", "redis", "aioredis", "elasticsearch",
    "celery", "rq", "dramatiq", "arq", "aiormq", "pika",
    "kafka-python", "confluent-kafka", "nats-py",
    "boto3", "azure-storage-blob", "google-cloud-storage",
    "kubernetes", "docker", "httpx", "aiohttp", "requests",
    "pydantic", "attrs", "marshmallow",
}


def _detect_packages() -> tuple[dict, dict, dict, dict]:
    """
    Scan installed packages using importlib.metadata.
    Returns (ai_providers, ai_frameworks, web_frameworks, infrastructure).
    """
    all_pkgs = {d.name.lower(): d.version for d in importlib.metadata.distributions()}

    ai_providers = {k: v for k, v in all_pkgs.items() if k in _AI_PROVIDERS}
    ai_frameworks = {k: v for k, v in all_pkgs.items() if k in _AI_FRAMEWORKS}
    web_frameworks = {k: v for k, v in all_pkgs.items() if k in _WEB_FRAMEWORKS}
    infra = {k: v for k, v in all_pkgs.items() if k in _INFRASTRUCTURE}

    return ai_providers, ai_frameworks, web_frameworks, infra


# ── Environment auto-detection ────────────────────────────────────────────────

def _detect_environment(deployment_type: str, cloud_meta: dict) -> str:
    """
    Auto-detect environment (production, staging, development, ci, local).

    Priority:
      1. Explicit MODUS_ENVIRONMENT env var (always wins)
      2. Standard environment variables (ENV, ENVIRONMENT, NODE_ENV, etc.)
      3. K8s namespace hints (contains prod, staging, dev, etc.)
      4. CI system detection
      5. Docker Compose (likely local development)
      6. Bare metal with no signals = development
    """
    env = os.environ

    # 1. Explicit Modus env var
    explicit = env.get("MODUS_ENVIRONMENT", "").lower().strip()
    if explicit:
        return _normalise_env(explicit)

    # 2. Standard env vars used across languages/frameworks
    for var in ("ENVIRONMENT", "ENV", "NODE_ENV", "FLASK_ENV", "RAILS_ENV",
                "APP_ENV", "DJANGO_SETTINGS_MODULE", "MIX_ENV", "RACK_ENV",
                "ASPNETCORE_ENVIRONMENT", "SPRING_PROFILES_ACTIVE"):
        val = env.get(var, "").lower().strip()
        if val:
            return _normalise_env(val)

    # 3. K8s namespace hints (e.g., "payments-prod", "staging", "dev-us-east")
    ns = cloud_meta.get("k8s_namespace", "")
    if ns:
        ns_lower = ns.lower()
        if any(p in ns_lower for p in ("prod", "prd", "live")):
            return "production"
        if any(p in ns_lower for p in ("stag", "stg", "preprod", "pre-prod")):
            return "staging"
        if any(p in ns_lower for p in ("dev", "develop", "sandbox", "test")):
            return "development"

    # 4. CI/CD systems
    if deployment_type == "ci":
        return "ci"

    # 5. Docker Compose without explicit env = likely local dev
    if deployment_type == "docker" and env.get("COMPOSE_PROJECT_NAME"):
        return "development"

    # 6. Cloud deployments without explicit env signals = assume production
    if deployment_type in ("aws_lambda", "aws_ecs", "kubernetes",
                           "gcp_cloudrun", "gcp_appengine",
                           "azure_appservice", "azure_containerapp"):
        return "production"

    # 7. Bare metal with no signals = development (developer's machine)
    if deployment_type == "bare_metal":
        return "development"

    return "production"


def _normalise_env(val: str) -> str:
    """Map environment strings to one of: production, staging, development, ci."""
    if val in ("production", "prod", "prd", "live", "release"):
        return "production"
    if val in ("staging", "stg", "stage", "preprod", "pre-prod", "uat", "qa"):
        return "staging"
    if val in ("development", "dev", "develop", "local", "sandbox", "test", "testing"):
        return "development"
    if val in ("ci", "ci/cd", "build", "pipeline"):
        return "ci"
    # Django settings module heuristic
    if "prod" in val:
        return "production"
    if "stag" in val or "stage" in val:
        return "staging"
    if "dev" in val or "local" in val or "test" in val:
        return "development"
    return val


# ── Cloud provider detection (strengthened) ───────────────────────────────────

def _detect_cloud_provider(deployment_type: str, env: dict) -> str:
    """
    Strengthen cloud provider identification using multiple signals.

    Uses (in order):
      1. Deployment-type signals (Lambda=AWS, Cloud Run=GCP, etc.)
      2. Instance metadata file markers
      3. Environment variable patterns
      4. DNS/hostname patterns
    """
    # Already determined by deployment type
    if deployment_type in ("aws_lambda", "aws_ecs"):
        return "aws"
    if deployment_type in ("gcp_cloudrun", "gcp_appengine"):
        return "gcp"
    if deployment_type in ("azure_appservice", "azure_containerapp"):
        return "azure"

    # AWS signals
    aws_signals = [
        env.get("AWS_REGION"),
        env.get("AWS_DEFAULT_REGION"),
        env.get("AWS_EXECUTION_ENV"),
        env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"),
        env.get("EKS_CLUSTER_NAME"),
    ]
    if any(aws_signals):
        return "aws"

    # AWS IMDSv2 marker (EC2, EKS, ECS)
    # The presence of the metadata endpoint config file is a zero-cost check
    if os.path.exists("/etc/amazon"):
        return "aws"

    # GCP signals
    gcp_signals = [
        env.get("GOOGLE_CLOUD_PROJECT"),
        env.get("GCLOUD_PROJECT"),
        env.get("GCP_PROJECT"),
        env.get("CLOUDSDK_CORE_PROJECT"),
        env.get("CLOUDSDK_COMPUTE_REGION"),
        env.get("GKE_NODEPOOL_NAME"),
        env.get("GOOGLE_APPLICATION_CREDENTIALS"),
    ]
    if any(gcp_signals):
        return "gcp"

    # GCP metadata marker
    if os.path.exists("/etc/google_cloud"):
        return "gcp"

    # Azure signals
    azure_signals = [
        env.get("WEBSITE_INSTANCE_ID"),
        env.get("AZURE_FUNCTIONS_ENVIRONMENT"),
        env.get("AzureWebJobsStorage"),
        env.get("AZURE_CLIENT_ID"),
        env.get("IDENTITY_ENDPOINT"),  # Azure managed identity
        env.get("MSI_ENDPOINT"),       # Azure legacy managed identity
    ]
    if any(azure_signals):
        return "azure"

    # Azure IMDS marker
    if os.path.exists("/var/lib/waagent"):
        return "azure"

    # Hostname patterns (last resort — common in cloud VMs)
    hostname = socket.gethostname().lower()
    if hostname.startswith("ip-") and "-" in hostname:
        return "aws"  # AWS EC2 hostname pattern: ip-172-31-x-x
    if "internal.cloudapp.net" in hostname:
        return "azure"

    # Platform-specific services detected via packages
    # (already captured in _detect_packages, but good fallback)
    try:
        pkgs = {d.name.lower() for d in importlib.metadata.distributions()}
        if "boto3" in pkgs and "google-cloud-storage" not in pkgs and "azure-storage-blob" not in pkgs:
            return "aws"
        if "google-cloud-storage" in pkgs and "boto3" not in pkgs:
            return "gcp"
        if "azure-storage-blob" in pkgs and "boto3" not in pkgs:
            return "azure"
    except Exception:
        pass

    return "unknown"


# ── Deployment type detection ──────────────────────────────────────────────────

def _detect_deployment() -> tuple[str, str, dict]:
    """
    Returns (deployment_type, cloud_provider, extra_metadata).
    Detects from environment variables and filesystem signals.
    """
    env = os.environ
    meta: dict[str, str] = {}

    # AWS Lambda
    if env.get("AWS_LAMBDA_FUNCTION_NAME"):
        meta["aws_function_name"] = env["AWS_LAMBDA_FUNCTION_NAME"]
        meta["aws_region"] = env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", ""))
        return "aws_lambda", "aws", meta

    # AWS ECS
    if env.get("ECS_CONTAINER_METADATA_URI") or env.get("ECS_CONTAINER_METADATA_URI_V4"):
        meta["ecs_cluster"] = env.get("ECS_CLUSTER", "")
        meta["ecs_task_family"] = env.get("ECS_TASK_FAMILY", "")
        meta["aws_region"] = env.get("AWS_REGION", env.get("AWS_DEFAULT_REGION", ""))
        return "aws_ecs", "aws", meta

    # Kubernetes — most reliable signal
    if (
        env.get("KUBERNETES_SERVICE_HOST")
        or os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ):
        meta["k8s_namespace"] = env.get("POD_NAMESPACE", env.get("NAMESPACE", ""))
        meta["k8s_pod_name"] = env.get("POD_NAME", env.get("HOSTNAME", ""))
        meta["k8s_deployment"] = env.get("DEPLOYMENT_NAME", env.get("APP_NAME", ""))
        meta["k8s_node"] = env.get("NODE_NAME", "")

        # Try to read namespace from service account
        ns_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        if ns_file.exists() and not meta["k8s_namespace"]:
            try:
                meta["k8s_namespace"] = ns_file.read_text().strip()
            except OSError:
                pass

        # Detect K8s flavor
        if env.get("EKS_CLUSTER_NAME") or os.path.exists("/etc/amazon"):
            meta["k8s_flavor"] = "eks"
        elif env.get("GKE_NODEPOOL_NAME") or env.get("CLOUDSDK_COMPUTE_REGION"):
            meta["k8s_flavor"] = "gke"
        elif env.get("WEBSITE_INSTANCE_ID") or os.path.exists("/var/lib/waagent"):
            meta["k8s_flavor"] = "aks"
        else:
            # Check for OpenShift
            if env.get("OPENSHIFT_BUILD_NAME") or os.path.exists("/run/secrets/kubernetes.io/serviceaccount/..data/namespace"):
                meta["k8s_flavor"] = "openshift"

        # Use strengthened cloud detection
        cloud = _detect_cloud_provider("kubernetes", dict(env))
        return "kubernetes", cloud, meta

    # Google Cloud Run / App Engine
    if env.get("K_SERVICE"):  # Cloud Run
        meta["gcp_service"] = env["K_SERVICE"]
        meta["gcp_revision"] = env.get("K_REVISION", "")
        meta["gcp_region"] = env.get("GOOGLE_CLOUD_REGION", "")
        return "gcp_cloudrun", "gcp", meta

    if env.get("GAE_APPLICATION"):
        meta["gae_service"] = env.get("GAE_SERVICE", "")
        return "gcp_appengine", "gcp", meta

    # Azure App Service / Container Instances
    if env.get("WEBSITE_INSTANCE_ID"):
        meta["azure_app"] = env.get("WEBSITE_SITE_NAME", "")
        return "azure_appservice", "azure", meta

    if env.get("CONTAINER_APP_NAME"):
        meta["azure_container_app"] = env["CONTAINER_APP_NAME"]
        return "azure_containerapp", "azure", meta

    # Azure Functions
    if env.get("AZURE_FUNCTIONS_ENVIRONMENT"):
        meta["azure_function_app"] = env.get("WEBSITE_SITE_NAME", "")
        return "azure_functions", "azure", meta

    # Docker (without K8s)
    if os.path.exists("/.dockerenv"):
        meta["container_name"] = env.get("HOSTNAME", socket.gethostname())
        # Detect compose
        if env.get("COMPOSE_PROJECT_NAME"):
            meta["compose_project"] = env["COMPOSE_PROJECT_NAME"]
            meta["compose_service"] = env.get("COMPOSE_SERVICE", "")
        cloud = _detect_cloud_provider("docker", dict(env))
        return "docker", cloud, meta

    # Check /proc/1/cgroup for container signature without /.dockerenv
    try:
        cgroup = Path("/proc/self/cgroup").read_text()
        if "docker" in cgroup or "containerd" in cgroup:
            cloud = _detect_cloud_provider("docker", dict(env))
            return "docker", cloud, meta
    except OSError:
        pass

    # HashiCorp Nomad
    if env.get("NOMAD_ALLOC_ID") or env.get("NOMAD_JOB_NAME"):
        meta["nomad_job"] = env.get("NOMAD_JOB_NAME", "")
        meta["nomad_alloc_id"] = env.get("NOMAD_ALLOC_ID", "")
        cloud = _detect_cloud_provider("nomad", dict(env))
        return "nomad", cloud, meta

    # Fly.io
    if env.get("FLY_APP_NAME"):
        meta["fly_app"] = env["FLY_APP_NAME"]
        meta["fly_region"] = env.get("FLY_REGION", "")
        return "fly_io", "fly", meta

    # Railway
    if env.get("RAILWAY_PROJECT_ID"):
        meta["railway_project"] = env.get("RAILWAY_PROJECT_NAME", "")
        return "railway", "railway", meta

    # Render
    if env.get("RENDER_SERVICE_ID"):
        meta["render_service"] = env.get("RENDER_SERVICE_NAME", "")
        return "render", "render", meta

    # Heroku
    if env.get("DYNO"):
        meta["heroku_dyno"] = env["DYNO"]
        meta["heroku_app"] = env.get("HEROKU_APP_NAME", "")
        return "heroku", "heroku", meta

    # Bare metal / VM / CI
    if env.get("CI") or env.get("GITHUB_ACTIONS") or env.get("GITLAB_CI") or env.get("CIRCLECI"):
        ci_system = "unknown"
        if env.get("GITHUB_ACTIONS"):
            ci_system = "github_actions"
        elif env.get("GITLAB_CI"):
            ci_system = "gitlab_ci"
        elif env.get("CIRCLECI"):
            ci_system = "circleci"
        elif env.get("JENKINS_URL"):
            ci_system = "jenkins"
        elif env.get("BUILDKITE"):
            ci_system = "buildkite"
        meta["ci_system"] = ci_system
        return "ci", "unknown", meta

    # Bare metal — use strengthened cloud detection in case it's an EC2/GCE/Azure VM
    cloud = _detect_cloud_provider("bare_metal", dict(env))
    return "bare_metal", cloud, meta


# ── App identity auto-detection ────────────────────────────────────────────────

def _detect_app_id(env_override: Optional[str] = None) -> tuple[str, str]:
    """
    Auto-detect app_id and app_name from the environment.
    Returns (app_id, app_name).

    Priority:
        1. Explicit MODUS_APP_ID env var
        2. K8s deployment name
        3. Docker Compose service name
        4. ECS task family
        5. AWS Lambda function name
        6. Git repo name (from .git/config)
        7. Current directory name
        8. "unnamed-app" — never hostname; a random Docker hostname is not useful identity
    """
    if env_override:
        raw = env_override
    elif (app := os.getenv("MODUS_APP_ID")):
        raw = app
    elif (dep := os.getenv("DEPLOYMENT_NAME", os.getenv("APP_NAME"))):
        raw = dep
    elif (svc := os.getenv("COMPOSE_SERVICE", os.getenv("K_SERVICE"))):
        raw = svc
    elif (family := os.getenv("ECS_TASK_FAMILY")):
        raw = family
    elif (fn := os.getenv("AWS_LAMBDA_FUNCTION_NAME")):
        raw = fn
    else:
        # git repo name beats cwd; cwd beats "unnamed-app"; hostname never used
        # because in Docker it's a random hex string (e.g. "a3f2b1c4d5e6") — useless.
        git = _git_repo_name()
        cwd = Path.cwd().name
        raw = git or cwd or "unnamed-app"

    # Normalise: lowercase, replace spaces/underscores with hyphens, strip non-alphanumeric
    app_id = re.sub(r"[^a-z0-9\-]", "", raw.lower().replace("_", "-").replace(" ", "-"))
    app_id = re.sub(r"-+", "-", app_id).strip("-") or "unnamed-app"

    # Human name: title-case the slug
    app_name = os.getenv(
        "MODUS_APP_NAME",
        app_id.replace("-", " ").title(),
    )

    return app_id, app_name


def _git_repo_name() -> Optional[str]:
    """Extract repo name from nearest .git/config or git remote URL."""
    path = Path.cwd()
    for _ in range(5):  # Walk up max 5 levels
        git_config = path / ".git" / "config"
        if git_config.exists():
            try:
                content = git_config.read_text()
                # Extract repo name from remote URL
                match = re.search(r"url\s*=\s*.+[/:]([^/\n]+?)(?:\.git)?\s*$", content, re.M)
                if match:
                    return match.group(1)
                # Fallback: directory name
                return path.name
            except OSError:
                return path.name
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None


# ── Route extraction ───────────────────────────────────────────────────────────

def _extract_fastapi_routes(app_obj) -> list[dict]:
    routes = []
    try:
        for route in getattr(app_obj, "routes", []):
            path = getattr(route, "path", None)
            if not path or path in ("/openapi.json", "/redoc", "/docs"):
                continue
            methods = sorted(getattr(route, "methods", None) or ["GET"])
            name = getattr(route, "name", None)
            endpoint = getattr(route, "endpoint", None)
            doc = getattr(endpoint, "__doc__", None)
            routes.append({
                "path": path,
                "methods": methods,
                "name": name,
                "likely_ai_endpoint": _is_ai_route(path, name),
                "docstring": (doc.strip().split("\n")[0] if doc else None),
            })
    except Exception:
        pass
    return routes


def _extract_flask_routes(app_obj) -> list[dict]:
    routes = []
    try:
        for rule in app_obj.url_map.iter_rules():
            methods = sorted(m for m in rule.methods or [] if m not in ("HEAD", "OPTIONS"))
            if not methods:
                continue
            routes.append({
                "path": rule.rule,
                "methods": methods,
                "name": rule.endpoint,
                "likely_ai_endpoint": _is_ai_route(rule.rule, rule.endpoint),
                "docstring": None,
            })
    except Exception:
        pass
    return routes


def _extract_django_routes() -> list[dict]:
    routes = []
    try:
        from django.urls import get_resolver
        resolver = get_resolver()
        for pattern in resolver.url_patterns:
            path = str(getattr(pattern, "pattern", ""))
            routes.append({
                "path": f"/{path}",
                "methods": ["GET", "POST"],  # Django doesn't expose methods at resolver level
                "name": getattr(pattern, "name", None),
                "likely_ai_endpoint": _is_ai_route(path),
                "docstring": None,
            })
    except Exception:
        pass
    return routes


def _discover_routes(framework: str) -> list[dict]:
    """
    Attempt to extract routes from the detected web framework.
    Searches sys.modules for live app instances.
    """
    routes = []

    # FastAPI / Starlette — look for app objects in sys.modules
    if framework in ("fastapi", "starlette"):
        try:
            import fastapi
            for module in list(sys.modules.values()):
                for attr_name in dir(module):
                    try:
                        obj = getattr(module, attr_name, None)
                        if isinstance(obj, fastapi.FastAPI):
                            routes = _extract_fastapi_routes(obj)
                            if routes:
                                return routes
                    except Exception:
                        continue
        except ImportError:
            pass

    # Flask
    if framework == "flask":
        try:
            import flask
            for module in list(sys.modules.values()):
                for attr_name in dir(module):
                    try:
                        obj = getattr(module, attr_name, None)
                        if isinstance(obj, flask.Flask):
                            routes = _extract_flask_routes(obj)
                            if routes:
                                return routes
                    except Exception:
                        continue
        except ImportError:
            pass

    # Django
    if framework == "django":
        routes = _extract_django_routes()

    return routes


# ── Service dependency detection ───────────────────────────────────────────────

_SERVICE_ENV_PATTERNS = {
    "postgres": re.compile(r"(DATABASE_URL|POSTGRES.*URL|PG.*URL|DB_URL)", re.I),
    # mysql deliberately excludes DATABASE_URL — if DATABASE_URL is set, we classify
    # it as postgres (more common default). Explicit MYSQL_URL takes precedence.
    "mysql": re.compile(r"(MYSQL.*URL|MYSQL.*HOST)", re.I),
    "redis": re.compile(r"(REDIS.*URL|REDIS.*HOST|CACHE.*URL)", re.I),
    "mongodb": re.compile(r"(MONGO.*URL|MONGODB.*URI)", re.I),
    "rabbitmq": re.compile(r"(RABBITMQ.*URL|AMQP.*URL|BROKER.*URL)", re.I),
    "kafka": re.compile(r"(KAFKA.*BROKERS|KAFKA.*BOOTSTRAP)", re.I),
    "elasticsearch": re.compile(r"(ELASTICSEARCH.*URL|ES_URL|ELASTIC_URL)", re.I),
    "s3": re.compile(r"(S3.*BUCKET|AWS_S3|S3_ENDPOINT)", re.I),
    "sqs": re.compile(r"(SQS.*URL|SQS.*QUEUE)", re.I),
    "dynamodb": re.compile(r"(DYNAMODB.*TABLE|DYNAMO.*TABLE)", re.I),
    "gcs": re.compile(r"(GCS.*BUCKET|GOOGLE.*STORAGE)", re.I),
    "azure_blob": re.compile(r"(AZURE.*STORAGE|BLOB.*STORAGE)", re.I),
    "vector_db": re.compile(r"(PINECONE|WEAVIATE|QDRANT|CHROMA|MILVUS|FAISS)", re.I),
}


def _detect_service_dependencies() -> dict[str, str]:
    deps = {}
    env = os.environ
    for service, pattern in _SERVICE_ENV_PATTERNS.items():
        for key, val in env.items():
            if pattern.search(key):
                # Redact credentials from URLs but keep the host
                safe_val = _redact_url(val)
                deps[service] = f"{key}={safe_val}"
                break
    return deps


def _redact_url(url: str) -> str:
    """
    Remove passwords from connection strings for safe display.
    Handles both user:pass@host and :pass@host (no username) patterns.
    """
    # Standard: scheme://user:pass@host  →  scheme://user:***@host
    url = re.sub(r"://([^:@/]+):([^@]+)@", r"://\1:***@", url)
    # No-username: scheme://:pass@host  →  scheme://:***@host
    url = re.sub(r"://:([^@]+)@", r"://:***@", url)
    return url


# ── Process info ───────────────────────────────────────────────────────────────

def _get_process_info() -> tuple[int, Optional[float]]:
    """Returns (worker_count, memory_mb). Best-effort, never raises."""
    workers = 1
    memory_mb = None

    try:
        # Memory from /proc/self/status (Linux)
        status = Path("/proc/self/status").read_text()
        match = re.search(r"VmRSS:\s+(\d+)\s+kB", status)
        if match:
            memory_mb = int(match.group(1)) / 1024
    except OSError:
        pass

    try:
        # Detect gunicorn/uvicorn workers from environment
        if os.getenv("WEB_CONCURRENCY"):
            workers = int(os.getenv("WEB_CONCURRENCY", "1"))
        elif os.getenv("UVICORN_WORKERS"):
            workers = int(os.getenv("UVICORN_WORKERS", "1"))
        elif os.getenv("GUNICORN_WORKERS"):
            workers = int(os.getenv("GUNICORN_WORKERS", "1"))
    except (ValueError, TypeError):
        pass

    return workers, memory_mb


# ── Main scanner ───────────────────────────────────────────────────────────────

class EnvironmentScanner:
    """
    Discovers everything about the running environment.
    Call scan() once at startup. Run time: < 100ms.
    """

    def scan(
        self,
        app_id: Optional[str] = None,
        app_name: Optional[str] = None,
        environment: str = "production",
        agent_version: str = "",
    ) -> TopologySnapshot:
        """
        Run full environment discovery. Returns a TopologySnapshot.
        All detection is best-effort — failures produce empty/default values.
        """
        from datetime import datetime, timezone

        detected_app_id, detected_app_name = _detect_app_id(app_id)
        final_app_id = app_id or detected_app_id
        final_app_name = app_name or detected_app_name

        # Package detection
        ai_providers, ai_frameworks, web_frameworks, infra = _detect_packages()

        # Deployment
        deployment_type, cloud_provider, deploy_meta = _detect_deployment()

        # Environment auto-detection (uses deployment signals + env vars)
        detected_env = _detect_environment(deployment_type, deploy_meta)

        # Primary web framework (first detected)
        web_framework = next(iter(web_frameworks.keys()), "")

        # Route discovery
        api_routes = []
        if web_framework:
            try:
                api_routes = _discover_routes(web_framework)
            except Exception:
                pass

        # Service dependencies
        service_deps = _detect_service_dependencies()

        # Process info
        workers, memory_mb = _get_process_info()

        return TopologySnapshot(
            # Identity
            app_id=final_app_id,
            app_name=final_app_name,
            environment=environment,

            # Runtime
            runtime="python",
            python_version=platform.python_version(),
            platform_name=f"{platform.system()} {platform.machine()}",
            hostname=socket.gethostname(),
            pid=os.getpid(),

            # Deployment
            deployment_type=deployment_type,
            cloud_provider=cloud_provider,
            detected_environment=detected_env,
            container_name=deploy_meta.get("container_name", ""),
            k8s_namespace=deploy_meta.get("k8s_namespace", ""),
            k8s_deployment=deploy_meta.get("k8s_deployment", ""),
            k8s_pod_name=deploy_meta.get("k8s_pod_name", ""),
            k8s_flavor=deploy_meta.get("k8s_flavor", ""),
            aws_region=deploy_meta.get("aws_region", ""),
            aws_function_name=deploy_meta.get("aws_function_name", ""),
            ecs_cluster=deploy_meta.get("ecs_cluster", ""),
            in_container=deployment_type in (
                "docker", "kubernetes", "aws_ecs", "aws_lambda",
                "gcp_cloudrun", "azure_containerapp", "azure_functions",
                "nomad", "fly_io", "railway", "render", "heroku",
            ),

            # Packages
            ai_providers=ai_providers,
            ai_frameworks=ai_frameworks,
            web_frameworks=web_frameworks,
            infrastructure=infra,

            # Framework
            web_framework=web_framework,
            api_routes=api_routes,
            route_count=len(api_routes),

            # Services
            service_dependencies=service_deps,

            # Process
            worker_processes=workers,
            memory_mb=memory_mb,

            # Agent
            agent_version=agent_version,
            sdk_versions={},

            # Metadata
            scanned_at=datetime.now(timezone.utc).isoformat(),
        )
