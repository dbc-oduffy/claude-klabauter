"""Reader and lookup tests for coordinator_core.design_registry (mirror of DoE's design-registry/1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import design_registry as dr

BOARD_URL = "https://claude.ai/artifact/fixture-canvas-001"
BOARD = "claude.ai/artifact/fixture-canvas-001"  # load keeps only the matched `claude.ai/artifact/<id>`, as DoE's does

REGISTRY = f"""schema: design-registry/1
entries:
  - id: settings
    board: {BOARD_URL}
    artboards: [route-settings-390, route-settings-1440]
    harvest: docs/design/x/harvest/
    sources:
      - "src/app/settings/page.tsx"
      - "src/components/settings/**"
    status: designed
    ratification: null
  - id: detail
    board: {BOARD_URL}
    artboards: [a]
    harvest: docs/design/x/harvest/
    sources:
      - 'src/app/(dashboard)/exec/[decisionId]/page.tsx'
    status: implemented
  - id: auth
    board: {BOARD_URL}
    artboards: [a]
    harvest: docs/design/y/harvest
    sources: [src/components/auth/*.tsx, "src/q?/x.ts"]
    status: partial
  - id: gate
    board: {BOARD_URL}
    artboards: [a]
    harvest: docs/design/x/harvest/
    sources: [src/components/gate/**]
    status: deviated
    ratification: docs/design/x/ratification.md
  - id: unbuilt
    board: {BOARD_URL}
    artboards: [a]
    harvest: docs/design/z/harvest/
    sources: []
    status: designed
"""


def _repo(tmp_path: Path, text: str | bytes | None = REGISTRY, rel: str = dr.DEFAULT_PATH) -> Path:
    if text is not None:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8", newline="")
    return tmp_path


def _swap(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


def test_constants():
    assert dr.SCHEMA_ID == "design-registry/1"
    assert dr.DEFAULT_PATH == "docs/design/registry.yaml"
    assert dr.LOCAL_KEY == "design_registry_path"


def test_absent_registry_is_none_none(tmp_path):
    assert dr.load(tmp_path) == (None, None)
    assert dr.lookup(tmp_path, ["src/a.ts"]) == ([], None)


def test_load_returns_doe_keys_and_values(tmp_path):
    entries, code = dr.load(_repo(tmp_path))
    assert code is None
    first = entries[0]
    assert set(first) == {"id", "board", "artboards", "harvest", "sources", "status", "ratification"}
    assert first["board"] == BOARD
    assert first["harvest"] == "docs/design/x/harvest"
    assert first["ratification"] is None
    assert entries[2]["harvest"] == "docs/design/y/harvest"
    assert entries[3]["ratification"] == "docs/design/x/ratification.md"
    assert entries[4]["sources"] == []


def test_bom_and_crlf_and_cr_parse(tmp_path):
    for variant in (REGISTRY.replace("\n", "\r\n"), REGISTRY.replace("\n", "\r")):
        payload = b"\xef\xbb\xbf" + variant.encode("utf-8")
        entries, code = dr.load(_repo(tmp_path, payload))
        assert code is None and len(entries) == 5


@pytest.mark.parametrize(
    "old,new",
    [
        ("schema: design-registry/1", "schema: design-registry/2"),
        (f"board: {BOARD_URL}\n    artboards: [route-settings-390", "board: https://example.com/x\n    artboards: [route-settings-390"),
        ("id: detail", "id: settings"),
        ("status: designed\n    ratification: null", "status: bogus\n    ratification: null"),
        ("artboards: [route-settings-390, route-settings-1440]", "artboards: []"),
        ("harvest: docs/design/y/harvest", "harvest: /"),
        ("ratification: docs/design/x/ratification.md", "ratification: null"),
        ("    sources: [src/components/gate/**]", "    sources: src/components/gate/**"),
        ("  - id: settings", "\t- id: settings"),
        ("    status: implemented", "    status: &anchor implemented"),
        ("  - id: detail", "  - detail"),
    ],
)
def test_reject_rules(tmp_path, old, new):
    text = _swap(REGISTRY, old, new)
    assert dr.load(_repo(tmp_path, text)) == (None, "registry-unparseable")


def test_board_must_end_at_the_id(tmp_path):
    text = _swap(REGISTRY, f"board: {BOARD_URL}\n    artboards: [route", f"board: {BOARD_URL} extra\n    artboards: [route")
    assert dr.load(_repo(tmp_path, text)) == (None, "registry-unparseable")


def test_board_url_prefix_is_trimmed_to_match(tmp_path):
    text = _swap(REGISTRY, f"board: {BOARD_URL}\n    artboards: [route", "board: https://www.claude.ai/artifact/abc-1\n    artboards: [route")
    entries, code = dr.load(_repo(tmp_path, text))
    assert code is None and entries[0]["board"] == "claude.ai/artifact/abc-1"


def test_deviated_needs_ratification(tmp_path):
    text = _swap(REGISTRY, "    ratification: docs/design/x/ratification.md\n", "")
    assert dr.load(_repo(tmp_path, text)) == (None, "registry-unparseable")


def test_invalid_utf8_is_unparseable(tmp_path):
    assert dr.load(_repo(tmp_path, b"schema: \xff\xfe\n")) == (None, "registry-unparseable")


def test_local_key_is_honoured(tmp_path):
    _repo(tmp_path, REGISTRY, "design/custom.yaml")
    (tmp_path / "coordinator.local.md").write_text(
        f"---\n{dr.LOCAL_KEY}: design/custom.yaml\n---\nbody\n", encoding="utf-8"
    )
    assert dr.registry_path(tmp_path) == (tmp_path / "design" / "custom.yaml").resolve()
    entries, code = dr.load(tmp_path)
    assert code is None and len(entries) == 5


def test_local_key_with_bom_crlf_and_quotes(tmp_path):
    _repo(tmp_path, REGISTRY, "design/custom.yaml")
    (tmp_path / "coordinator.local.md").write_bytes(
        b"\xef\xbb\xbf---\r\n" + f'{dr.LOCAL_KEY}: "design\\custom.yaml"\r\n---\r\n'.encode()
    )
    entries, code = dr.load(tmp_path)
    assert code is None and entries


def test_local_key_escaping_root_is_unparseable(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.yaml").write_text(REGISTRY, encoding="utf-8")
    (root / "coordinator.local.md").write_text(
        f"---\n{dr.LOCAL_KEY}: ../outside.yaml\n---\n", encoding="utf-8"
    )
    assert dr.load(root) == (None, "registry-unparseable")
    assert dr.lookup(root, ["x"]) == (None, "registry-unparseable")


@pytest.mark.parametrize(
    "glob,path,expected",
    [
        ("src/components/settings/**", "src/components/settings/a/b/c.tsx", True),
        ("src/components/settings/**", "src/components/settings", True),
        ("src/**/page.tsx", "src/page.tsx", True),
        ("src/**/page.tsx", "src/a/b/page.tsx", True),
        ("src/components/auth/*.tsx", "src/components/auth/a.tsx", True),
        ("src/components/auth/*.tsx", "src/components/auth/x/a.tsx", False),
        ("src/q?/x.ts", "src/qa/x.ts", True),
        ("src/q?/x.ts", "src/q/x.ts", False),
        ("src/q?/x.ts", "src/qa/b/x.ts", False),
        ("src/App/x.ts", "src/app/x.ts", False),
        ("src/app/(dashboard)/[id]/page.tsx", "src/app/(dashboard)/[id]/page.tsx", True),
        ("src/app/(dashboard)/[id]/page.tsx", "src/app/dashboard/i/page.tsx", False),
        ("src/app/(dashboard)/[id]/page.tsx", "src/app/(dashboard)/d/page.tsx", False),
        ("src\\components\\settings\\**", "src/components/settings/a.tsx", True),
        ("src/components/settings/**", "src\\components\\settings\\a.tsx", True),
        ("./src/a.ts", "src/a.ts", True),
        ("src/a.ts", "src/b.ts", False),
    ],
)
def test_matches(glob, path, expected):
    assert dr.matches(glob, path) is expected


