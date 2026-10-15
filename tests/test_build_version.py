"""
The version the Orchestrator and Conductor report comes from the build, not
from MODUS_VERSION (the image tag in deploy/.env) or any other setting.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from conductor.core import build_info as conductor_build
from conductor.core.config import Settings as ConductorSettings
from orchestrator.core import build_info as orchestrator_build
from orchestrator.core.config import Settings as OrchestratorSettings

ROOT = Path(__file__).resolve().parent.parent

BUILDS = [
    pytest.param(orchestrator_build, "orchestrator", ["Dockerfile", "Dockerfile.orchestrator"], id="orchestrator"),
    pytest.param(conductor_build, "conductor", ["Dockerfile.conductor"], id="conductor"),
]


@pytest.mark.parametrize(("build", "package", "dockerfiles"), BUILDS)
def test_without_a_build_file_the_source_version_is_reported(build, package, dockerfiles, tmp_path):
    assert build.read_build_version(tmp_path / "BUILD_VERSION") == build.SOURCE_VERSION
    # A checkout has no build file (it is written only inside images)
    assert not (ROOT / package / "BUILD_VERSION").exists()
    assert build.BUILD_VERSION_FILE == ROOT / package / "BUILD_VERSION"


@pytest.mark.parametrize(("build", "package", "dockerfiles"), BUILDS)
@pytest.mark.parametrize("version", ["1.2.0", "1.2.0-rc.1", "2.0.0+build.7"])
def test_the_baked_version_is_reported(build, package, dockerfiles, tmp_path, version):
    path = tmp_path / "BUILD_VERSION"
    path.write_text(version + "\n", encoding="utf-8")
    assert build.read_build_version(path) == version


@pytest.mark.parametrize(("build", "package", "dockerfiles"), BUILDS)
@pytest.mark.parametrize("content", ["latest", "", "v1.2.0", "1.2", "1.2.0 extra"])
def test_an_invalid_build_file_fails_loudly(build, package, dockerfiles, tmp_path, content):
    path = tmp_path / "BUILD_VERSION"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(RuntimeError, match="valid version"):
        build.read_build_version(path)


def test_image_tag_does_not_set_the_orchestrator_version(monkeypatch):
    # deploy/.env holds MODUS_VERSION=latest and the Compose file passes .env
    # to the container: it must not become the reported version.
    monkeypatch.setenv("MODUS_VERSION", "latest")
    settings = OrchestratorSettings(_env_file=None)
    assert settings.version == orchestrator_build.build_version()
    assert settings.version != "latest"
    assert "version" not in OrchestratorSettings.model_fields


def test_environment_does_not_set_the_conductor_version(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_VERSION", "latest")
    monkeypatch.setenv("MODUS_VERSION", "latest")
    settings = ConductorSettings(_env_file=None)
    assert settings.version == conductor_build.build_version()
    assert "version" not in ConductorSettings.model_fields


def test_conductor_source_version_is_the_package_version():
    import conductor

    assert conductor_build.SOURCE_VERSION == conductor.__version__


@pytest.mark.parametrize(("build", "package", "dockerfiles"), BUILDS)
def test_dockerfiles_bake_the_build_argument(build, package, dockerfiles):
    for name in dockerfiles:
        text = (ROOT / name).read_text(encoding="utf-8")
        assert 'ARG MODUS_BUILD_VERSION=""' in text, name
        assert 'LABEL org.opencontainers.image.version="${MODUS_BUILD_VERSION}"' in text, name
        assert f'> {package}/BUILD_VERSION' in text, name
        # The build rejects what the service would reject
        pattern = re.search(r"grep -Eqx '([^']+)'", text).group(1)
        assert pattern == build.VERSION_PATTERN.pattern.strip("^$"), name
        # Written before the code is chowned and the user switched
        assert text.index("> " + package + "/BUILD_VERSION") < text.index("USER modus"), name


def test_build_context_excludes_local_build_files():
    assert "**/BUILD_VERSION" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_publish_passes_the_release_version():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "MODUS_BUILD_VERSION=${{ steps.version.outputs.is_release == 'true' && steps.version.outputs.version || '' }}" in text
    assert "scripts/check_versions.py" in text


def test_source_versions_agree():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+, as in CI

    with open(ROOT / "sdk" / "pyproject.toml", "rb") as f:
        version = tomllib.load(f)["project"]["version"]
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_versions.py"), version],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_versions.py"), "99.0.0"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "expected 99.0.0" in result.stderr
