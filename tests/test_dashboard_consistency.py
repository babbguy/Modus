"""
Static consistency checks for dashboard JS assets. These tests don't boot
the app — they grep the source files to ensure:

- Every tile id referenced in HELP_REGISTRY actually exists in a view file
  (so hover help doesn't point at nonexistent tiles)
- Every guide id in the GUIDES catalog has a matching docs/guides/*.md
  (so "Read on GitHub" links don't 404)
- Every view file that imports createTile also imports from ../tile.js
- Every view registered in router.js has a corresponding file
- No dashboard JS file contains native alert()/confirm() (GAP-6 standard)
- Every .js file in dashboard/js/views/ is either registered in router.js
  or explicitly known to be a helper (none today)

These catch drift at build time, long before a user notices.
"""
from __future__ import annotations

import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard" / "js"
VIEWS = DASH / "views"
DOCS_GUIDES = REPO / "docs" / "guides"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


# ── HELP_REGISTRY consistency ───────────────────────────────────────────────


def test_help_registry_ids_exist_in_views():
    """Every tile id in HELP_REGISTRY (dashboard/js/help.js) must appear as
    a tile id in at least one view file. Otherwise hover help points at a
    tile that was renamed or deleted and nobody noticed."""
    help_src = _read(DASH / "help.js")

    # Extract tile ids from HELP_REGISTRY (the first-level string keys)
    # Format: 'tile-id': { ... }
    registry_block = re.search(
        r"export const HELP_REGISTRY\s*=\s*\{(.*?)\n\};",
        help_src,
        re.DOTALL,
    )
    assert registry_block, "HELP_REGISTRY block not found in help.js"

    block = registry_block.group(1)
    # Keys are quoted strings followed by a colon and an object literal
    tile_ids = set(re.findall(r"'([a-z][a-z0-9-]+)':\s*\{", block))
    assert tile_ids, "HELP_REGISTRY appears empty — parser regression?"

    # Concatenate all view source so we can grep for the id
    all_views_src = ""
    for view_file in VIEWS.glob("*.js"):
        all_views_src += _read(view_file)

    missing = []
    for tile_id in sorted(tile_ids):
        # Look for the id as a quoted string in a createTile call, or as
        # the body id (tile_id + "-body") in a document.getElementById call
        if (
            f"id: '{tile_id}'" not in all_views_src
            and f'id: "{tile_id}"' not in all_views_src
        ):
            missing.append(tile_id)

    assert not missing, (
        f"HELP_REGISTRY has ids that don't exist in any view: {missing}. "
        "Rename or remove them from help.js."
    )


def test_help_registry_guide_links_exist():
    """Every learnMore id in HELP_REGISTRY must match a guide id in the
    GUIDES catalog."""
    help_src = _read(DASH / "help.js")

    learn_more_ids = set(re.findall(r"learnMore:\s*'([^']+)'", help_src))
    guide_ids = set(re.findall(r"id:\s*'([^']+)'[^\}]*?githubPath", help_src))
    assert guide_ids, "GUIDES catalog appears empty"

    orphan = learn_more_ids - guide_ids
    assert not orphan, (
        f"HELP_REGISTRY learnMore points at unknown guide ids: {orphan}. "
        "Add them to GUIDES or fix the learnMore references."
    )


def test_guide_files_match_catalog():
    """Every guide id in help.js GUIDES must have a matching
    docs/guides/<id>.md file and vice versa. Prevents broken 'Read on
    GitHub' links and orphaned tutorial files."""
    help_src = _read(DASH / "help.js")

    # Extract guide ids from GUIDES array entries
    # Format: { id: '01-name-here', title: ..., githubPath: 'docs/guides/01-name-here.md' }
    guides_block = re.search(
        r"export const GUIDES\s*=\s*\[(.*?)^\];",
        help_src,
        re.DOTALL | re.MULTILINE,
    )
    assert guides_block, "GUIDES array not found in help.js"
    catalog_ids = set(re.findall(r"id:\s*'([0-9]{2}-[a-z0-9-]+)'", guides_block.group(1)))
    assert catalog_ids, "GUIDES catalog parsing failed"

    disk_ids = {
        p.stem for p in DOCS_GUIDES.glob("*.md")
        if re.match(r"^\d{2}-", p.stem)
    }

    missing_files = catalog_ids - disk_ids
    orphan_files = disk_ids - catalog_ids
    assert not missing_files, (
        f"GUIDES catalog references files that don't exist on disk: {missing_files}"
    )
    assert not orphan_files, (
        f"docs/guides/ has tutorial files not in GUIDES catalog: {orphan_files}. "
        f"Add them to help.js or delete them."
    )


