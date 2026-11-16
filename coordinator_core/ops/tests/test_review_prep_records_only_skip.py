"""review.prep over record-only paths answers a typed records-only skip; any product path still slices."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.record_paths import record_prefix_js_regex
from coordinator_core.ops.review_mint import execute_review
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RECORD = "state/run-evidence/ask-x/evidence.md"
RECORD_JSON = "state/run-evidence/ask-x/family-stage-result.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tracked.py").write_text("t = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _prep(repo: Path, paths: list) -> dict:
    head = _git(repo, "rev-parse", "HEAD").strip()
    return rp.review_prep(
        repo, base=head, head=head, paths=paths, slice_prefix="prep-r", plan_path="docs/plans/p.md",
        session_id="sess-own",
    )


def test_records_only_paths_return_a_typed_skip_not_an_empty_halt_shape(repo):
    (repo / RECORD_JSON).parent.mkdir(parents=True)
    (repo / RECORD_JSON).write_text("{}\n", encoding="utf-8")
    (repo / RECORD).write_text("evidence\n", encoding="utf-8")
    reply = _prep(repo, [RECORD, RECORD_JSON])
    assert reply["error"] is None
    assert reply["review_skip"] == {"kind": "records-only", "paths": [RECORD, RECORD_JSON]}
    assert reply["slices"] == []


def test_a_product_path_beside_records_is_never_skipped(repo):
    (repo / "tracked.py").write_text("t = 2\n", encoding="utf-8")
    (repo / RECORD).parent.mkdir(parents=True)
    (repo / RECORD).write_text("evidence\n", encoding="utf-8")
    reply = _prep(repo, [RECORD, "tracked.py"])
    assert not reply.get("review_skip")


def test_a_computed_reply_carries_a_null_review_skip_and_stores_the_key(repo):
    (repo / "tracked.py").write_text("t = 2\n", encoding="utf-8")
    reply = _prep(repo, ["tracked.py"])
    assert reply["error"] is None and reply["review_skip"] is None
    assert "review_skip" in rp._STORED_KEYS


def test_the_emitted_guard_treats_the_skip_as_verify_only_and_embeds_the_shared_regex():
    guard = execute_review._PREP_GUARDS_JS
    assert "_reviewPrep?.review_skip?.kind === 'records-only'" in guard.split("const _verifyOnly", 1)[1].split(";\n", 1)[0]
    assert f"/{record_prefix_js_regex()}/" in execute_review._DECLARED_PRODUCT_WRITE_JS
    assert f"/{record_prefix_js_regex()}/" in execute_review._NO_PRODUCT_LANDED_JS
