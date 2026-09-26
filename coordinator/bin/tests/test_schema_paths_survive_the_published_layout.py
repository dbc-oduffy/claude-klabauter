"""The schema-reaching CLIs must resolve their schemas in the PUBLISHED layout.

Claude-klabauter#30: `coordinator-doc-new --sizing-object` crashed in the
published mirror, where `coordinator/bin/` lands one directory shallower as
`bin/`. The schema path was a fixed `__file__.parent.parent.parent` walk that was
correct only in the authoring tree, so every test run there passed.

Each arm copies the CLI into a flattened `<tmp>/bin/` — the published shape —
and runs the code path that reads the schema. A `__file__` walk from there lands
outside the tree and fails; a path taken off the imported
`coordinator_core.frontmatter` package resolves in both layouts.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import shutil
from pathlib import Path

import yaml

_BIN_DIR = Path(__file__).resolve().parent.parent

_VALID_SIZING = (
    "schema: sizing-object\n"
    "intent: example\n"
    "estimate:\n"
    "  tshirt: S\n"
    "  provisional: true\n"
    "route: dispatch\n"
    "detents: []\n"
    "fork: null\n"
    "xl_exit: null\n"
    "premise:\n"
    "  provenance: not-applicable\n"
    "  evidence: example\n"
    "status: open\n"
    "plan: null\n"
)


def _load_from_published_layout(tmp_path: Path, name: str):
    published_bin = tmp_path / "published" / "bin"
    published_bin.mkdir(parents=True)
    copy = published_bin / name
    shutil.copyfile(_BIN_DIR / name, copy)
    shutil.copytree(_BIN_DIR / "lib", published_bin / "lib", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    mod_name = "published_" + name.replace("-", "_").removesuffix(".py")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(copy))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_sizing_reverse_edge_validates_from_the_published_layout(tmp_path):
    cli = _load_from_published_layout(tmp_path, "coordinator-doc-new.py")

    new_text = cli._mutate_sizing_reverse_edge(
        _VALID_SIZING, "docs/plans/2026-09-19-published-layout.md"
    )

    assert yaml.safe_load(new_text)["plan"] == "docs/plans/2026-09-19-published-layout.md"


def test_plan_spine_schema_resolves_from_the_published_layout(tmp_path):
    check = _load_from_published_layout(tmp_path, "plan-spine-check.py")

    assert check._schema_path().is_file()
