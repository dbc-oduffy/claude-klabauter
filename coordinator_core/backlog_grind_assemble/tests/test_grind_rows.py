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


def _record(**extra) -> dict:
    """A run record carrying every `RUN_RECORD_REQUIRED` key."""
    return {"profile": "bug", "run_id": "r", "counts": {}, "spend": {}, "handed_back": [], "halted": False, **extra}


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
    path.write_text(text, encoding="utf-8", newline="\n")
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
        "row_rel": "state/bug-backlog/bug-1.yaml",
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
                "--row", close_fixture["row_rel"],
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
                "--row", close_fixture["row_rel"],
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
                "--row", close_fixture["row_rel"],
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
                "--row", close_fixture["row_rel"],
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


class TestRowContainment:
    def test_absolute_row_outside_repo_root_is_refused_with_usage(self, close_fixture, tmp_path):
        other_root = tmp_path / "other-root"
        other_root.mkdir()
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
                "--repo-root", str(other_root),
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE
        assert close_fixture["row_path"].is_file()

    def test_escaping_row_is_refused_with_usage(self, close_fixture, tmp_path):
        outside = tmp_path.parent / "outside-row.yaml"
        outside.write_text(close_fixture["row_text"], encoding="utf-8")
        try:
            exit_code = grind_rows.main(
                [
                    "close",
                    "--profile-dir", str(close_fixture["profile_dir"]),
                    "--profile", "bug",
                    "--row", f"../{outside.name}",
                    "--digest", close_fixture["digest"],
                    "--verdict", "fix",
                    "--evidence-file", str(close_fixture["evidence_file"]),
                    "--closed-by", "test-session",
                    "--run-stamp", "2026-09-21T00:00:00Z",
                    "--repo-root", str(tmp_path),
                ]
            )
            assert exit_code == grind_rows.EXIT_USAGE
            assert outside.is_file()
        finally:
            outside.unlink()


class TestCrashWindowReconciliation:
    def test_rerun_after_replace_but_before_unlink_finishes_the_removal(self, close_fixture, tmp_path):
        first = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", close_fixture["row_rel"],
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert first == grind_rows.EXIT_OK
        archived = tmp_path / "archive" / "bug-backlog" / "2026-09" / "bug-1.yaml"
        assert archived.is_file()

        # Simulate the crash window: os.replace landed, unlink did not --
        # restore the source row so both copies exist simultaneously.
        close_fixture["row_path"].write_text(close_fixture["row_text"], encoding="utf-8", newline="\n")
        assert close_fixture["row_path"].is_file()

        second = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", close_fixture["row_rel"],
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert second == grind_rows.EXIT_OK
        assert archived.is_file()
        assert not close_fixture["row_path"].exists()

    def test_genuinely_conflicting_archive_destination_still_refuses(self, close_fixture, tmp_path):
        archive_dir = tmp_path / "archive" / "bug-backlog" / "2026-09"
        archive_dir.mkdir(parents=True)
        (archive_dir / "bug-1.yaml").write_text("id: other\nstatus: closed-fixed\n", encoding="utf-8")

        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", close_fixture["row_rel"],
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_REFUSAL
        assert close_fixture["row_path"].is_file()


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
                "--row", close_fixture["row_rel"],
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


# ---------------------------------------------------------------------------
# DR-276: each verb's actual writes/moves/deletes carry a session touch-claim
# when run through the SAME in-process route production uses --
# `entry_point_shim._backlog_grind_assemble_entry`'s `cli_entry.
# recording_declared_writes()` wrap around `grind_rows.main`, never a second
# recorder. Session id is resolved the same way `resolve_session_id` reads it
# (env-var ladder), matching the `_make_repo`/env-var fixture pattern in
# `coordinator_core/tests/test_ipc_scope_touch_self_report.py`.
#
# Spec backlink: cross-repo memo engine-bugs-routed-from-doe-bug-grind
# (DoE queue-grind blocker: ledger/archive/run-record writes reached through
# grind-row verbs carried no session claim and were refused at
# `scoped_git_commit`).
# ---------------------------------------------------------------------------


