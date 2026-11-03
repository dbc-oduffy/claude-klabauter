"""Handoff directives: d1's `--out`, d2's lint `--file`, and d6's successor path all
carry the path coordinator-doc-new actually writes (the month shard), and an
existing flat file stays flat. Everything lives under tmp_path."""

from __future__ import annotations

from pathlib import Path

import coordinator_core.baton_assemble as ba

FLAT = "state/handoffs/2026-11-03-hosted-team-debug-token-f02d67.md"
SHARD = "state/handoffs/2026-11/2026-11-03-hosted-team-debug-token-f02d67.md"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)
    return repo


def _directives(repo: Path, out: str) -> dict[str, dict]:
    lineage = {
        "kind": "handoff",
        "artifact_path": "",
        "output_path": out,
        "deliverable_id": "d-x",
        "initiative": "x",
        "predecessor": None,
    }
    ds = ba._build_directives("handoff", lineage, title="t", root=repo)
    return {d["id"]: d for d in ds}


def _file_arg(directive: dict) -> str:
    args = directive["args"]
    return args[args.index("--file") + 1] if "--file" in args else args[0]


def test_flat_out_is_carried_as_the_month_shard_to_d1_and_d2(tmp_path):
    repo = _repo(tmp_path)

    ds = _directives(repo, FLAT)

    assert f"--out={SHARD}" in ds["d1"]["args"]
    assert _file_arg(ds["d2"]) == SHARD
    assert not any(FLAT in str(d["args"]) for d in ds.values())


def test_existing_flat_file_stays_flat(tmp_path):
    repo = _repo(tmp_path)
    (repo / FLAT).write_text("x", encoding="utf-8")

    ds = _directives(repo, FLAT)

    assert f"--out={FLAT}" in ds["d1"]["args"]
    assert ds["d1"]["already_satisfied"] is True
    assert _file_arg(ds["d2"]) == FLAT


def test_already_sharded_out_is_unchanged(tmp_path):
    repo = _repo(tmp_path)

    ds = _directives(repo, SHARD)

    assert f"--out={SHARD}" in ds["d1"]["args"]
    assert _file_arg(ds["d2"]) == SHARD


def test_existing_shard_marks_d1_satisfied(tmp_path):
    repo = _repo(tmp_path)
    (repo / SHARD).parent.mkdir(parents=True)
    (repo / SHARD).write_text("x", encoding="utf-8")

    ds = _directives(repo, FLAT)

    assert ds["d1"]["already_satisfied"] is True
