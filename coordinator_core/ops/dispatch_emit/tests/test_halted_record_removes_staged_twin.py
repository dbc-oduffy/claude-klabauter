"""A halted-record commit with an unsafe run id removes no staged twin.

Token-gated reaping is pinned in test_halted_record_twin_reap_after_token.py."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_record_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM

RUN = "ask-20261023T000000-aaaaaa"
SHA = "a" * 64


def _twin(root: Path, run: str) -> Path:
    path = root / "scratch" / "warp" / f"{run}.staged-{SHA}.workflow.mjs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// twin\n", encoding="utf-8")
    return path


def _commit(root: Path, run_id: str) -> dict:
    return halted_record_commit.commit_halted_record(
        root, root / ".git", {"run_id": run_id, HALTED_RECORD_PARAM: {"halted": "refusal"}, "run_token": "bad"}
    )


def test_unsafe_run_id_removes_nothing(tmp_path):
    twin = _twin(tmp_path, RUN)
    out = _commit(tmp_path, "../x")
    assert out.get("refused") == "run-id-unsafe", out
    assert twin.exists()
