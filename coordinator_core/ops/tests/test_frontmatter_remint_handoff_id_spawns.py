"""handoff_id resolver for frontmatter.remint: derive, refuse referenced / colliding / underivable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.frontmatter_remint import handoff_id

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _write(root: Path, rel: str, handoff_id_value: str | None, extra: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    hid = f"handoff_id: {handoff_id_value}\n" if handoff_id_value is not None else ""
    p.write_text(f"---\ntitle: x\n{hid}{extra}---\nbody\n", encoding="utf-8")
    return p


def test_a_many_path_call_spawns_no_git_grep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No citer scan on any path of a call: the git-grep scan was over the DR-344 kill bar."""
    from coordinator_core.ops import frontmatter_remint

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    rels = [f"state/handoffs/2026-10-1{i}-thing-{i}.md" for i in range(4)]
    for i, rel in enumerate(rels):
        _write(tmp_path, rel, f"hnd-thing-{i}")
    (tmp_path / "state" / "ledger.json").write_text('{"x": "hnd-thing-2"}\n', encoding="utf-8")
    real = subprocess.run
    calls: list[list[str]] = []

    def counting(argv, *a, **k):
        if argv[:1] == ["git"] and "grep" in argv:
            calls.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", counting)
    out = frontmatter_remint._handler({"paths": rels, "fields": ["handoff_id"]}, repo_root=tmp_path)
    assert calls == []
    outcomes = {r["path"]: r["fields"][0] for r in out["results"]}
    assert outcomes[rels[2]]["outcome"] == "written"
    assert "hnd-thing-2" in (tmp_path / rels[2]).read_text(encoding="utf-8").split("handoff_id_aliases:")[1]
