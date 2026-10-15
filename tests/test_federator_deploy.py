"""
Federator bare-metal deployment: the systemd units, deploy.sh, and the slim
write handler's refusal to run without a JWT secret.
"""

from __future__ import annotations

import asyncio
import configparser
import re
from pathlib import Path

from federator import write_handler

ROOT = Path(__file__).resolve().parent.parent
FEDERATOR = ROOT / "federator"
DEPLOY_SH = (FEDERATOR / "deploy.sh").read_text(encoding="utf-8")


def _unit(name: str) -> configparser.ConfigParser:
    text = (FEDERATOR / "systemd" / name).read_text(encoding="utf-8")
    text = text.replace("\\\n", " ")  # systemd line continuations
    parser = configparser.ConfigParser(strict=False, interpolation=None, comment_prefixes=("#", ";"))
    parser.optionxform = str  # keys are case-sensitive
    parser.read_string(text)
    return parser


# ── systemd units ────────────────────────────────────────────────────────────


def test_writer_unit_does_not_wait_for_sd_notify():
    service = _unit("federator-writer.service")["Service"]
    # uvicorn never sends READY=1 or watchdog pings
    assert service["Type"] in ("simple", "exec")
    assert "WatchdogSec" not in service
    assert service["Restart"] == "on-failure"


def test_writer_unit_serves_the_activated_socket():
    service = _unit("federator-writer.service")["Service"]
    socket = _unit("federator-writer.socket")["Socket"]
    # systemd passes the first socket as fd 3 (SD_LISTEN_FDS_START); fd 0 is
    # stdin, which is /dev/null unless StandardInput=socket.
    assert re.search(r"--fd 3\b", service["ExecStart"])
    assert "StandardInput" not in service
    assert socket["ListenStream"] == "127.0.0.1:8444"
    assert socket["Accept"] == "false"


def test_writer_unit_requires_its_settings_file():
    service = _unit("federator-writer.service")["Service"]
    # The secret comes from this file; a missing file must fail the unit.
    assert service["EnvironmentFile"] == "/opt/federator/.env"
    assert "FEDERATOR_JWT_SECRET" not in service.get("Environment", "")


def test_units_use_the_paths_deploy_sh_installs():
    assert "APP_DIR=/opt/federator" in DEPLOY_SH
    assert "DATA_DIR=/data" in DEPLOY_SH
    for name in ("federator-writer.service", "federator-aggregate.service"):
        service = _unit(name)["Service"]
        assert service["WorkingDirectory"] == "/opt/federator"
        assert service["ExecStart"].startswith("/opt/federator/venv/bin/")
        assert service["ReadWritePaths"] == "/data"


# ── deploy.sh ────────────────────────────────────────────────────────────────


def test_systemd_install_copies_this_directory_as_the_package():
    # deploy.sh runs from federator/, which is itself the package: there is no
    # federator/ subdirectory to copy.
    assert "cp -r federator/" not in DEPLOY_SH
    assert 'tar -C "$SCRIPT_DIR"' in DEPLOY_SH
    assert '"${APP_DIR}/federator"' in DEPLOY_SH
    # every unit deploy.sh installs exists
    for unit in re.findall(r"federator-[a-z]+\.(?:service|socket|timer)", DEPLOY_SH):
        assert (FEDERATOR / "systemd" / unit).is_file(), unit


def test_systemd_install_keeps_secrets_out_of_the_code_directory():
    # Same exclusions as the image build context
    ignored = {
        line.strip()
        for line in (FEDERATOR / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    excluded = set(re.findall(r"--exclude='([^']+)'", DEPLOY_SH))
    assert {p.rstrip("/") for p in ignored} <= excluded


def test_health_checks_use_the_right_scheme_per_port():
    # 443 is Caddy's TLS port: never plain HTTP
    assert not re.search(r"http://[^\s\"']*:443\b", DEPLOY_SH)
    assert 'https://${domain}/health' in DEPLOY_SH
    assert '--resolve "${domain}:443:127.0.0.1"' in DEPLOY_SH
    # 8443 (full container) and 8444 (systemd writer) are plain HTTP
    assert "http://127.0.0.1:8443/health" in DEPLOY_SH
    assert "http://127.0.0.1:8444/health" in DEPLOY_SH


# ── Slim write handler: no secret, no service ────────────────────────────────


async def _lifespan_startup() -> list[dict]:
    sent: list[dict] = []
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    await write_handler.app({"type": "lifespan"}, receive, send)
    return sent


def test_writer_refuses_to_start_without_a_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(write_handler, "JWT_SECRET", "")
    monkeypatch.setattr(write_handler, "DB_PATH", str(tmp_path / "federator.db"))
    sent = asyncio.run(_lifespan_startup())
    assert sent[0]["type"] == "lifespan.startup.failed"
    assert "FEDERATOR_JWT_SECRET" in sent[0]["message"]
    assert not (tmp_path / "federator.db").exists()


def test_writer_starts_with_a_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(write_handler, "JWT_SECRET", "test-federator-secret-0123456789abcdef")
    monkeypatch.setattr(write_handler, "DB_PATH", str(tmp_path / "federator.db"))
    sent = asyncio.run(_lifespan_startup())
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert (tmp_path / "federator.db").exists()


def test_writer_rejects_every_token_without_a_secret(monkeypatch):
    import time

    import jwt

    token = jwt.encode(
        {"sub": "org", "exp": int(time.time()) + 60, "iss": "modus-orchestrator", "aud": "modus-federator"},
        "some-other-secret-0123456789abcdef", algorithm="HS256",
    )
    monkeypatch.setattr(write_handler, "JWT_SECRET", "")
    assert write_handler._verify_jwt(token) is None
