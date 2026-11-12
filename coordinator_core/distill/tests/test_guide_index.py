from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.distill.guide_index import (
    RELEVANT_GUIDES_K,
    RagIndexUnavailable,
    build_index,
    rag_graph_db,
    rag_known_paths,
    ranked_guides_for_batches,
    relevant_guides_for_batches,
)


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _wiki(tmp_path: Path) -> dict[str, str]:
    _write(tmp_path, "docs/wiki/sharding.md", "---\ntitle: x\n---\n# Relay sharding\n## Byte caps\nShards relay truncation.\n")
    _write(tmp_path, "docs/wiki/guard-messaging.md", "# Guard messaging\n## Register\nDeny advisory text.\n")
    _write(tmp_path, "docs/wiki/fenced.md", "# Fenced\n```\n# not a heading\n```\n")
    _write(tmp_path, "docs/wiki/other-a.md", "# Other\nunrelated filler words\n")
    _write(tmp_path, "docs/wiki/other-b.md", "# Another\nmore filler content\n")
    return {
        s: f"docs/wiki/{s}.md"
        for s in ("sharding", "guard-messaging", "fenced", "other-a", "other-b")
    }


def test_batch_gets_the_guide_sharing_its_terms(tmp_path):
    slugs = _wiki(tmp_path)
    _write(tmp_path, "docs/specs/a.md", "The relay truncation of shards by byte caps.")
    _write(tmp_path, "docs/specs/b.md", "Guard messaging register for deny advisory.")
    out = relevant_guides_for_batches(tmp_path, [["docs/specs/a.md"], ["docs/specs/b.md"]], slugs)
    assert out[0][0]["slug"] == "sharding"
    assert out[1][0]["slug"] == "guard-messaging"
    assert out[0][0]["outline"] == ["# Relay sharding", "## Byte caps"]


def test_no_overlap_yields_empty_list_and_missing_files_do_not_raise(tmp_path):
    slugs = _wiki(tmp_path)
    out = relevant_guides_for_batches(tmp_path, [["docs/specs/gone.md"]], slugs)
    assert out == [[]]


def test_top_k_bound_and_deterministic_order(tmp_path):
    slugs = {}
    for i in range(20):
        slugs[f"g{i:02d}"] = f"docs/wiki/g{i:02d}.md"
        _write(tmp_path, slugs[f"g{i:02d}"], "# Topic\nquartz relay\n")
    for i in range(30):  # the matched term must stay rarer than half the corpus
        slugs[f"z{i:02d}"] = f"docs/wiki/z{i:02d}.md"
        _write(tmp_path, slugs[f"z{i:02d}"], f"# Zed\nfiller{i}\n")
    _write(tmp_path, "docs/specs/a.md", "quartz relay topic")
    a = relevant_guides_for_batches(tmp_path, [["docs/specs/a.md"]], slugs, k=RELEVANT_GUIDES_K)
    b = relevant_guides_for_batches(tmp_path, [["docs/specs/a.md"]], slugs, k=RELEVANT_GUIDES_K)
    assert a == b
    assert len(a[0]) == RELEVANT_GUIDES_K
    assert [g["slug"] for g in a[0]] == sorted(g["slug"] for g in a[0])  # equal scores tie on slug


def test_fenced_hash_lines_and_frontmatter_are_not_headings(tmp_path):
    slugs = _wiki(tmp_path)
    idx = build_index(tmp_path, slugs)
    by_slug = {g["slug"]: g for g in idx["guides"]}
    assert by_slug["fenced"]["outline"] == ["# Fenced"]
    assert by_slug["sharding"]["outline"][0] == "# Relay sharding"


def test_outline_stays_within_entry_byte_bound(tmp_path):
    _write(tmp_path, "docs/wiki/big.md", "".join(f"## {'h' * 80} {i}\n" for i in range(40)))
    idx = build_index(tmp_path, {"big": "docs/wiki/big.md"})
    import json

    entry = idx["guides"][0]
    assert len(json.dumps(entry, separators=(",", ":")).encode()) <= 450


