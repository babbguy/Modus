"""
Modus Conductor — Build Version
=======================================
The version the Conductor reports (/healthz, /ready, the OpenAPI document).

A container image carries the version it was built as: Dockerfile.conductor
takes a MODUS_BUILD_VERSION build argument (publish.yml passes the release
version), validates it and writes it to conductor/BUILD_VERSION, next to this
package. Without that file (a source checkout, or an image built without the
argument) the version is the package's own, conductor.__version__.

Copyright 2026 babbguy
SPDX-License-Identifier: FSL-1.1-ALv2
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from conductor import __version__ as SOURCE_VERSION

BUILD_VERSION_FILE = Path(__file__).resolve().parent.parent / "BUILD_VERSION"

# Same rule as the Dockerfile's build-argument check: MAJOR.MINOR.PATCH with
# an optional pre-release or build suffix.
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.+-]+)?$")


def read_build_version(path: Path = BUILD_VERSION_FILE) -> str:
    """The version baked into the image at `path`, else the source version."""
    try:
        version = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return SOURCE_VERSION
    if not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError(f"{path} does not hold a valid version: {version!r}")
    return version


@lru_cache(maxsize=1)
def build_version() -> str:
    return read_build_version()
