"""A subagent's bare `rm` of a tracked path needs the dispatch marker that declares it (fixtures under tmp_path)."""

from __future__ import annotations

import json
import statistics
import struct
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import block_subagent_destructive_action as guard
from coordinator_core.guard_messages.testing import envelope_of

RUN = "warp-2026-10-25-demo-20261025T000000-abcdef"
ROW = "C4"
SESSION = "sess-emitter"
MARK = f"COORDINATOR_DISPATCH_ROW={RUN}/{ROW}"
DECLARED = "pkg/a.py"
TRACKED_OTHER = "pkg/b.py"
UNTRACKED = "pkg/new.txt"


def _write_index(root: Path, names) -> None:
    out = bytearray(b"DIRC" + struct.pack(">II", 2, len(names)))
    for name in sorted(names):
        raw = name.encode()
        entry = b"\0" * 60 + struct.pack(">H", min(len(raw), 0x0FFF)) + raw + b"\0"
        out += entry + b"\0" * ((8 - len(entry) % 8) % 8)
    out += b"\0" * 20
    (root / ".git" / "index").write_bytes(bytes(out))


def _write_run(root: Path, *, writes=(DECLARED,)) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_DIR_ROOT

    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    manifest = {
        "run_dir": str(rec),
        "rows": [{"id": ROW, "agent_type": "x", "model": "m", "brief_path": "b.md", "writes": list(writes), "wave": 0}],
        "review_declared_paths": [],
        "marker_path": "m",
    }
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    run_dir = root / RUN_DIR_ROOT
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{RUN}.workflow.mjs.emitted.json").write_text(json.dumps({"session_id": SESSION}), encoding="utf-8")


@pytest.fixture(autouse=True)
def _channel_live(monkeypatch):
    monkeypatch.setattr(guard, "_DECLARED_DELETE_CHANNEL_LIVE", True)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    for rel in (DECLARED, TRACKED_OTHER, UNTRACKED):
        (tmp_path / rel).write_text("x", encoding="utf-8")
    _write_index(tmp_path, [DECLARED, TRACKED_OTHER])
    _write_run(tmp_path)
    return tmp_path


def _check(repo, command, *, agent=True):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": SESSION,
        "cwd": str(repo),
    }
    if agent:
        payload["agent_id"] = "deadbeef0123"
        payload["agent_type"] = "coordinator:executor"
    return envelope_of(guard.check(payload))


def test_bare_rm_of_tracked_path_denied(repo):
    assert _check(repo, f"rm -- {TRACKED_OTHER}") is not None
    assert _check(repo, f"rm {DECLARED}") is not None


def test_marker_declaring_the_path_admits(repo):
    assert _check(repo, f"{MARK} rm -- {DECLARED}") is None


@pytest.mark.parametrize(
    "command",
    [
        f"{MARK} rm -- {TRACKED_OTHER}",
        f"{MARK} rm -- {DECLARED} {TRACKED_OTHER}",
        f"{MARK} rm -rf {DECLARED}",
        f"{MARK} rm -f {DECLARED}",
        f"COORDINATOR_DISPATCH_ROW=other-{RUN}/{ROW} rm -- {DECLARED}",
        f"COORDINATOR_DISPATCH_ROW={RUN}/NOPE rm -- {DECLARED}",
        f"{MARK} rm -- {DECLARED} && COORDINATOR_DISPATCH_ROW={RUN}/X rm -- {UNTRACKED}",
    ],
)
def test_undeclared_or_forced_stays_denied(repo, command):
    assert _check(repo, command) is not None


@pytest.mark.parametrize(
    "command",
    [
        f"{MARK} true; rm -- {DECLARED}",
        f"{MARK} true && rm -- {DECLARED}",
        f"export {MARK}; rm -- {DECLARED}",
        f"{MARK}; rm -- {DECLARED}",
        f"env {MARK} rm -- {DECLARED}",
        f"rm -- {TRACKED_OTHER}; {MARK} rm -- {DECLARED}",
    ],
)
def test_marker_on_another_segment_licenses_nothing(repo, command):
    assert _check(repo, command) is not None


def test_marker_on_own_segment_admits_beside_other_segments(repo):
    assert _check(repo, f"echo hi; {MARK} rm -- {DECLARED}") is None
    assert _check(repo, f"{MARK} rm -- {DECLARED}; rm -- {UNTRACKED}") is None


