"""Check that the Orchestrator, the Conductor and the SDK carry a release version.

Usage: python scripts/check_versions.py 1.2.0

The images report the version they are built as (the MODUS_BUILD_VERSION build
argument), and a source checkout reports the version in its source. Both must
be the version of the code, so a release fails here when any of these differ:

    orchestrator/core/build_info.py   SOURCE_VERSION
    conductor/__init__.py             __version__
    sdk/pyproject.toml                [project] version

Standard library only, and nothing is imported from the packages.
"""
from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _assigned(path: Path, name: str) -> str:
    """The string literal assigned to a module-level `name` in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise SystemExit(f"{path.relative_to(ROOT)}: no string assignment to {name}")


def source_versions() -> dict[str, str]:
    with open(ROOT / "sdk" / "pyproject.toml", "rb") as f:
        sdk = tomllib.load(f)["project"]["version"]
    return {
        "orchestrator/core/build_info.py SOURCE_VERSION": _assigned(
            ROOT / "orchestrator" / "core" / "build_info.py", "SOURCE_VERSION"
        ),
        "conductor/__init__.py __version__": _assigned(ROOT / "conductor" / "__init__.py", "__version__"),
        "sdk/pyproject.toml version": sdk,
    }


def main(args: list[str]) -> int:
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    expected = args[0].removeprefix("v")
    wrong = {where: found for where, found in source_versions().items() if found != expected}
    for where, found in wrong.items():
        print(f"{where} is {found}, expected {expected}", file=sys.stderr)
    if not wrong:
        print(f"All versions are {expected}")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