def _make_rag(home: Path, repo: Path, headings: dict[str, list[str]], data_files=()) -> Path:
    import sqlite3

    db = home / "project-rag" / "indexes" / repo.name.replace("-", "_") / "root" / "graph.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE md_symbols (symbol_id TEXT, name TEXT, kind TEXT, heading_level INTEGER, "
        "file TEXT, line INTEGER)"
    )
    conn.execute("CREATE TABLE data_symbols (symbol_id TEXT, name TEXT, file TEXT)")
    for file, names in headings.items():
        for i, name in enumerate(names):
            conn.execute("INSERT INTO md_symbols VALUES (?,?,?,?,?,?)", (f"{file}{i}", name, "heading", 1 if i == 0 else 2, file, i + 1))
    for f in data_files:
        conn.execute("INSERT INTO data_symbols VALUES (?,?,?)", (f, f, f))
    conn.commit()
    conn.close()
    return db


def _rag_headings(slugs: dict[str, str]) -> dict[str, list[str]]:
    return {
        slugs["sharding"]: ["Relay sharding", "Byte caps"],
        slugs["guard-messaging"]: ["Guard messaging", "Register"],
        slugs["fenced"]: ["Fenced"],
        slugs["other-a"]: ["Other"],
        slugs["other-b"]: ["Another"],
    }


def test_rag_ranking_used_when_index_serves_the_repo(tmp_path):
    repo = tmp_path / "repo-x"
    repo.mkdir()
    home = tmp_path / "home"
    slugs = _wiki(repo)
    _write(repo, "docs/specs/a.md", "The relay truncation of shards by byte caps.")
    _make_rag(home, repo, _rag_headings(slugs))
    source, out = ranked_guides_for_batches(repo, [["docs/specs/a.md"]], slugs, settings_home_dir=home)
    assert source == "rag"
    assert out[0][0]["slug"] == "sharding"
    assert out[0][0]["outline"] == ["# Relay sharding", "## Byte caps"]


def test_missing_index_is_a_typed_refusal_naming_the_key_and_ranking_falls_back(tmp_path):
    repo = tmp_path / "repo-x"
    repo.mkdir()
    slugs = _wiki(repo)
    _write(repo, "docs/specs/a.md", "The relay truncation of shards by byte caps.")
    with pytest.raises(RagIndexUnavailable) as exc:
        rag_graph_db(repo, tmp_path / "empty-home")
    assert exc.value.key == "project-rag/indexes/repo_x/root/graph.db"
    source, out = ranked_guides_for_batches(repo, [["docs/specs/a.md"]], slugs, settings_home_dir=tmp_path / "empty-home")
    assert source == "lexical"
    assert out == relevant_guides_for_batches(repo, [["docs/specs/a.md"]], slugs)


def test_index_not_covering_the_guides_falls_back_to_lexical(tmp_path):
    repo = tmp_path / "repo-x"
    repo.mkdir()
    home = tmp_path / "home"
    slugs = _wiki(repo)
    _write(repo, "docs/specs/a.md", "The relay truncation of shards by byte caps.")
    _make_rag(home, repo, {"docs/elsewhere.md": ["Relay sharding"]})
    source, out = ranked_guides_for_batches(repo, [["docs/specs/a.md"]], slugs, settings_home_dir=home)
    assert source == "lexical"
    assert out[0][0]["slug"] == "sharding"


def test_index_without_md_symbols_table_falls_back(tmp_path):
    import sqlite3

    repo = tmp_path / "repo-x"
    repo.mkdir()
    home = tmp_path / "home"
    slugs = _wiki(repo)
    db = home / "project-rag" / "indexes" / "repo_x" / "root" / "graph.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    assert ranked_guides_for_batches(repo, [[]], slugs, settings_home_dir=home)[0] == "lexical"


def test_rag_read_never_writes_the_store(tmp_path):
    repo = tmp_path / "repo-x"
    repo.mkdir()
    home = tmp_path / "home"
    slugs = _wiki(repo)
    db = _make_rag(home, repo, _rag_headings(slugs))
    before = db.read_bytes()
    ranked_guides_for_batches(repo, [["docs/wiki/sharding.md"]], slugs, settings_home_dir=home)
    rag_known_paths(repo, ["docs/wiki/sharding.md"], home)
    assert db.read_bytes() == before


def test_rag_known_paths_reports_indexed_files_only(tmp_path):
    repo = tmp_path / "repo-x"
    repo.mkdir()
    home = tmp_path / "home"
    _make_rag(home, repo, {"docs/a.md": ["A"]}, data_files=["state/b.yaml"])
    got = rag_known_paths(repo, ["docs/a.md", "state/b.yaml", "docs/gone.md"], home)
    assert got == {"docs/a.md", "state/b.yaml"}
    with pytest.raises(RagIndexUnavailable):
        rag_known_paths(repo, ["docs/a.md"], tmp_path / "nohome")