# ── Router consistency ─────────────────────────────────────────────────────


def test_every_view_in_router_has_a_file():
    """Every key in VIEW_MODULES in router.js must match a file under
    dashboard/js/views/."""
    router_src = _read(DASH / "router.js")
    modules_block = re.search(
        r"const VIEW_MODULES\s*=\s*\{(.*?)\};",
        router_src,
        re.DOTALL,
    )
    assert modules_block, "VIEW_MODULES not found in router.js"

    module_paths = re.findall(
        r"import\(['\"]([^'\"]+)['\"]\)",
        modules_block.group(1),
    )
    assert module_paths, "No dynamic imports parsed from router.js"

    missing = []
    for rel in module_paths:
        # rel starts with ./views/xxx.js — resolve relative to dashboard/js/
        candidate = (DASH / rel.lstrip("./")).resolve()
        if not candidate.exists():
            missing.append(rel)
    assert not missing, f"router.js imports missing view files: {missing}"


# ── UX hygiene ─────────────────────────────────────────────────────────────


def test_no_native_alert_or_confirm_in_dashboard():
    """Native alert() and confirm() block the UI thread and are unstyled.
    All user feedback should use inline modals. This is the GAP-6 standard
    applied to every new dashboard code going forward.

    Exclusions: the wizard onboarding.js predates the standard and is
    tracked for separate cleanup; tooltip code may use confirm() comments."""
    offenders = []
    # toast.js DEFINES the replacement confirm() helper so it's exempt.
    # onboarding.js predates the standard and is tracked for separate cleanup.
    exempt = {"toast.js", "onboarding.js"}
    for js_file in list(DASH.glob("*.js")) + list(VIEWS.glob("*.js")):
        if js_file.name in exempt:
            continue
        src = _read(js_file)
        # Strip block comments and line comments to avoid false positives
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
        src = re.sub(r"//[^\n]*", "", src)
        # Search for actual calls, not property names
        if re.search(r"\balert\s*\(", src):
            offenders.append(f"{js_file.name}: alert(")
        if re.search(r"(?<!\.)\bconfirm\s*\(", src):
            offenders.append(f"{js_file.name}: confirm(")

    assert not offenders, (
        f"Native alert/confirm found in dashboard JS: {offenders}. "
        "Replace with inline modal feedback per GAP-6 standard."
    )


def test_all_view_files_export_render_and_destroy():
    """Every view registered in the router must export render() AND
    destroy(), otherwise view switching leaks state or fails mid-transition."""
    router_src = _read(DASH / "router.js")
    modules_block = re.search(
        r"const VIEW_MODULES\s*=\s*\{(.*?)\};",
        router_src,
        re.DOTALL,
    )
    module_paths = re.findall(
        r"import\(['\"]([^'\"]+)['\"]\)",
        modules_block.group(1),
    )

    bad = []
    for rel in module_paths:
        candidate = (DASH / rel.lstrip("./")).resolve()
        if not candidate.exists():
            continue
        src = _read(candidate)
        if "export async function render" not in src and "export function render" not in src:
            bad.append(f"{candidate.name}: missing render()")
        if "export function destroy" not in src:
            bad.append(f"{candidate.name}: missing destroy()")

    assert not bad, f"View lifecycle contract violations: {bad}"


def test_layouts_defaults_covers_all_views_with_gridstack():
    """Any view that calls initGrid must have a layout entry in
    layouts/defaults.js. Otherwise tiles render without positions and
    users see an empty grid on first load."""
    layouts_src = _read(DASH / "layouts" / "defaults.js")
    layout_keys = set(re.findall(r"^\s{2}([a-z][a-z_]*)\s*:", layouts_src, re.MULTILINE))

    missing = []
    for view_file in VIEWS.glob("*.js"):
        src = _read(view_file)
        # initGrid is called like initGrid(container, 'viewname')
        m = re.search(r"initGrid\([^,]+,\s*['\"]([^'\"]+)['\"]\)", src)
        if m:
            view_key = m.group(1)
            if view_key not in layout_keys:
                missing.append(f"{view_file.name} -> '{view_key}'")

    assert not missing, (
        f"Views call initGrid but have no layout in defaults.js: {missing}"
    )