def _make_repo(tmp_path):
    """A plain `.git` directory is enough for `git_common_dir`'s in-process
    walk (no subprocess) to resolve this as a repo root -- no real `git
    init`/commit needed, since none of these tests observe git's own
    working-tree status (unlike `TestCloseVerbGitIndex` below, which does)."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _touched_paths(repo, sid):
    from coordinator_core.session import touch_record

    p = Path(repo) / ".git" / "coordinator-sessions" / sid / "touch-record.jsonl"
    if not p.is_file():
        return []
    raw = p.read_bytes()
    return [
        touch_record.decode_line(line).path
        for line in touch_record.iter_complete_lines(raw)
    ]


def _run_grind_row_recording(argv, cwd):
    from coordinator_core.cli_entry import recording_declared_writes

    with recording_declared_writes(cwd=str(cwd)):
        return grind_rows.main(argv)


@pytest.mark.spawns_process
@pytest.mark.cadence
class TestDR276DeclaredWrites:
    def _set_session(self, monkeypatch, sid):
        monkeypatch.setenv("CLAUDE_SESSION_ID", sid)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    def test_append_records_a_touch_claim_for_the_ledger_file(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        self._set_session(monkeypatch, "sid-append")

        exit_code = _run_grind_row_recording(
            [
                "append",
                "--profile", "bug",
                "--row-id", "row-1",
                "--digest", "d" * 64,
                "--stage", "triage",
                "--verdict", "confirmed-bug",
                "--outcome", "confirmed-bug",
                "--evidence-file", "evidence.txt",
                "--run-stamp", "2026-09-22T00:00:00Z",
                "--repo-root", str(repo),
            ],
            cwd=repo,
        )
        assert exit_code == grind_rows.EXIT_OK
        assert "state/queue-grind/bug/row-1.jsonl" in _touched_paths(repo, "sid-append")

    def test_append_records_a_touch_claim_when_process_cwd_diverges_from_repo_root(
        self, tmp_path, monkeypatch
    ):
        """state/bug-backlog/2026-09-22-ledger-only-grind-commits-deny-on-
        path-scope-for-run-scratch-files.yaml: `cmd_append` used to call bare
        `declare_write(ledger_path)`, which records under the OUTER
        `recording_declared_writes()` scope `entry_point_shim._backlog_
        grind_assemble_entry` opens with no `cwd=` (defaulting to
        `os.getcwd()`) -- never under the verb's own explicit `--repo-root`.
        A dispatched agent's process cwd is routinely NOT `--repo-root` (its
        own workspace dir, here a non-repo `elsewhere/`), so the claim landed
        against the WRONG (or no) `caller_repo_root` and was silently
        skipped -- the ledger-only committer then denied on path scope,
        reading `orphan -- no session holds a claim on it`. This pins the
        fix: the claim must land under `--repo-root` regardless of process
        cwd."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo = _make_repo(repo_dir)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        self._set_session(monkeypatch, "sid-append-cwd")

        exit_code = _run_grind_row_recording(
            [
                "append",
                "--profile", "bug",
                "--row-id", "row-1",
                "--digest", "d" * 64,
                "--stage", "triage",
                "--verdict", "confirmed-bug",
                "--outcome", "confirmed-bug",
                "--evidence-file", "evidence.txt",
                "--run-stamp", "2026-09-22T00:00:00Z",
                "--repo-root", str(repo),
            ],
            cwd=elsewhere,
        )
        assert exit_code == grind_rows.EXIT_OK
        assert "state/queue-grind/bug/row-1.jsonl" in _touched_paths(repo, "sid-append-cwd")

    def test_close_records_touch_claims_for_both_new_and_removed_paths(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git").mkdir()
        self._set_session(monkeypatch, "sid-close")

        row_path = tmp_path / "state" / "bug-backlog" / "bug-1.yaml"
        row_text = _write_row(row_path)
        profile_dir = tmp_path / "queue-profiles"
        _write_profile(profile_dir, archive_path="archive/bug-backlog", schema_rel="schema/bug.schema.json")
        _write_schema(tmp_path / "schema" / "bug.schema.json")
        evidence_file = tmp_path / "evidence.txt"
        evidence_file.write_text("Confirmed fixed by the batch's own verify stage.", encoding="utf-8")
        digest = _sha256(row_text)

        exit_code = _run_grind_row_recording(
            [
                "close",
                "--profile-dir", str(profile_dir),
                "--profile", "bug",
                "--row", "state/bug-backlog/bug-1.yaml",
                "--digest", digest,
                "--verdict", "fix",
                "--evidence-file", str(evidence_file),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-22T00:00:00Z",
                "--repo-root", str(tmp_path),
            ],
            cwd=tmp_path,
        )
        assert exit_code == grind_rows.EXIT_OK
        touched = _touched_paths(tmp_path, "sid-close")
        assert "state/bug-backlog/bug-1.yaml" in touched
        assert any(p.startswith("archive/bug-backlog/2026-09/") and p.endswith("bug-1.yaml") for p in touched)

    def test_settle_records_a_touch_claim_for_the_deleted_ledger(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        self._set_session(monkeypatch, "sid-settle")
        ledger_dir = repo / "state" / "queue-grind" / "bug"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "row-1.jsonl").write_text('{"row_id": "row-1"}\n', encoding="utf-8")

        exit_code = _run_grind_row_recording(
            ["settle", "--profile", "bug", "--row-id", "row-1", "--repo-root", str(repo)],
            cwd=repo,
        )
        assert exit_code == grind_rows.EXIT_OK
        assert "state/queue-grind/bug/row-1.jsonl" in _touched_paths(repo, "sid-settle")

    def test_run_record_records_a_touch_claim_for_the_run_record(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        self._set_session(monkeypatch, "sid-run-record")
        record_file = repo / "record.json"
        record_file.write_text(json.dumps(_record()), encoding="utf-8")

        exit_code = _run_grind_row_recording(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000000Z",
                "--record-file", str(record_file),
                "--repo-root", str(repo),
            ],
            cwd=repo,
        )
        assert exit_code == grind_rows.EXIT_OK
        assert "state/queue-grind/bug/runs/20260922T000000Z.json" in _touched_paths(repo, "sid-run-record")


class TestRunRecordVerb:
    def test_writes_the_record_atomically_under_repo_root(self, tmp_path, capsys):
        record_file = tmp_path / "record.json"
        record_file.write_text(json.dumps(_record(a=1, b=2)), encoding="utf-8")

        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000000Z",
                "--record-file", str(record_file),
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        target = tmp_path / "state" / "queue-grind" / "bug" / "runs" / "20260922T000000Z.json"
        assert json.loads(target.read_text(encoding="utf-8")) == _record(a=1, b=2)
        # No leftover temp file.
        assert list(target.parent.glob(".*.tmp-*")) == []

    def test_reads_a_percent_encoded_record_argument(self, tmp_path, capsys):
        from urllib.parse import quote

        record = _record(reason="it's — café")
        exit_code = grind_rows.main(
            [
                "run-record", "--profile", "bug", "--run-id", "20260922T000000Z",
                "--record-urlenc", quote(json.dumps(record), safe=""),
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        target = tmp_path / "state" / "queue-grind" / "bug" / "runs" / "20260922T000000Z.json"
        assert json.loads(target.read_text(encoding="utf-8")) == record

    def test_record_file_and_urlenc_together_is_usage(self, tmp_path, capsys):
        exit_code = grind_rows.main(
            [
                "run-record", "--profile", "bug", "--run-id", "20260922T000000Z",
                "--record-file", "-", "--record-urlenc", "%7B%7D",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE

    def test_reads_from_stdin_when_record_file_is_a_dash(self, tmp_path, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_record(x=1))))
        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000001Z",
                "--record-file", "-",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        target = tmp_path / "state" / "queue-grind" / "bug" / "runs" / "20260922T000001Z.json"
        assert json.loads(target.read_text(encoding="utf-8")) == _record(x=1)

    def test_reads_from_stdin_when_record_file_is_absent(self, tmp_path, monkeypatch, capsys):
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_record(y=2))))
        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000004Z",
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        target = tmp_path / "state" / "queue-grind" / "bug" / "runs" / "20260922T000004Z.json"
        assert json.loads(target.read_text(encoding="utf-8")) == _record(y=2)

    def test_run_id_escaping_the_runs_directory_is_a_usage_error(self, tmp_path):
        record_file = tmp_path / "record.json"
        record_file.write_text(json.dumps(_record()), encoding="utf-8")
        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "../escape",
                "--record-file", str(record_file),
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE

    def test_malformed_json_is_a_usage_error(self, tmp_path):
        record_file = tmp_path / "record.json"
        record_file.write_text("not json", encoding="utf-8")
        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000002Z",
                "--record-file", str(record_file),
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE

    def test_missing_repo_root_is_a_usage_error(self, tmp_path):
        record_file = tmp_path / "record.json"
        record_file.write_text("{}", encoding="utf-8")
        exit_code = grind_rows.main(
            [
                "run-record",
                "--profile", "bug",
                "--run-id", "20260922T000003Z",
                "--record-file", str(record_file),
            ]
        )
        assert exit_code == grind_rows.EXIT_USAGE


