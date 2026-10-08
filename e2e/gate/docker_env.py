# Copyright 2026 babbguy
# SPDX-License-Identifier: Apache-2.0
"""Bring-up and teardown of the production image under the gate.

Everything runs through the docker CLI so it behaves the same on Linux CI
runners and on Windows with Docker Desktop.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .common import GateError, log

POSTGRES_IMAGE = "postgres:16.12-alpine"   # matches the PostgreSQL 16 the Compose files use
APP_PORT = 8080
HOOK_PORT = 9000
CPU_LIMIT = "1"
MEMORY_LIMIT = "1g"


def docker(*args: str, check: bool = True, timeout: float = 600, capture: bool = True) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=capture, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise GateError(f"docker {' '.join(args[:3])} failed ({proc.returncode}): {proc.stderr.strip()[:2000]}")
    return (proc.stdout or "").strip()


def build_image(context: Path, tag: str) -> None:
    log(f"building {tag} from {context}")
    proc = subprocess.run(["docker", "build", "-t", tag, str(context)], text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise GateError(f"docker build failed for {context}")


def _http_status(url: str, timeout: float = 3.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


@dataclass
class Stack:
    """The containers of one gate run."""
    image: str
    db: str                      # "sqlite" | "postgres"
    out_dir: Path
    run_id: str = field(default_factory=lambda: secrets.token_hex(4))
    master_key: str = field(default_factory=lambda: "mds_master_" + secrets.token_urlsafe(32))
    jwt_secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    pg_password: str = field(default_factory=lambda: secrets.token_urlsafe(18))
    base_url: str = ""
    hook_url_host: str = ""      # receiver as seen from the gate (host)
    hook_url_app: str = ""       # receiver as seen from the Modus container
    started: list[str] = field(default_factory=list)
    network: str = ""

    @property
    def prefix(self) -> str:
        return f"modus-gate-{self.run_id}"

    @property
    def app(self) -> str:
        return f"{self.prefix}-app"

    @property
    def pg(self) -> str:
        return f"{self.prefix}-pg"

    @property
    def hook(self) -> str:
        return f"{self.prefix}-hook"

    # ── bring-up ────────────────────────────────────────────────────────────
    def up(self, hook_script: Path) -> None:
        self.network = self.prefix
        docker("network", "create", self.network)
        self.started.append("network:" + self.network)
        self._start_hook(hook_script)
        if self.db == "postgres":
            self._start_postgres()
        self._start_app()

    def _start_hook(self, script: Path) -> None:
        # The webhook receiver runs on the gate's Docker network (reachable from
        # Modus by name on every platform) using the Modus image's own Python.
        docker("create", "--name", self.hook, "--network", self.network,
               "-p", f"127.0.0.1::{HOOK_PORT}", "--entrypoint", "python",
               self.image, "-u", "/tmp/gate_hook.py", str(HOOK_PORT))
        self.started.append(self.hook)
        docker("cp", str(script), f"{self.hook}:/tmp/gate_hook.py")
        docker("start", self.hook)
        port = self._host_port(self.hook, HOOK_PORT)
        self.hook_url_host = f"http://127.0.0.1:{port}"
        self.hook_url_app = f"http://{self.hook}:{HOOK_PORT}"
        if not _wait_http(self.hook_url_host + "/_gate/health", 60):
            raise GateError("webhook receiver did not start: " + docker("logs", self.hook, check=False))

    def _start_postgres(self) -> None:
        log(f"starting {POSTGRES_IMAGE}")
        docker("run", "-d", "--name", self.pg, "--network", self.network,
               "-e", "POSTGRES_DB=modus", "-e", "POSTGRES_USER=modus",
               "-e", f"POSTGRES_PASSWORD={self.pg_password}", POSTGRES_IMAGE)
        self.started.append(self.pg)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            # pg_isready on the TCP socket: the init script's temporary server
            # only listens on the unix socket, so this waits for the real one.
            r = subprocess.run(["docker", "exec", self.pg, "pg_isready", "-h", "127.0.0.1", "-U", "modus", "-d", "modus"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                return
            time.sleep(1)
        raise GateError("PostgreSQL did not become ready")

    def app_env(self) -> dict[str, str]:
        if self.db == "postgres":
            db_url = f"postgresql+asyncpg://modus:{self.pg_password}@{self.pg}:5432/modus"
        else:
            db_url = "sqlite+aiosqlite:////app/data/modus.db"
        return {
            # Production posture: JWT auth, generated master key and secrets.
            "MODUS_ENVIRONMENT": "production",
            "MODUS_AUTH_MODE": "jwt",
            "MODUS_JWT_SECRET": self.jwt_secret,
            "MODUS_MASTER_API_KEY": self.master_key,
            "MODUS_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
            "MODUS_DATABASE_URL": db_url,
            # Shortest documented reconcile interval, so the gate sees many
            # aggregator cycles within a few minutes.
            "MODUS_AGGREGATE_INTERVAL_SECONDS": "10",
        }

    def _start_app(self) -> None:
        log(f"starting {self.image} ({self.db}) capped at --cpus={CPU_LIMIT} --memory={MEMORY_LIMIT}")
        args = ["run", "-d", "--name", self.app, "--network", self.network,
                f"--cpus={CPU_LIMIT}", f"--memory={MEMORY_LIMIT}", f"--memory-swap={MEMORY_LIMIT}",
                "-p", f"127.0.0.1::{APP_PORT}"]
        for k, v in self.app_env().items():
            args += ["-e", f"{k}={v}"]
        args.append(self.image)
        docker(*args)
        self.started.append(self.app)
        port = self._host_port(self.app, APP_PORT)
        self.base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if _http_status(self.base_url + "/health") == 200:
                log(f"Modus is up at {self.base_url}")
                return
            state = docker("inspect", "-f", "{{.State.Status}}", self.app, check=False)
            if state in ("exited", "dead"):
                break
            time.sleep(1)
        raise GateError("Modus did not become healthy:\n" + self.logs()[-4000:])

    def _host_port(self, container: str, port: int) -> int:
        out = docker("port", container, f"{port}/tcp")
        m = re.search(r":(\d+)\s*$", out.splitlines()[0])
        if not m:
            raise GateError(f"no published port for {container}: {out}")
        return int(m.group(1))

    # ── helpers ─────────────────────────────────────────────────────────────
    def logs(self, container: Optional[str] = None) -> str:
        proc = subprocess.run(["docker", "logs", container or self.app], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        return (proc.stdout or "") + (proc.stderr or "")

    def exec(self, container: str, *cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", container, *cmd], capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")

    def cpu_usage_seconds(self) -> Optional[float]:
        """Total CPU time the Modus container has used, from its cgroup.

        Exact, unlike `docker stats`, whose percentages are computed from
        sample deltas and swing wildly when the Docker host is busy.
        """
        r = self.exec(self.app, "sh", "-c",
                      "cat /sys/fs/cgroup/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpuacct/cpuacct.usage 2>/dev/null "
                      "|| cat /sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")
        out = (r.stdout or "").strip()
        m = re.search(r"^usage_usec\s+(\d+)", out, re.M)
        if m:
            return int(m.group(1)) / 1e6
        if out.isdigit():
            return int(out) / 1e9
        return None

    def popen_exec(self, container: str, *cmd: str) -> subprocess.Popen:
        return subprocess.Popen(["docker", "exec", container, *cmd], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")

    def down(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for c in (self.app, self.pg, self.hook):
            if c in self.started:
                try:
                    (self.out_dir / f"{c.replace(self.prefix + '-', '')}.log").write_text(
                        self.logs(c), encoding="utf-8")
                except Exception as exc:  # keep tearing down
                    log(f"could not save logs of {c}: {exc}")
        for c in reversed(self.started):
            if c.startswith("network:"):
                docker("network", "rm", c.split(":", 1)[1], check=False)
            else:
                docker("rm", "-f", "-v", c, check=False)
        self.started.clear()


def _wait_http(url: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _http_status(url) == 200:
            return True
        time.sleep(0.5)
    return False


# ── docker stats sampling ─────────────────────────────────────────────────────

_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2,
          "gb": 1000**3, "gib": 1024**3}


def parse_bytes(text: str) -> float:
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", text)
    if not m:
        return 0.0
    return float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1)


class StatsSampler:
    """Streams `docker stats` for the Modus container in the background.

    CPU % is relative to one core (100 % = one full CPU), and the container is
    capped at one CPU.
    """

    def __init__(self, container: str):
        self.container = container
        self.samples: list[tuple[float, float, float, str]] = []   # (t, cpu%, mem bytes, phase)
        self.phase = "startup"
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["docker", "stats", "--format", "{{json .}}", self.container],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            encoding="utf-8", errors="replace")
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
                cpu = float(d.get("CPUPerc", "0%").rstrip("%") or 0)
                mem = parse_bytes(d.get("MemUsage", "0B").split("/")[0])
            except (ValueError, KeyError):
                continue
            if mem <= 0:
                continue   # container not running yet / already gone
            self.samples.append((time.monotonic(), cpu, mem, self.phase))

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def peak_mem(self) -> float:
        return max((s[2] for s in self.samples), default=0.0)

    def cpu_in(self, phase: str) -> list[float]:
        return [s[1] for s in self.samples if s[3] == phase]