def test_untracked_file_admitted_with_or_without_marker(repo):
    assert _check(repo, f"rm -- {UNTRACKED}") is None
    assert _check(repo, f"{MARK} rm -- {UNTRACKED}") is None


def test_dash_leading_operand_after_dashdash_is_an_operand(repo):
    (repo / "-weird").write_text("x", encoding="utf-8")
    _write_index(repo, [DECLARED, TRACKED_OTHER, "-weird"])
    assert _check(repo, "rm -- -weird") is not None


def test_operand_outside_repo_unchanged(repo, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "f.txt"
    outside.write_text("x", encoding="utf-8")
    assert _check(repo, f"rm {outside.as_posix()}") is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -- $TARGET",
        f"{MARK} rm -- $TARGET",
        f"cd pkg && rm -- {UNTRACKED}",
        f"cd pkg && {MARK} rm -- a.py",
        f"cd pkg && {MARK} git rm -q -- a.py",
        "pushd pkg; rm -- new.txt",
    ],
)
def test_unresolvable_operand_or_delete_after_cd_refused(repo, command):
    env = _check(repo, command)
    assert env is not None
    assert "cannot resolve" in json.dumps(env)


def test_cd_after_the_delete_does_not_refuse(repo):
    assert _check(repo, f"{MARK} rm -- {DECLARED} && cd pkg") is None


def test_dollar_operand_outside_a_repo_unchanged(tmp_path_factory):
    plain = tmp_path_factory.mktemp("norepo")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -- $TARGET"},
        "session_id": SESSION,
        "cwd": str(plain),
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    assert envelope_of(guard.check(payload)) is None


def _set_ignorecase(repo, value: bool) -> None:
    (repo / ".git" / "config").write_text(f"[core]\n\tignorecase = {str(value).lower()}\n", encoding="utf-8")


def test_case_insensitive_repo_folds_tracked_and_declared(repo):
    _set_ignorecase(repo, True)
    assert _check(repo, "rm -- PKG/B.PY") is not None
    assert _check(repo, f"{MARK} rm -- PKG/B.PY") is not None
    assert _check(repo, f"{MARK} rm -- PKG/A.PY") is None


def test_case_sensitive_repo_keeps_exact_case(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pkg").mkdir()
    for rel in (DECLARED, TRACKED_OTHER):
        (tmp_path / rel).write_text("x", encoding="utf-8")
    _write_index(tmp_path, [DECLARED, TRACKED_OTHER])
    _write_run(tmp_path)
    _set_ignorecase(tmp_path, False)
    assert _check(tmp_path, "rm -- pkg/B.PY") is None
    assert _check(tmp_path, f"{MARK} rm -- pkg/A.PY") is None
    assert _check(tmp_path, "rm -- pkg/b.py") is not None


def test_glob_matching_one_tracked_and_one_untracked_denied(repo):
    assert _check(repo, "rm -- pkg/*.py pkg/new.*") is not None
    assert _check(repo, "rm -- pkg/new.*") is None
    assert _check(repo, "rm -- pkg/zzz*") is None


def test_marker_never_licenses_a_glob_with_a_tracked_match(repo):
    _write_index(repo, [DECLARED])
    (repo / "pkg" / "b.py").unlink()
    assert _check(repo, f"{MARK} rm -- pkg/*.py") is not None


def test_unreadable_index_fails_closed(repo):
    (repo / ".git" / "index").write_bytes(b"garbage")
    assert _check(repo, f"rm -- {UNTRACKED}") is not None
    assert _check(repo, f"{MARK} rm -- {DECLARED}") is None


def test_em_payload_unaffected(repo):
    assert _check(repo, f"rm -- {TRACKED_OTHER}", agent=False) is None


def test_deny_message_names_marker_form(repo):
    env = _check(repo, f"rm -- {TRACKED_OTHER}")
    assert "COORDINATOR_DISPATCH_ROW=<run_id>/<row_id> rm --" in json.dumps(env)


def test_untracked_rm_process_time_with_50k_index_entries(repo):
    _write_index(repo, [f"d{i // 100}/f{i}.py" for i in range(50_000)] + [DECLARED])
    samples = []
    for _ in range(20):
        start = time.process_time()
        verdict = _check(repo, f"rm -- {UNTRACKED}")
        samples.append(time.process_time() - start)
        assert verdict is None
    median = statistics.median(samples)
    print(f"median check() process time, 50k-entry index: {median * 1000:.1f}ms")
    assert median < 0.05