def test_lookup_hits_shape_and_order(tmp_path):
    _repo(tmp_path)
    harvest = tmp_path / "docs/design/x/harvest"
    harvest.mkdir(parents=True)
    (harvest / "harvest.json").write_text("{}", encoding="utf-8")
    hits, code = dr.lookup(
        tmp_path,
        ["src/components/settings/x.tsx", "src/app/settings/page.tsx", "src/components/auth/a.tsx", "nope.py"],
    )
    assert code is None
    assert [h["id"] for h in hits] == ["settings", "auth"]
    settings, auth = hits
    assert settings == {
        "id": "settings",
        "board_url": BOARD,
        "artboards": ["route-settings-390", "route-settings-1440"],
        "status": "designed",
        "paths": ["src/components/settings/x.tsx", "src/app/settings/page.tsx"],
        "harvest_path": "docs/design/x/harvest/harvest.json",
    }
    assert "harvest_path" not in auth
    assert "harvest_expected_path" not in settings
    assert auth["harvest_expected_path"] == "docs/design/y/harvest/harvest.json"


def test_lookup_backslash_paths_hit(tmp_path):
    hits, _ = dr.lookup(_repo(tmp_path), ["src\\components\\settings\\a.tsx"])
    assert [h["id"] for h in hits] == ["settings"]