class TestSweepSettlesOrphanedLedgers:
    def test_sweep_settles_only_rows_the_queue_no_longer_yields(self, tmp_path):
        # A hand closure `git mv`s the row out without settling; a restored row
        # at the same id would inherit the stale ledger's route-to-* mark.
        profile_dir = tmp_path / "queue-profiles"
        _write_profile(profile_dir, archive_path="archive/bug-backlog", schema_rel="schema/bug.schema.json")
        queue = tmp_path / "state" / "bug-backlog"
        queue.mkdir(parents=True)
        (queue / "live.yaml").write_text("id: bug-live\nstatus: open\n", encoding="utf-8")
        (queue / "filtered.yaml").write_text("id: bug-filtered\nstatus: parked\n", encoding="utf-8")
        ledger_dir = tmp_path / "state" / "queue-grind" / "bug"
        (ledger_dir / "_handback").mkdir(parents=True)
        for row_id in ("bug-live", "bug-filtered", "bug-gone"):
            (ledger_dir / f"{row_id}.jsonl").write_text('{"row_id": "%s"}\n' % row_id, encoding="utf-8")
        (ledger_dir / "_handback" / "mark-gone.json").write_text(
            json.dumps({"row": "bug-gone", "type": "route-to-debt", "reason": "x"}), encoding="utf-8"
        )
        (ledger_dir / "_handback" / "mark-live.json").write_text(
            json.dumps({"row": "bug-live", "type": "park", "reason": "x"}), encoding="utf-8"
        )

        rc = grind_rows.main(
            [
                "sweep",
                "--profile-dir", str(profile_dir),
                "--profile", "bug",
                "--queue", str(queue),
                "--repo-root", str(tmp_path),
            ]
        )

        assert rc == grind_rows.EXIT_OK
        assert sorted(p.name for p in ledger_dir.glob("*.jsonl")) == ["bug-filtered.jsonl", "bug-live.jsonl"]
        assert sorted(p.name for p in (ledger_dir / "_handback").glob("*.json")) == ["mark-live.json"]

    def test_sweep_without_a_queue_is_a_usage_error_and_deletes_nothing(self, tmp_path):
        ledger = tmp_path / "state" / "queue-grind" / "bug" / "bug-1.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("{}\n", encoding="utf-8")
        rc = grind_rows.main(
            ["sweep", "--profile-dir", str(tmp_path), "--profile", "bug", "--repo-root", str(tmp_path)]
        )
        assert rc == grind_rows.EXIT_USAGE
        assert ledger.is_file()


