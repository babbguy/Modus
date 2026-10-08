"""Tests -- scripts/seed.py talks to the compose service name and sends the master key."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_seed():
    spec = importlib.util.spec_from_file_location("seed_script", ROOT / "scripts" / "seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_env_agents_points_at_the_compose_service(tmp_path):
    seed = _load_seed()
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "modus" in compose["services"]

    seed.ENV_AGENTS_PATH = tmp_path / ".env.agents"
    seed.REPO_ROOT = tmp_path
    seed.write_env_agents("mds_team_x")
    text = seed.ENV_AGENTS_PATH.read_text(encoding="utf-8")
    assert "MODUS_URL=http://modus:8080" in text
    assert "orchestrator:8080" not in text


def test_every_request_carries_the_master_key_header():
    seed = _load_seed()
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen.append(req.get_header("X-modus-apikey"))
        return Resp(json.dumps([]).encode())

    with patch.object(seed.urllib.request, "urlopen", fake_urlopen):
        seed._req("GET", "/api/v1/teams", token="mds_master_abc")
        seed._req("POST", "/api/v1/policies", {"a": 1}, token="mds_master_abc")
    assert seen == ["mds_master_abc", "mds_master_abc"]


def test_sample_policy_money_is_a_decimal_string():
    src = (ROOT / "scripts" / "seed.py").read_text(encoding="utf-8")
    assert '"cap_usd": "10.00"' in src and '"cap_usd": 10.0' not in src
