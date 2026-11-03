"""Ratchet: a flat-only lister of an output-tree (`docs/plans/*.md`-shaped literal) may not grow.

The four sharded record trees (`output_tree_layout.trees()`) hold `<tree>/<YYYY-MM>/<file>` entries
beside flat legacy ones. A string literal that ends `<tree>/*.<ext>` lists only the flat level, so a
sharded entry is invisible to it. `_FLAT_ONLY_SITES` pins each production file that still carries
such a literal, with its exact count. It only shrinks: a new literal, or a count above the pin, fails;
a count below the pin also fails, so the pin is lowered when a site is widened.

Not counted: test scaffolding (`assemble._is_test_scaffolding`), docstrings, a column-0 op
declaration (`MUTATES = ["docs/plans/*.md"]` is a declaration, not a reader), and a file that imports
`output_tree_layout` (it resolves shards itself).

Negative-spec: static scan only (stdlib `ast`, no import of scanned modules, no git spawn); fast tier.
"""

from __future__ import annotations

import ast
import os
import re
from collections import Counter
from functools import lru_cache

from coordinator_core.ops.generator_census.assemble import _is_test_scaffolding
from coordinator_core.ops.generator_census.declarations import DECLARATION_NAMES
from coordinator_core.session import output_tree_layout

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_SCAN_TOPS = ("coordinator_core", "coordinator/bin")

_FLAT_GLOB = re.compile(
    r"(?:^|/)(?:" + "|".join(re.escape(t) for t in output_tree_layout.trees()) + r")/\*\.\w+$"
)

_FLAT_ONLY_SITES = {
    "coordinator_core/frontmatter/schema_validate.py": 1,
    "coordinator_core/ops/changelog_ops.py": 2,
    "coordinator_core/ops/records_query.py": 4,
    "coordinator_core/workstream_complete/directives_memo_lifecycle.py": 1,
    "coordinator_core/write_guards/nudge_baton_body_bar.py": 1,
}


def _imports_output_tree_layout(tree: ast.AST) -> bool:
    """True when the module imports `output_tree_layout`; a comment or string naming it does not count."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (
            (node.module or "").endswith("output_tree_layout")
            or any(a.name == "output_tree_layout" for a in node.names)
        ):
            return True
        if isinstance(node, ast.Import) and any(a.name.endswith("output_tree_layout") for a in node.names):
            return True
    return False


def _flat_literal_count(text: str) -> int:
    """Count flat tree-glob string literals outside docstrings and column-0 op declarations."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return 0
    if _imports_output_tree_layout(tree):
        return 0
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id in DECLARATION_NAMES for t in targets):
            skip.update(id(n) for n in ast.walk(node))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in skip
        and _FLAT_GLOB.search(node.value)
    )


@lru_cache(maxsize=1)
def _sites() -> dict[str, int]:
    found: Counter[str] = Counter()
    for top in _SCAN_TOPS:
        for dirpath, _dirs, files in os.walk(os.path.join(_REPO_ROOT, top)):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                rel = os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")
                if _is_test_scaffolding(rel):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    continue
                n = _flat_literal_count(text)
                if n:
                    found[rel] = n
    return dict(found)


def test_no_new_flat_only_output_tree_listers():
    grown = {p: n for p, n in _sites().items() if n > _FLAT_ONLY_SITES.get(p, 0)}
    assert not grown, (
        "flat-only output-tree glob literal(s) above the pinned count (list via "
        f"output_tree_layout.iter_entries instead): {grown}"
    )


def test_pinned_sites_still_carry_their_literals():
    found = _sites()
    stale = {p: n for p, n in _FLAT_ONLY_SITES.items() if found.get(p, 0) < n}
    assert not stale, f"pinned site(s) shrank or vanished -- lower or remove the pin: {stale}"