class TestArchiveDestinationIsTheMoversPath:
    def test_close_writes_exactly_the_path_archive_destination_names(self, close_fixture, tmp_path, capsys):
        stamp = "20261002T010203Z"
        exit_code = grind_rows.main(
            [
                "close",
                "--profile-dir", str(close_fixture["profile_dir"]),
                "--profile", "bug",
                "--row", close_fixture["row_rel"],
                "--digest", close_fixture["digest"],
                "--verdict", "fix",
                "--evidence-file", str(close_fixture["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", stamp,
                "--repo-root", str(tmp_path),
            ]
        )
        assert exit_code == grind_rows.EXIT_OK
        dest = grind_rows.archive_destination("archive/bug-backlog", stamp, "bug-1.yaml")
        assert dest == "archive/bug-backlog/2026-10/bug-1.yaml"
        assert (tmp_path / dest).is_file()
        printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert Path(printed["new"]) == tmp_path / dest


def test_evidence_rel_names_the_one_evidence_home():
    assert (
        grind_rows.evidence_rel("bug", "[run-id]", "<item-id>", "done")
        == "state/queue-grind/bug/evidence/[run-id]/<item-id>.done.md"
    )
    assert grind_rows.evidence_rel("bug", "r1", "i1", "verify") == (
        "state/queue-grind/bug/evidence/r1/i1.verify.md"
    )


class TestAppendEvidenceFileRefusal:
    @staticmethod
    def _argv(tmp_path, evidence):
        return [
            "append",
            "--profile", "bug",
            "--row-id", "bug-1",
            "--digest", "abc123",
            "--stage", "triage",
            "--verdict", "confirmed",
            "--outcome", "profile-declared",
            "--evidence-file", evidence,
            "--run-stamp", "2026-09-21T00:00:00Z",
            "--repo-root", str(tmp_path),
        ]

    @pytest.mark.parametrize(
        "evidence",
        [
            "state/scratch/x.md",
            "state\\scratch\\x.md",
            "scratch/x.md",
            "a/scratch/b/x.md",
            "../outside.md",
            "/elsewhere/outside.md",
            "C:\\elsewhere\\outside.md",  # abs-path-ok: refusal fixture for a Windows drive path
        ],
    )
    def test_refused_without_writing(self, tmp_path, capsys, evidence):
        assert grind_rows.main(self._argv(tmp_path, evidence)) == grind_rows.EXIT_USAGE
        assert not (tmp_path / "state" / "queue-grind").exists()
        assert "state/queue-grind/<profile>/evidence" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "evidence",
        [
            "state/queue-grind/bug/evidence/r1/i1.done.md",
            "evidence.txt",
            "scratchy/x.md",
        ],
    )
    def test_admitted_relative_and_absolute(self, tmp_path, evidence):
        assert grind_rows.main(self._argv(tmp_path, evidence)) == grind_rows.EXIT_OK
        absolute = str(tmp_path / "state" / "queue-grind" / "bug" / "evidence" / "r1" / "i1.done.md")
        assert grind_rows.main(self._argv(tmp_path, absolute)) == grind_rows.EXIT_OK
        stored = (tmp_path / "state/queue-grind/bug/bug-1.jsonl").read_text(encoding="utf-8")
        assert json.dumps(evidence)[1:-1] in stored


def test_evidence_rel_rejects_unknown_kind():
    with pytest.raises(ValueError):
        grind_rows.evidence_rel("bug", "r1", "i1", "notes")


class TestCloseReleasesRowClaim:
    @pytest.fixture
    def claimed(self, close_fixture, tmp_path, monkeypatch):
        from coordinator_core.session import claims, liveness
        from coordinator_core.tests.git_seed import seeded_repo

        seeded_repo(tmp_path, readme="x")
        live: set = set()
        monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live)
        for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)

        def as_session(sid):
            monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
            live.add(sid)

        key = claims.row_claim_key(close_fixture["row_rel"])
        close_fixture.update(
            claims=claims,
            as_session=as_session,
            claim_dir=tmp_path / ".git" / "coordinator-sessions" / "row-claims" / key,
            root=tmp_path,
        )
        return close_fixture

    def _close(self, fx, *, digest=None):
        return grind_rows.main(
            [
                "close",
                "--profile-dir", str(fx["profile_dir"]),
                "--profile", "bug",
                "--row", fx["row_rel"],
                "--digest", digest or fx["digest"],
                "--verdict", "fix",
                "--evidence-file", str(fx["evidence_file"]),
                "--closed-by", "test-session",
                "--run-stamp", "2026-09-21T00:00:00Z",
                "--repo-root", str(fx["root"]),
            ]
        )

    def _claim(self, fx):
        return fx["claims"].claim_artifact(
            "row", fx["row_rel"], baton_repo_root=str(fx["root"]), cwd=str(fx["root"])
        )

    def test_closers_own_claim_is_released(self, claimed):
        claimed["as_session"]("sid-a")
        assert self._claim(claimed)
        assert claimed["claim_dir"].is_dir()
        assert self._close(claimed) == grind_rows.EXIT_OK
        assert not claimed["claim_dir"].exists()

    def test_reconcile_path_also_releases(self, claimed):
        claimed["as_session"]("sid-a")
        assert self._close(claimed) == grind_rows.EXIT_OK
        claimed["row_path"].write_text(claimed["row_text"], encoding="utf-8", newline="\n")
        assert self._claim(claimed)
        assert self._close(claimed) == grind_rows.EXIT_OK
        assert not claimed["claim_dir"].exists()

    def test_peer_live_claim_is_untouched(self, claimed):
        claimed["as_session"]("sid-a")
        assert self._claim(claimed)
        claimed["as_session"]("sid-b")
        assert self._close(claimed) == grind_rows.EXIT_OK
        assert (claimed["claim_dir"] / "session_id").read_text().strip() == "sid-a"

    def test_digest_mismatch_refusal_keeps_claim(self, claimed):
        claimed["as_session"]("sid-a")
        assert self._claim(claimed)
        assert self._close(claimed, digest="0" * 64) == grind_rows.EXIT_MANIFEST_STALE
        assert claimed["claim_dir"].is_dir()

    def test_close_without_claim_succeeds(self, claimed):
        claimed["as_session"]("sid-a")
        assert self._close(claimed) == grind_rows.EXIT_OK
        assert not claimed["claim_dir"].exists()

    def test_close_records_zero_spawns(self, claimed, monkeypatch):
        from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

        claimed["as_session"]("sid-a")
        assert self._claim(claimed)
        with _count_spawns_attributed(monkeypatch) as spawns:
            assert self._close(claimed) == grind_rows.EXIT_OK
        assert list(spawns) == []
        assert not claimed["claim_dir"].exists()


