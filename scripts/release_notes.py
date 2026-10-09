"""Print the CHANGELOG.md section for a release version (the GitHub release notes).

Usage: python scripts/release_notes.py 1.1.0 [--images]

Exits non-zero when the version has no section or the section is empty, so a
release can never go out with missing notes. --images appends the docker pull
commands for the published container images.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
IMAGES = ("modus-orchestrator", "modus-conductor")


def section(text: str, version: str) -> str:
    """Body of the `## [version]` section, without its heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.M)
    match = heading.search(text)
    if not match:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    rest = text[match.end():]
    end = re.search(r"^## \[", rest, re.M)
    body = (rest[: end.start()] if end else rest).strip()
    if not body:
        raise SystemExit(f"CHANGELOG.md section for {version} is empty")
    return body


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        raise SystemExit(__doc__)
    version = args[0].removeprefix("v")
    notes = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if "--images" in argv:
        pulls = "\n".join(f"docker pull ghcr.io/babbguy/{name}:{version}" for name in IMAGES)
        notes += f"\n\n### Container images\n\n```\n{pulls}\n```"
    print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
