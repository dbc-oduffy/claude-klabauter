"""
coordinator_core.ops.tests.test_goal_kr_status — tests for goal.set_kr_status
(coordinator_core.ops.goal_kr_status).

Coverage:
  (a) single-process correctness: known kr_id rewritten in place, all other
      bytes (comments, other KR entries, the body: block) preserved.
  (b) fail-loud: unknown kr_id, no key_results: block, and a matched entry with
      no status: field each raise ValueError with zero writes.
  (c) missing goal file raises FileNotFoundError (locked_rmw's missing_ok=False
      default) with zero writes.
  (d) AUTHORITATIVE cross-process proof: two concurrent writers targeting
      DIFFERENT key_results[] entries in the SAME goal YAML both persist their
      update — the scenario this op exists for (parallel plan-arc executors
      reporting KR progress into one shared goal file).
  (e) write provenance: status_source/status_set_at created when absent,
      updated in place when present, idempotent across repeated calls (no
      duplicate lines), correct ISO-8601 shape, and scoped to the matched
      entry only — a sibling key_results[] entry in the same file is never
      stamped.

POSIX guard mirrors test_locked_write.py: skipped if neither fcntl nor msvcrt
is available.

Spec backlink: state/handoffs/2026-07-25_001110_slate-shared-substrate-extractions.md
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

try:
    import fcntl as _fcntl  # noqa: F401
    _FCNTL_AVAILABLE = True
except ImportError:
    _FCNTL_AVAILABLE = False

try:
    import msvcrt as _msvcrt  # noqa: F401
    _MSVCRT_AVAILABLE = True
except ImportError:
    _MSVCRT_AVAILABLE = False

_LOCKING_AVAILABLE = _FCNTL_AVAILABLE or _MSVCRT_AVAILABLE
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.parent.resolve())

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
    pytest.mark.skipif(
        not _LOCKING_AVAILABLE,
        reason="locked_rmw needs a file-lock backend (fcntl or msvcrt) — neither available",
    ),
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from coordinator_core.ops.goal_kr_status import set_kr_status  # noqa: E402

_GOAL_YAML = textwrap.dedent(
    """\
    schema: goal
    id: "goal-test"
    title: "test-goal"
    status: active
    key_results:
      - id: kr-1
        text: "first KR"
        kind: outcome
        status: not-started
        weekly_perceptible: true
      - id: kr-2
        text: "second KR"
        kind: output
        status: not-started
        weekly_perceptible: false
    created: 2026-07-25
    period: repo
    period_value: "Q3-2026"

    body: |
      # test-goal

      Some free-form body text with a status: word in it that must NOT be touched.
    """
)


def _make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "goal-kr-status-test@claude-klabauter.test")
    _git("config", "user.name", "Goal KR Status Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


# ---------------------------------------------------------------------------
# (a) Single-process correctness + preservation
# ---------------------------------------------------------------------------


class TestSingleProcessCorrectness:
    def test_rewrites_matched_status_line_only(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        result = set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)

        assert result == {"goal_file": str(goal_file), "kr_id": "kr-1", "status": "in-progress"}
        new_text = goal_file.read_text(encoding="utf-8")
        assert "status: in-progress" in new_text
        assert "kr-2\n    text: \"second KR\"\n    kind: output\n    status: not-started" in new_text
        assert "Some free-form body text with a status: word in it that must NOT be touched." in new_text

    def test_same_status_value_still_stamps_provenance(self, tmp_path):
        """Re-writing the SAME status value is not a byte-identical no-op:
        provenance stamping means every write records status_source/
        status_set_at, even when the status scalar itself is unchanged. Only
        the matched kr-1 entry gains the two provenance lines; every other
        byte (kr-2's entry, the body: block, comments) is untouched.
        """
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        set_kr_status(goal_file, "kr-1", "not-started", repo_root=repo)
        new_text = goal_file.read_text(encoding="utf-8")

        assert new_text != _GOAL_YAML
        assert "    status: not-started\n    status_source: goal.set_kr_status\n" in new_text
        assert re.search(
            r"status_set_at: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", new_text
        )
        # kr-2's entry and the body: block are untouched — no provenance stamped there.
        kr2_entry = new_text.split("id: kr-2", 1)[1].split("\ncreated:", 1)[0]
        assert (
            "\n    text: \"second KR\"\n    kind: output\n    status: not-started\n"
            "    weekly_perceptible: false" == kr2_entry
        )
        assert "status_source" not in kr2_entry
        assert "status_set_at" not in kr2_entry
        assert "Some free-form body text with a status: word in it that must NOT be touched." in new_text


# ---------------------------------------------------------------------------
# (e) Write provenance — stamping, idempotency, isolation
# ---------------------------------------------------------------------------


class TestProvenance:
    _TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def _kr1_entry(self, text: str) -> str:
        return text.split("id: kr-1", 1)[1].split("\n  - id: kr-2", 1)[0]

    def test_provenance_created_when_absent(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        assert "status_source" not in _GOAL_YAML
        assert "status_set_at" not in _GOAL_YAML

        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)
        kr1_entry = self._kr1_entry(goal_file.read_text(encoding="utf-8"))

        assert kr1_entry.count("status_source: goal.set_kr_status") == 1
        assert kr1_entry.count("status_set_at:") == 1

    def test_provenance_updated_in_place_when_present(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)
        first_text = goal_file.read_text(encoding="utf-8")
        first_stamp = re.search(r"status_set_at: (\S+)", self._kr1_entry(first_text)).group(1)

        # A tiny sleep guarantees the second-precision stamp can differ; not
        # required for correctness (idempotent overwrite is the assertion),
        # but keeps this test meaningful rather than a coincidental pass.
        import time

        time.sleep(1.1)

        set_kr_status(goal_file, "kr-1", "done", repo_root=repo)
        second_text = goal_file.read_text(encoding="utf-8")
        kr1_entry = self._kr1_entry(second_text)

        assert kr1_entry.count("status_source: goal.set_kr_status") == 1
        assert kr1_entry.count("status_set_at:") == 1
        second_stamp = re.search(r"status_set_at: (\S+)", kr1_entry).group(1)
        assert second_stamp != first_stamp
        assert "status: done" in kr1_entry

    def test_idempotent_no_duplicate_lines_across_repeated_calls(self, tmp_path):
        """Two successive set_kr_status calls on the same kr_id must leave
        exactly one status_source line and one status_set_at line — no
        duplicate append on the second write.
        """
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)
        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)

        full_text = goal_file.read_text(encoding="utf-8")
        assert full_text.count("status_source: goal.set_kr_status") == 1
        assert full_text.count("status_set_at:") == 1
        kr1_entry = self._kr1_entry(full_text)
        assert kr1_entry.count("status_source: goal.set_kr_status") == 1
        assert kr1_entry.count("status_set_at:") == 1

    def test_status_set_at_shape(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)
        kr1_entry = self._kr1_entry(goal_file.read_text(encoding="utf-8"))
        stamp = re.search(r"status_set_at: (\S+)", kr1_entry).group(1)

        assert self._TIMESTAMP_RE.match(stamp), f"unexpected status_set_at shape: {stamp!r}"

    def test_provenance_only_written_into_matched_entry(self, tmp_path):
        """Writing kr-1's status must not stamp provenance into kr-2's
        sibling entry in the same file.
        """
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        set_kr_status(goal_file, "kr-1", "in-progress", repo_root=repo)
        text = goal_file.read_text(encoding="utf-8")

        kr2_entry = text.split("id: kr-2", 1)[1].split("\ncreated:", 1)[0]
        assert "status_source" not in kr2_entry
        assert "status_set_at" not in kr2_entry
        assert "status: not-started" in kr2_entry


# ---------------------------------------------------------------------------
# (b) Fail-loud paths
# ---------------------------------------------------------------------------


class TestFailLoud:
    def test_unknown_kr_id_raises_and_does_not_write(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        with pytest.raises(ValueError, match="unknown kr_id"):
            set_kr_status(goal_file, "kr-999", "done", repo_root=repo)

        assert goal_file.read_text(encoding="utf-8") == _GOAL_YAML

    def test_no_key_results_block_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "no-krs.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text("schema: goal\nid: x\nstatus: active\n", encoding="utf-8")

        with pytest.raises(ValueError, match="no key_results"):
            set_kr_status(goal_file, "kr-1", "done", repo_root=repo)

    def test_entry_without_status_field_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "statusless.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(
            "schema: goal\nid: x\nkey_results:\n  - id: kr-1\n    text: \"no status here\"\n",
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="no existing status"):
            set_kr_status(goal_file, "kr-1", "done", repo_root=repo)

    def test_missing_kr_id_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        with pytest.raises(ValueError, match="kr_id is required"):
            set_kr_status(goal_file, "", "done", repo_root=repo)

    def test_missing_status_raises(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        with pytest.raises(ValueError, match="status is required"):
            set_kr_status(goal_file, "kr-1", "", repo_root=repo)


# ---------------------------------------------------------------------------
# (c) Missing goal file
# ---------------------------------------------------------------------------


class TestMissingFile:
    def test_missing_goal_file_raises_file_not_found(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "does-not-exist.yaml"

        with pytest.raises(FileNotFoundError):
            set_kr_status(goal_file, "kr-1", "done", repo_root=repo)


# ---------------------------------------------------------------------------
# (d) AUTHORITATIVE cross-process proof: concurrent writers, different KRs,
# same goal file — both updates must persist.
# ---------------------------------------------------------------------------

# The subprocess monkeypatches the module's internal pure-mutate step
# (_apply_kr_status) to sleep BEFORE returning, widening the window during
# which the lock is held so two truly-concurrent writers reliably overlap.
# This proves the op-level call path — not just the underlying locked_rmw
# primitive (already covered by test_locked_write.py) — serialises correctly
# when both writers target the SAME file but DIFFERENT key_results[] entries,
# which is the actual multi-plan-arc scenario this op exists for.
_WRITER_SCRIPT = textwrap.dedent("""\
    import sys, time
    from pathlib import Path

    sys.path.insert(0, sys.argv[5])

    import coordinator_core.ops.goal_kr_status as mod

    goal_file = Path(sys.argv[1])
    repo_root = Path(sys.argv[2])
    kr_id = sys.argv[3]
    new_status = sys.argv[4]
    delay = float(sys.argv[6])

    _orig = mod._apply_kr_status

    def _delayed(old_text, kr_id, new_status):
        time.sleep(delay)
        return _orig(old_text, kr_id, new_status)

    mod._apply_kr_status = _delayed

    mod.set_kr_status(goal_file, kr_id, new_status, repo_root=repo_root, timeout=30)
""")


class TestCrossProcessConcurrentDifferentKRs:
    def test_concurrent_writers_on_different_krs_both_persist(self, tmp_path):
        repo = _make_git_repo(tmp_path / "repo")
        goal_file = repo / "state" / "goals" / "test-goal.yaml"
        goal_file.parent.mkdir(parents=True)
        goal_file.write_text(_GOAL_YAML, encoding="utf-8")

        script = tmp_path / "writer.py"
        script.write_text(_WRITER_SCRIPT, encoding="utf-8")

        p1 = subprocess.Popen(
            [
                sys.executable, str(script),
                str(goal_file), str(repo), "kr-1", "in-progress",
                _PROJECT_ROOT, "0.2",
            ],
            creationflags=_NO_WINDOW,
        )
        p2 = subprocess.Popen(
            [
                sys.executable, str(script),
                str(goal_file), str(repo), "kr-2", "done",
                _PROJECT_ROOT, "0.2",
            ],
            creationflags=_NO_WINDOW,
        )
        r1 = p1.wait(timeout=30)
        r2 = p2.wait(timeout=30)

        assert r1 == 0, "kr-1 writer subprocess failed"
        assert r2 == 0, "kr-2 writer subprocess failed"

        final_text = goal_file.read_text(encoding="utf-8")

        assert "  - id: kr-1" in final_text
        assert "  - id: kr-2" in final_text
        # Both updates must have landed — neither writer's lock-held mutation
        # was lost to the other's concurrent read-modify-write.
        kr1_block_start = final_text.index("id: kr-1")
        kr2_block_start = final_text.index("id: kr-2")
        kr1_block = final_text[kr1_block_start:kr2_block_start]
        kr2_block = final_text[kr2_block_start:]

        assert "status: in-progress" in kr1_block
        assert "status: done" in kr2_block