class TestRunRecordLandsTheRunsBookkeeping:
    """`run-record` commits the run's own ledgers, sweep, evidence, record and
    block-discharge row in one commit, in a real tmp repo."""

    SID = "sid-a"

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        from coordinator_core.session import touch_record
        from coordinator_core.tests.git_seed import seeded_repo

        seeded_repo(tmp_path, readme="x")
        for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("COORDINATOR_SESSION_ID", self.SID)
        live = {"sid-b"}
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: sid in live)
        home = tmp_path / "state" / "queue-grind" / "bug"
        home.mkdir(parents=True)
        for name in ("old-1", "old-2", "keep"):
            (home / f"{name}.jsonl").write_text('{"row_id": "%s"}\n' % name, encoding="utf-8")
        assert _git(["add", "-A"], tmp_path).returncode == 0
        assert _git(["commit", "-m", "seed ledgers"], tmp_path).returncode == 0
        return tmp_path

    def _run_record(self, repo, capsys, run_id="20261015T000000Z", record=None, extra=()):
        from urllib.parse import quote

        capsys.readouterr()
        code = grind_rows.main(
            ["run-record", "--profile", "bug", "--run-id", run_id,
             "--record-urlenc", quote(json.dumps(record or _record()), safe=""),
             "--repo-root", str(repo), *extra]
        )
        captured = capsys.readouterr()
        return code, captured

    def _sweep_and_append(self, repo):
        for row in ("old-1", "old-2"):
            assert grind_rows.main(["settle", "--profile", "bug", "--row-id", row, "--repo-root", str(repo)]) == 0
        assert grind_rows.main(
            ["append", "--profile", "bug", "--row-id", "new-1", "--digest", "d" * 64, "--stage", "triage",
             "--verdict", "fix", "--outcome", "fix", "--evidence-file", "state/queue-grind/bug/evidence/r/new-1.done.md",
             "--run-stamp", "20261015T000000Z", "--repo-root", str(repo)]
        ) == 0
        evidence = repo / grind_rows.evidence_rel("bug", "r", "new-1", "done")
        evidence.parent.mkdir(parents=True)
        evidence.write_text("done\n", encoding="utf-8")
        grind_rows._declare_under_repo_root(evidence, repo)

    def test_everything_lands_in_one_commit_and_the_tree_is_clean(self, repo, capsys):
        self._sweep_and_append(repo)
        discharge = repo / "state" / "block-discharge" / f"{self.SID}.jsonl"
        discharge.parent.mkdir(parents=True)
        discharge.write_text('{"fired": 1}\n', encoding="utf-8")
        before = _git(["rev-list", "--count", "HEAD"], repo).stdout.strip()

        code, captured = self._run_record(repo, capsys)

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["commit"] and out["withheld"] == []
        assert out["deleted"] == [
            "state/queue-grind/bug/old-1.jsonl",
            "state/queue-grind/bug/old-2.jsonl",
        ]
        assert set(out["landed"]) == {
            "state/queue-grind/bug/new-1.jsonl",
            "state/queue-grind/bug/evidence/r/new-1.done.md",
            "state/queue-grind/bug/runs/20261015T000000Z.json",
            f"state/block-discharge/{self.SID}.jsonl",
        }
        assert _git(["rev-list", "--count", "HEAD"], repo).stdout.strip() == str(int(before) + 1)
        assert _git(["status", "--porcelain"], repo).stdout == ""
        subject = _git(["log", "-1", "--format=%s"], repo).stdout.strip()
        assert subject == "grind(bug): run 20261015T000000Z bookkeeping, removed 2 settled ledgers"

    def test_a_ledger_a_live_peer_claims_is_withheld_and_left_dirty(self, repo, capsys):
        from coordinator_core.session import scope, touch_record

        self._sweep_and_append(repo)
        keep = repo / "state" / "queue-grind" / "bug" / "keep.jsonl"
        keep.write_text('{"row_id": "keep", "changed": 1}\n', encoding="utf-8")
        grind_rows._declare_under_repo_root(keep, repo)
        peer_file = "state/queue-grind/bug/keep.jsonl"
        scope.touch("sid-b", peer_file, str(repo), kind=touch_record.KIND_WRITE)

        code, captured = self._run_record(repo, capsys)

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["withheld"] == [peer_file]
        assert peer_file not in out["landed"]
        assert _git(["status", "--porcelain"], repo).stdout.split() == ["M", peer_file]

    def test_a_run_with_nothing_swept_or_appended_still_lands_its_record(self, repo, capsys):
        code, captured = self._run_record(repo, capsys)

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["landed"] == ["state/queue-grind/bug/runs/20261015T000000Z.json"]
        assert out["deleted"] == [] and out["commit"]
        assert _git(["status", "--porcelain"], repo).stdout == ""

    def test_no_resolvable_session_writes_the_record_and_commits_nothing(self, repo, capsys, monkeypatch):
        monkeypatch.delenv("COORDINATOR_SESSION_ID")

        code, captured = self._run_record(repo, capsys)

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["commit"] is None and out["landed"] == []

    def test_a_commit_refusal_exits_nonzero_with_the_verbatim_reason(self, repo, capsys, monkeypatch):
        from coordinator_core.ops.ceremony import commit_v2

        monkeypatch.setattr(
            commit_v2, "_handler", lambda params, repo_root=None: {"committed": False, "sha": None, "error": "gate says no"}
        )

        code, captured = self._run_record(repo, capsys)

        assert code == grind_rows.EXIT_REFUSAL
        assert "gate says no" in captured.err

    def test_plain_bookkeeping_spawns_no_git_process(self, repo, capsys, monkeypatch):
        from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

        self._sweep_and_append(repo)
        capsys.readouterr()
        with _count_spawns_attributed(monkeypatch) as spawns:
            code, captured = self._run_record(repo, capsys)
        assert code == grind_rows.EXIT_OK, captured.err
        assert json.loads(captured.out)["commit"]
        assert list(spawns) == []

    # -- widened run-close land set ------------------------------------------------

    def _write(self, repo, rel, text, *, claim=True):
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if claim:
            grind_rows._declare_under_repo_root(path, repo)

    def _decisions(self, repo, **keys):
        path = repo.parent / f"{repo.name}-decisions.json"
        path.write_text(json.dumps(keys), encoding="utf-8")
        return ["--decisions-file", str(path)]

    def _widened_extra(self, repo, declared, decisions=()):
        from coordinator_core.ops.dispatch_emit.grind_commit_contract import encode_declared_paths

        return ["--queue", "state/bug-backlog", "--declared-urlenc", encode_declared_paths(declared), *decisions]

    def test_queue_edit_declared_sizing_baton_and_minted_row_land_in_one_commit(self, repo, capsys):
        self._write(repo, "state/bug-backlog/bug-1.yaml", "status: open\n", claim=False)
        assert _git(["add", "-A"], repo).returncode == 0
        assert _git(["commit", "-m", "seed row"], repo).returncode == 0
        self._write(repo, "state/bug-backlog/bug-1.yaml", "status: open\nnote: handed back\n")
        self._write(repo, "state/sizings/s.yaml", "size: s\n", claim=False)
        self._write(repo, "state/handoffs/b.md", "---\ncarried_items: []\n---\nbaton\n", claim=False)
        self._write(repo, "state/bug-backlog/TF-9.yaml", "status: open\n", claim=False)
        before = _git(["rev-list", "--count", "HEAD"], repo).stdout.strip()
        extra = self._widened_extra(
            repo, ["state/sizings/s.yaml", "state/handoffs/b.md"],
            self._decisions(repo, minted=["state/bug-backlog/TF-9.yaml"], baseline_dirty=[]),
        )

        code, captured = self._run_record(repo, capsys, extra=extra)

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert set(out["landed"]) == {
            "state/bug-backlog/bug-1.yaml", "state/sizings/s.yaml", "state/handoffs/b.md",
            "state/bug-backlog/TF-9.yaml", "state/queue-grind/bug/runs/20261015T000000Z.json",
        }
        assert _git(["rev-list", "--count", "HEAD"], repo).stdout.strip() == str(int(before) + 1)
        assert _git(["status", "--porcelain"], repo).stdout == ""

    def test_a_baseline_dirty_claim_does_not_land(self, repo, capsys):
        self._write(repo, "state/sizings/old.yaml", "predates the run\n")
        self._write(repo, "state/sizings/new.yaml", "this run\n")
        extra = self._widened_extra(repo, [], self._decisions(repo, baseline_dirty=["state/sizings/old.yaml"]))

        code, captured = self._run_record(repo, capsys, extra=extra)

        assert code == grind_rows.EXIT_OK, captured.err
        landed = json.loads(captured.out)["landed"]
        assert "state/sizings/new.yaml" in landed and "state/sizings/old.yaml" not in landed
        assert _git(["status", "--porcelain"], repo).stdout.split() == ["??", "state/sizings/old.yaml"]

    def test_a_peer_contested_declared_path_is_withheld_and_named(self, repo, capsys):
        from coordinator_core.session import scope, touch_record

        self._write(repo, "state/handoffs/b.md", "---\ncarried_items: []\n---\nbaton\n", claim=False)
        scope.touch("sid-b", "state/handoffs/b.md", str(repo), kind=touch_record.KIND_WRITE)

        code, captured = self._run_record(repo, capsys, extra=self._widened_extra(repo, ["state/handoffs/b.md"]))

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["withheld"] == ["state/handoffs/b.md"]
        assert "state/handoffs/b.md" not in out["landed"]

    def test_a_declared_path_outside_the_roots_never_lands(self, repo, capsys):
        self._write(repo, "docs/elsewhere.md", "x\n", claim=False)

        code, captured = self._run_record(repo, capsys, extra=self._widened_extra(repo, ["docs/elsewhere.md"]))

        assert code == grind_rows.EXIT_OK, captured.err
        assert "docs/elsewhere.md" not in json.loads(captured.out)["landed"]
        assert _git(["status", "--porcelain", "-uall"], repo).stdout.split() == ["??", "docs/elsewhere.md"]

    def test_a_record_missing_a_required_key_exits_usage_and_writes_nothing(self, repo, capsys):
        record = _record()
        del record["handed_back"]

        code, captured = self._run_record(repo, capsys, record=record)

        assert code == grind_rows.EXIT_USAGE
        assert "handed_back" in captured.err
        assert not (repo / "state/queue-grind/bug/runs/20261015T000000Z.json").exists()

    def test_a_tradeoff_is_written_verbatim_beside_the_evidence_and_lands(self, repo, capsys):
        text = "it's a trade — café\nsecond line"
        record = _record(handed_back=[
            {"row": "bug-1", "type": "needs-judgment", "reason": "fix reported a tradeoff", "tradeoff": text},
            {"row": "bug-2", "type": "needs-judgment", "reason": "r"},
        ])

        code, captured = self._run_record(repo, capsys, record=record)

        assert code == grind_rows.EXIT_OK, captured.err
        rel = grind_rows.evidence_rel("bug", "20261015T000000Z", "bug-1", "judgment")
        assert (repo / rel).read_text(encoding="utf-8") == text
        assert rel in json.loads(captured.out)["landed"]
        assert not (repo / grind_rows.evidence_rel("bug", "20261015T000000Z", "bug-2", "judgment")).exists()
        assert _git(["status", "--porcelain"], repo).stdout == ""

    # -- run-record --sweep ----------------------------------------------------------

    def _sweep_extra(self, repo, profile_dir):
        queue = repo / "state" / "bug-backlog"
        queue.mkdir(parents=True, exist_ok=True)
        (queue / "keep.yaml").write_text("id: keep\nstatus: open\n", encoding="utf-8")
        return ["--sweep", "--profile-dir", str(profile_dir), "--queue", "state/bug-backlog"]

    def test_sweep_settles_the_ledgers_and_lands_the_unlinks_in_the_records_commit(self, repo, capsys):
        profile_dir = repo.parent / f"{repo.name}-profiles"
        _write_profile(profile_dir, archive_path="archive/bug-backlog", schema_rel="schema/bug.schema.json")

        code, captured = self._run_record(repo, capsys, extra=self._sweep_extra(repo, profile_dir))

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["swept"] is True and out["sweep_reason"] == ""
        assert out["commit"]
        assert out["deleted"] == [f"state/queue-grind/bug/{n}.jsonl" for n in ("old-1", "old-2")]
        assert _git(["status", "--porcelain"], repo).stdout.split() == ["??", "state/bug-backlog/"]
        record = json.loads((repo / "state/queue-grind/bug/runs/20261015T000000Z.json").read_text(encoding="utf-8"))
        assert record["handed_back"] == []

    def test_a_refused_sweep_still_writes_the_record_and_names_the_failure(self, repo, capsys):
        from coordinator_core.ops.dispatch_emit.grind_commit_contract import SWEEP_FAILED_HEAD

        missing_profiles = repo.parent / f"{repo.name}-no-profiles"

        code, captured = self._run_record(repo, capsys, extra=self._sweep_extra(repo, missing_profiles))

        assert code == grind_rows.EXIT_OK, captured.err
        out = json.loads(captured.out)
        assert out["swept"] is False and out["sweep_reason"].startswith("profile refused")
        assert out["deleted"] == []
        record = json.loads((repo / "state/queue-grind/bug/runs/20261015T000000Z.json").read_text(encoding="utf-8"))
        dead = [h for h in record["handed_back"] if h["type"] == "stage-dead"]
        assert len(dead) == 1 and dead[0]["row"] == "20261015T000000Z"
        assert dead[0]["reason"].startswith(
            f"{SWEEP_FAILED_HEAD}: state/queue-grind/bug/runs/20261015T000000Z.json: profile refused"
        )
        assert record["counts"]["by_type"]["stage-dead"] == 1
        assert (repo / "state/queue-grind/bug/old-1.jsonl").is_file()

    def test_sweep_without_a_profile_dir_or_a_queue_is_a_usage_error_and_writes_no_record(self, repo, capsys):
        record_path = repo / "state/queue-grind/bug/runs/20261015T000000Z.json"
        no_profile_dir = self._run_record(repo, capsys, extra=["--sweep", "--queue", "state/bug-backlog"])
        no_queue = self._run_record(repo, capsys, extra=["--sweep", "--profile-dir", str(repo)])

        for code, captured in (no_profile_dir, no_queue):
            assert code == grind_rows.EXIT_USAGE
            assert "--sweep needs" in captured.err
        assert not record_path.exists()

    def test_the_widened_land_set_spawns_no_git_process(self, repo, capsys, monkeypatch):
        from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed

        self._write(repo, "state/sizings/s.yaml", "size: s\n", claim=False)
        extra = self._widened_extra(repo, ["state/sizings/s.yaml"])
        with _count_spawns_attributed(monkeypatch) as spawns:
            code, captured = self._run_record(repo, capsys, extra=extra)
        assert code == grind_rows.EXIT_OK, captured.err
        assert "state/sizings/s.yaml" in json.loads(captured.out)["landed"]
        assert list(spawns) == []
