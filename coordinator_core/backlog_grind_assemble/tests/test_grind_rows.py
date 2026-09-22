"""
coordinator_core.backlog_grind_assemble.tests.test_grind_rows — pytest for
`grind_rows.py`'s four verbs (`check`, `append`, `close`, `settle`).

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Row verbs, Tasks § C4.

Covers, per the row's own Tests bullet:
  - close leaves the index untouched (an unstaged delete plus an untracked
    add, per a real `git status --porcelain` in a tmp repo);
  - digest refusal (exit EXIT_MANIFEST_STALE on a mismatched or vanished row);
  - formatting of untouched fields preserved byte-for-byte;
  - append idempotence under a retried agent;
  - settle deletes only its own row's file.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows

# One test class spawns a real `git` process (in a tmp repo) to observe the
# actual working-tree status `close` leaves behind -- the whole point of that
# assertion is that no git PLUMBING call of ours does the staging, so a faked
# subprocess would prove nothing. Marked per the sibling
# test_backlog_grind_assemble.py convention (spawn ratchet:
# coordinator_core/tests/test_no_new_spawning_tests.py).
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_row(path: Path, *, status: str = "open") -> str:
    text = (
        "---\n"
        "id: bug-1\n"
        f"status: {status}\n"
        "title: a bug with an untouched title field\n"
        "---\n"
        "Body text describing the bug. Untouched by close.\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def _write_profile(profile_dir: Path, *, archive_path: str, schema_rel: str) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_text = (
        "row_id_key: id\n"
        "batch_key: [status]\n"
        "priority: {field: status, order: asc}\n"
        "verdicts: [confirmed, refuted]\n"
        "graph:\n"
        "  triage:\n"
        "    kind: triage\n"
        "    edges: {}\n"
        "closure:\n"
        "  status_field: status\n"
        "  closed_values:\n"
        "    fix: closed-fixed\n"
        "    refute-close: closed-refuted\n"
        "  stamp_fields: [closed_by, closed_at]\n"
        "triage_policy: |\n"
        "  Triage per fixture.\n"
        "appetite:\n"
        "  standard:\n"
        "    concurrency: 1\n"
        "    extra_verification: false\n"
        "    batch_size: 4\n"
        "    triage_depth: standard\n"
        "    window: 1\n"
        "    max_agent_calls: 10\n"
        f"archive_path: {archive_path}\n"
        f"schema: {schema_rel}\n"
    )
    (profile_dir / "bug.yaml").write_text(profile_text, encoding="utf-8")


def _write_schema(schema_path: Path) -> None:
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "title": {"type": "string"},
                    "closed_by": {"type": "string"},
                    "closed_at": {"type": "string"},
                },
                "required": ["id", "status"],
            }
        ),
        encoding="utf-8",
    )


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def close_fixture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    row_path = tmp_path / "state" / "bug-backlog" / "bug-1.yaml"
    row_text = _write_row(row_path)
    profile_dir = tmp_path / "queue-profiles"
    _write_profile(profile_dir, archive_path="archive/bug-backlog", schema_rel="schema/bug.schema.json")
    _write_schema(tmp_path / "schema" / "bug.schema.json")
    evidence_file = tmp_path / "evidence.txt"
    evidence_file.write_text("Confirmed fixed by the batch's own verify stage.", encoding="utf-8")
    return {
        "row_path": row_path,
        "row_text": row_text,
        "profile_dir": profile_dir,
        "evidence_file": evidence_file,
        "digest": _sha256(row_text),
    }


class TestCloseLeavesIndexUntouched:
    def test_close_leaves_an_unstaged_delete_and_untracked_add(self, close_fixture, tmp_path):
        init = _git(["init"], tmp_path)
        assert init.returncode == 0
        _git(["add", "-A"], tmp_path)
        _git(["-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-m", "initial"], tmp_path)

        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", str(close_fixture["row_path"]),
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK

        status = _git(["status", "--porcelain"], tmp_path)
        lines = status.stdout.splitlines()
        assert any(line.startswith(" D ") and "bug-1.yaml" in line for line in lines), lines
        # git reports a wholly-untracked new directory by its own top-level
        # name ("?? archive/"), not by the file inside it -- the point of
        # this assertion is that the moved-to path is untracked/new, not
        # staged, regardless of how git chooses to summarize it.
        assert any(line.startswith("?? ") and "archive" in line for line in lines), lines


class TestDigestRefusal:
    def test_mismatched_digest_refuses_with_manifest_stale(self, close_fixture, tmp_path):
        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", str(close_fixture["row_path"]),
                "--digest", "0" * 64,
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_MANIFEST_STALE
        assert close_fixture["row_path"].is_file(), "a refused close must not move the row"

    def test_vanished_row_refuses_with_manifest_stale(self, close_fixture, tmp_path):
        close_fixture["row_path"].unlink()
        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", str(close_fixture["row_path"]),
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_MANIFEST_STALE


class TestUntouchedFormattingPreservedByteForByte:
    def test_untouched_title_field_and_body_survive_byte_for_byte(self, close_fixture, tmp_path):
        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", str(close_fixture["row_path"]),
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        new_path = tmp_path / "archive/bug-backlog/2026-09/bug-1.yaml"
        new_text = new_path.read_text(encoding="utf-8")
        assert "title: a bug with an untouched title field\n" in new_text
        assert "Body text describing the bug. Untouched by close.\n" in new_text
        assert "status: closed-fixed" in new_text
        assert "closed_by: test-session" in new_text
        # serialize_yaml_scalar quotes a value containing a structural ':',
        # so an ISO timestamp round-trips quoted -- assert on the value
        # reaching the field, not on a specific quoting style.
        assert "closed_at:" in new_text and "2026-09-21T00:00:00Z" in new_text
        assert "CLOSED 2026-09-21T00:00:00Z" in new_text


class TestAppendIdempotence:
    def test_retried_append_does_not_duplicate_the_line(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        argv = [
            "append",
            "--profile", "bug",
            "--row-id", "bug-1",
            "--digest", "abc123",
            "--stage", "triage",
            "--verdict", "confirmed",
            "--outcome", "profile-declared",
            "--evidence-file", "evidence.txt",
            "--run-stamp", "2026-09-21T00:00:00Z",
            "--repo-root", str(tmp_path),
        ]
        assert grind_rows.main(argv) == grind_rows.EXIT_OK
        assert grind_rows.main(argv) == grind_rows.EXIT_OK

        ledger_path = Path("state/queue-grind/bug/bug-1.jsonl")
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

    def test_a_changed_field_appends_a_second_distinct_line(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        base_argv = [
            "append",
            "--profile", "bug",
            "--row-id", "bug-1",
            "--digest", "abc123",
            "--stage", "triage",
            "--verdict", "confirmed",
            "--outcome", "profile-declared",
            "--evidence-file", "evidence.txt",
            "--run-stamp", "2026-09-21T00:00:00Z",
            "--repo-root", str(tmp_path),
        ]
        assert grind_rows.main(base_argv) == grind_rows.EXIT_OK
        other_argv = list(base_argv)
        other_argv[other_argv.index("--stage") + 1] = "fix"
        assert grind_rows.main(other_argv) == grind_rows.EXIT_OK

        ledger_path = Path("state/queue-grind/bug/bug-1.jsonl")
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2


class TestSettleDeletesOnlyItsOwnRowFile:
    def test_settle_removes_only_the_named_row_ledger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ledger_dir = Path("state/queue-grind/bug")
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "bug-1.jsonl").write_text('{"row_id": "bug-1"}\n', encoding="utf-8")
        (ledger_dir / "bug-2.jsonl").write_text('{"row_id": "bug-2"}\n', encoding="utf-8")

        exit_code = grind_rows.main(
            ["settle", "--profile", "bug", "--row-id", "bug-1", "--repo-root", str(tmp_path)]
        )

        assert exit_code == grind_rows.EXIT_OK
        assert not (ledger_dir / "bug-1.jsonl").exists()
        assert (ledger_dir / "bug-2.jsonl").exists()

    def test_settle_on_an_already_settled_row_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        exit_code = grind_rows.main(
            ["settle", "--profile", "bug", "--row-id", "never-existed", "--repo-root", str(tmp_path)]
        )
        assert exit_code == grind_rows.EXIT_OK


class TestRepoRootRequired:
    def test_append_without_repo_root_is_a_usage_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        argv = [
            "append",
            "--profile", "bug",
            "--row-id", "bug-1",
            "--digest", "abc123",
            "--stage", "triage",
            "--verdict", "confirmed",
            "--outcome", "profile-declared",
            "--evidence-file", "evidence.txt",
            "--run-stamp", "2026-09-21T00:00:00Z",
        ]
        assert grind_rows.main(argv) == grind_rows.EXIT_USAGE

    def test_close_without_repo_root_is_a_usage_error(self, close_fixture):
        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", str(close_fixture["row_path"]),
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE

    def test_settle_without_repo_root_is_a_usage_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        exit_code = grind_rows.main(["settle", "--profile", "bug", "--row-id", "never-existed"])
        assert exit_code == grind_rows.EXIT_USAGE


class TestCheckVerb:
    def test_stale_and_vanished_rows_are_reported(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        row_a = tmp_path / "a.yaml"
        row_a.write_text("---\nid: a\n---\nbody\n", encoding="utf-8")
        digest_a = _sha256(row_a.read_text(encoding="utf-8"))

        row_b = tmp_path / "b.yaml"
        row_b.write_text("---\nid: b\n---\nbody\n", encoding="utf-8")
        # row_b's manifest digest deliberately wrong -- stale.
        manifest = [
            {"row_id": "a", "path": str(row_a), "digest": digest_a, "batch_key": "batch-1"},
            {"row_id": "b", "path": str(row_b), "digest": "0" * 64, "batch_key": "batch-1"},
            {"row_id": "c", "path": str(tmp_path / "c.yaml"), "digest": "0" * 64, "batch_key": "batch-1"},
            {"row_id": "d", "path": str(row_a), "digest": digest_a, "batch_key": "other-batch"},
        ]
        script_path = tmp_path / "script.mjs"
        manifest_const = {"entries": manifest, "digest": "0" * 64}
        script_path.write_text(
            f"const QUEUE_GRIND_MANIFEST = {json.dumps(manifest_const)};\n"
            f"const BATCHES = {json.dumps([{'batch_key': 'batch-1', 'id': 'batch-1:b0', 'rows': ['a', 'b', 'c']}, {'batch_key': 'other-batch', 'id': 'other-batch:b0', 'rows': ['d']}])};\n"
            "console.log('ok');\n",
            encoding="utf-8",
        )

        exit_code = grind_rows.main(
            ["check", "--manifest", str(script_path), "--batch", "batch-1:b0", "--repo-root", str(tmp_path)]
        )
        assert exit_code == grind_rows.EXIT_OK
        out = json.loads(capsys.readouterr().out)
        assert out["stale"] == ["b"]
        assert out["vanished"] == ["c"]