def test_empty_sources_never_hit(tmp_path):
    hits, _ = dr.lookup(_repo(tmp_path), ["", "docs/design/z/", "docs/design/z/harvest/harvest.json"])
    assert hits == []


def test_lookup_miss(tmp_path):
    assert dr.lookup(_repo(tmp_path), ["README.md"]) == ([], None)


def test_directory_path_uses_prefix_rule(tmp_path):
    repo = _repo(tmp_path)
    for directory in ("src/components/", "src/components/settings/", "src/components/settings/deep/", "src/"):
        hits, _ = dr.lookup(repo, [directory])
        assert "settings" in [h["id"] for h in hits], directory
    hits, _ = dr.lookup(repo, ["src/other/"])
    assert hits == []


def test_non_directory_path_gets_no_prefix_rule(tmp_path):
    repo = _repo(tmp_path)
    assert dr.lookup(repo, ["src/components"]) == ([], None)
    assert dr.lookup(repo, ["src/app/settings"]) == ([], None)
    assert dr.lookup(repo, ["src/app/settings/"])[0][0]["id"] == "settings"


def test_harvest_record_outside_root_is_omitted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "evil").mkdir()
    (tmp_path / "evil" / "harvest.json").write_text("{}", encoding="utf-8")
    text = _swap(REGISTRY, "harvest: docs/design/x/harvest/\n    sources:\n      - \"src/app/settings", "harvest: ../evil\n    sources:\n      - \"src/app/settings")
    hits, _ = dr.lookup(_repo(root, text), ["src/app/settings/page.tsx"])
    assert "harvest_path" not in hits[0]
    assert "harvest_expected_path" not in hits[0]


def test_lookup_spawns_nothing(tmp_path, monkeypatch):
    repo = _repo(tmp_path)

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    hits, code = dr.lookup(repo, ["src/components/settings/a.tsx"])
    assert code is None and hits


def test_module_imports_only_stdlib_and_frontmatter():
    import ast

    tree = ast.parse(Path(dr.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module if node.module.startswith("coordinator_core") else node.module.split(".")[0])
    assert mods <= {
        "__future__", "re", "functools", "pathlib", "typing",
        "coordinator_core.frontmatter.primitives",
    }