def test_detector_counts_listers_and_exempts_declarations_docstrings_and_shard_aware_files():
    assert _flat_literal_count('x = root.glob("docs/plans/*.md")\n') == 1
    assert _flat_literal_count('y = f"{r}/state/sizings/*.yaml"\n') == 1
    assert _flat_literal_count('z = ("state/handoffs/*.md", "docs/delivery/*.md")\n') == 2
    assert _flat_literal_count('MUTATES = ["docs/plans/*.md"]\n') == 0
    assert _flat_literal_count('"""docs/plans/*.md"""\nx = 1\n') == 0
    assert _flat_literal_count('def f():\n    """state/handoffs/*.md"""\n') == 0
    assert _flat_literal_count('x = "docs/plans/*/*.md"\n') == 0
    assert _flat_literal_count('x = "docs/decisions/*.md"\n') == 0
    shard_aware = "from coordinator_core.session import output_tree_layout\nx = 'docs/plans/*.md'\n"
    assert _flat_literal_count(shard_aware) == 0
    assert _flat_literal_count('def f():\n    MUTATES = ["docs/plans/*.md"]\n') == 1


def _write(root, rel, text="---\ntitle: t\n---\nbody\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_backfill_enumerate_corpus_lists_flat_and_sharded_entries(tmp_path):
    from coordinator_core.ops import backfill_deliverable_spine as mod

    _write(tmp_path, "docs/plans/2026-01-01-flat.md")
    _write(tmp_path, "docs/plans/2026-11/2026-11-03-sharded.md")
    _write(tmp_path, "state/handoffs/2026-01-01-flat.md")
    _write(tmp_path, "state/handoffs/2026-11/2026-11-03-sharded.md")
    _write(tmp_path, "state/sizings/2026-01-01-flat.yaml")
    _write(tmp_path, "state/sizings/2026-11/2026-11-03-sharded.yaml")
    names = {
        os.path.relpath(p, tmp_path).replace(os.sep, "/") for p in mod.enumerate_corpus(str(tmp_path))
    }
    for rel in (
        "docs/plans/2026-01-01-flat.md",
        "docs/plans/2026-11/2026-11-03-sharded.md",
        "state/handoffs/2026-01-01-flat.md",
        "state/handoffs/2026-11/2026-11-03-sharded.md",
        "state/sizings/2026-01-01-flat.yaml",
        "state/sizings/2026-11/2026-11-03-sharded.yaml",
    ):
        assert rel in names, (rel, sorted(names))


def test_backfill_non_month_subdir_is_not_descended(tmp_path):
    from coordinator_core.ops import backfill_deliverable_spine as mod

    _write(tmp_path, "docs/plans/notes/2026-11-03-x.md")
    assert mod._list_md_files(str(tmp_path / "docs" / "plans"), recursive=False) == []


def test_normalize_walk_dir_lists_flat_and_sharded_handoffs(tmp_path):
    from coordinator_core.ops import normalize_claimed_frontmatter as mod

    flat = _write(tmp_path, "state/handoffs/2026-01-01-flat.md")
    sharded = _write(tmp_path, "state/handoffs/2026-11/2026-11-03-sharded.md")
    got = {os.path.normpath(p) for p in mod.walk_dir(str(tmp_path / "state" / "handoffs"))}
    assert got == {os.path.normpath(str(flat)), os.path.normpath(str(sharded))}


def test_orphan_memo_owning_text_reads_flat_and_sharded_artifacts(tmp_path):
    from coordinator_core.ops import deferral_detect_orphan_memo as mod

    _write(tmp_path, "docs/plans/2026-01-01-a.md", "FLATPLAN\n")
    _write(tmp_path, "docs/plans/2026-11/2026-11-03-b.md", "SHARDEDPLAN\n")
    _write(tmp_path, "state/handoffs/2026-11/2026-11-03-c.md", "SHARDEDBATON\n")
    blob, errs = mod._read_owning_text(tmp_path)
    assert not errs
    assert "FLATPLAN" in blob and "SHARDEDPLAN" in blob and "SHARDEDBATON" in blob
    slug_blob, _ = mod._read_owning_text(tmp_path, mod._SLUG_OWNING_GLOBS)
    assert "SHARDEDPLAN" in slug_blob and "SHARDEDBATON" not in slug_blob
