"""
coordinator_core.ops.tests.test_queue_close — op-level coverage for "queue.close".

Coverage:
  (a) registration guard — queue.close fires @register_op.
  (b) open entry -> stamped closed + closed_at/closed_by + committed, THEN
      archived via fleet.archive_queue_entry (two commits land: the stamp,
      then the archive move).
  (c) already-closed entry WITH closed_at/closed_by present -> idempotent:
      no stamp commit lands (locked_rmw skips the byte-identical write),
      only the archive-move commit lands.
  (d) already-closed entry MISSING closed_at/closed_by (the 24-of-36 residue
      shape) -> the missing fields are backfilled and committed, an
      EXISTING field is never overwritten.
  (e) the one YAML-quoted status: "closed" shape normalizes to unquoted on
      write.
  (f) "deferred" status is refused, not silently promoted — no stamp, no
      commit, no archive; source untouched.
  (g) entry_path escaping state/improvement-queue/ is rejected -- via a
      differently-named subtree, a symlink resolving outside the queue dir,
      and an absolute path entirely outside the worktree (three distinct
      escape shapes, one shared guard).
  (h) missing entry_path / missing closed_by -> usage error.
  (i) repo_root is None -> setup error, never a worktree-derivation guess.
  (j) source already gone (already archived / concurrent close) -> closed
      False, archive step takes its own idempotent no-op path.
  (k) a stamp-commit failure surfaces loud and the archive step is never
      reached.
  (l) queue_family.load_family_records(..., where="status = closed") DOES
      match the quoted status: "closed" entry — pins the comparator
      behaviour this op's stamp-normalization relies on (see
      docs/decisions/DR-270-queue-closure-writer-side-commit-ownership.md
      "one real latent bug" section: verified NOT a bug, asserted here so it
      cannot silently regress).

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
  Importing coordinator_core.ops.queue_close fires @register_op("queue.close");
  importing coordinator_core.ops.fleet.archive_queue_entry fires
  @register_op("fleet.archive_queue_entry") — queue.close resolves it via
  coordinator_core.ipc.get_op_handler at call time, so it must already be
  registered (or lazily resolvable) before the handler runs.

Harness: asyncio.run() in sync test fns — no pytest-asyncio dependency. Git
mutations run ONLY inside the throwaway ``queue_repo`` fixture (a git repo
under tmp_path) — never against the working repo. Local fixture (not the
fleet/tests/conftest.py fleet_repo — that conftest is scoped to
coordinator_core/ops/fleet/tests/, not this directory).

Spec backlink: docs/plans/2026-08-05-*-improvement-queue-closure-writer.md § C12
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from pathlib import Path

import pytest

# ---- Import guards: fire @register_op side-effects. ----
import coordinator_core.ops  # noqa: F401 -- populates _REGISTRY for the eagerly-wired ops
import coordinator_core.ops.queue_close  # noqa: F401
import coordinator_core.ops.fleet.archive_queue_entry  # noqa: F401

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.queue_close import _handler


def _run(coro):
    """Run async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Local git-repo fixture (coordinator_core/ops/tests/conftest.py carries no
# improvement-queue seed helper, and this chunk may not add one — shared
# fixture is out of scope).
# ---------------------------------------------------------------------------


class QueueRepo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args), cwd=str(self.root), capture_output=True, check=True,
        )

    def _git_unchecked(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git"] + list(args), cwd=str(self.root), capture_output=True)

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def commit_count(self) -> int:
        result = self._git("rev-list", "--count", "HEAD")
        return int(result.stdout.decode().strip())

    def log_subjects(self, n: int = 5) -> list[str]:
        result = self._git("log", f"-{n}", "--format=%s")
        return [l for l in result.stdout.decode().strip().splitlines() if l.strip()]

    def git_status_clean(self) -> bool:
        return self._git_unchecked("status", "--porcelain").stdout.strip() == b""

    def seed_entry(
        self,
        name: str,
        *,
        status: str = "open",
        quoted_status: bool = False,
        closed_at: str | None = None,
        closed_by: str | None = None,
        created: str = "2026-01-01",
        title: str = "Test Queue Entry",
    ) -> Path:
        """Write and commit a state/improvement-queue/<name>.yaml — plain YAML,
        no fences, matching real on-disk entries."""
        path = self.root / "state" / "improvement-queue" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        status_val = f'"{status}"' if quoted_status else status
        lines = [
            f"created: {created}",
            f'title: "{title}"',
            'body: "test body"',
            f"status: {status_val}",
        ]
        if closed_at is not None:
            lines.append(f"closed_at: {closed_at}")
        if closed_by is not None:
            lines.append(f"closed_by: {closed_by}")
        lines += [
            'surface: "test-surface"',
            'proposed_action: "test action"',
            "from_repo: test-repo",
            "change_kind: script-edit",
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add queue entry {name}")
        return path

    def read_entry(self, name: str) -> str:
        return (self.root / "state" / "improvement-queue" / name).read_text(encoding="utf-8")

    def path_exists(self, repo_rel: str) -> bool:
        return (self.root / repo_rel).exists()


@pytest.fixture
def queue_repo(tmp_path) -> QueueRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True)

    _git("init", "-b", "main")
    _git("config", "user.email", "queue-close-test@claude-klabauter.test")
    _git("config", "user.name", "Queue Close Test")
    _git("config", "commit.gpgsign", "false")

    (repo_root / "state" / "improvement-queue").mkdir(parents=True)
    (repo_root / "state" / "improvement-queue" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return QueueRepo(repo_root)


# ---------------------------------------------------------------------------
# (a) Registration guard
# ---------------------------------------------------------------------------


def test_queue_close_registered():
    assert "queue.close" in _REGISTRY, (
        f"queue.close must be registered; registered ops: {sorted(_REGISTRY.keys())}"
    )


# ---------------------------------------------------------------------------
# (b) Open entry -> stamped + committed + archived (two commits)
# ---------------------------------------------------------------------------


def test_open_entry_stamps_commits_and_archives(queue_repo):
    queue_repo.seed_entry("2026-03-10-close-me.yaml", status="open")
    before = queue_repo.commit_count()

    result = _run(_handler(
        {
            "entry_path": "state/improvement-queue/2026-03-10-close-me.yaml",
            "closed_by": "test-em",
            "closed_at": "2026-03-11",
        },
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["closed"] is True
    assert result["archived"] is True
    assert result["dest"] == "archive/improvement-queue/2026-03/2026-03-10-close-me.yaml"
    assert result["committed_sha"] is not None

    # Two new commits: the stamp, then the archive move.
    assert queue_repo.commit_count() == before + 2

    dest = queue_repo.root / result["dest"]
    assert dest.is_file()
    assert not queue_repo.path_exists("state/improvement-queue/2026-03-10-close-me.yaml")
    body = dest.read_text(encoding="utf-8")
    assert "status: closed" in body
    assert "closed_at: 2026-03-11" in body
    assert "closed_by: test-em" in body
    assert queue_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (c) Already-closed WITH fields present -> idempotent stamp, one commit
# ---------------------------------------------------------------------------


def test_already_closed_with_fields_only_archives_no_restamp_commit(queue_repo):
    queue_repo.seed_entry(
        "2026-04-01-already.yaml", status="closed", closed_at="2026-04-01", closed_by="prior-em",
    )
    before = queue_repo.commit_count()

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-01-already.yaml", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["closed"] is True
    assert result["archived"] is True
    # No stamp commit (byte-identical -> locked_rmw skip, tree already clean at
    # this path) -- and NOT a resume, since the entry was never dirty.
    assert result["committed_sha"] is None
    assert result["resumed"] is False
    assert queue_repo.commit_count() == before + 1

    dest = queue_repo.root / result["dest"]
    body = dest.read_text(encoding="utf-8")
    # closed_by preserved from the ORIGINAL stamp, never overwritten by this call's caller.
    assert "closed_by: prior-em" in body


# ---------------------------------------------------------------------------
# (c2) Stranded-write resume: a prior run's stamp flip landed on disk but
# never got committed (crash between write and commit) -- this call must
# find nothing to re-flip (locked_rmw's own mutate returns old_text
# unchanged), detect the dirty tree BEFORE the archive delegation, re-read
# and re-validate the on-disk content under the lock, and commit those
# authenticated bytes -- reported distinctly via resumed=True.
# ---------------------------------------------------------------------------


def test_already_closed_but_dirty_resumes_stranded_commit(queue_repo):
    entry_rel = "state/improvement-queue/2026-04-01-stranded.yaml"
    queue_repo.seed_entry("2026-04-01-stranded.yaml", status="open")
    before = queue_repo.commit_count()

    # Simulate a prior process's stamp flip that landed on disk but crashed
    # before its own commit -- write the fully-closed frontmatter directly,
    # WITHOUT committing it (mirrors _build_close_mutate's own output shape,
    # bypassing the op so the write is left genuinely uncommitted/dirty).
    entry_path = queue_repo.root / entry_rel
    text = entry_path.read_text(encoding="utf-8")
    text = text.replace("status: open", "status: closed", 1)
    text = text.replace(
        "status: closed\n",
        "status: closed\nclosed_at: 2026-04-01\nclosed_by: prior-em\n",
        1,
    )
    entry_path.write_text(text, encoding="utf-8")
    assert not queue_repo.git_status_clean()

    result = _run(_handler(
        {"entry_path": entry_rel, "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["closed"] is True
    assert result["archived"] is True
    assert result["resumed"] is True
    # The resumed commit lands, then the archive move -- two new commits.
    assert result["committed_sha"] is not None
    assert queue_repo.commit_count() == before + 2

    dest = queue_repo.root / result["dest"]
    body = dest.read_text(encoding="utf-8")
    # closed_by preserved from the stranded write, never overwritten.
    assert "closed_by: prior-em" in body


def test_ac10_resume_refuses_to_commit_when_disk_no_longer_terminal(queue_repo, monkeypatch):
    """AC10 red-proof: a resume must FAIL LOUD, never commit, when the
    on-disk content it re-reads under the lock no longer carries the
    expected terminal close state (e.g. a concurrent writer raced the
    dirty-probe and flipped the entry back to 'open' before this call's
    own re-read landed).

    Exercises the real _handler path (not just the helper in isolation):
    the entry is fully-closed+committed-clean at the time _build_close_
    mutate reads it (state["changed"] stays False -- genuine terminal
    shape from THIS op's own view), but the dirty-probe is monkeypatched
    to force the resume branch regardless, and the disk content is
    mutated to a non-terminal status just before that branch's own
    lock-held re-read runs -- reproducing the same "content changed
    between the dirty check and the authenticated re-read" race the real
    guard exists to catch.
    """
    import coordinator_core.ops.queue_close as queue_close_mod

    entry_rel = "state/improvement-queue/2026-04-01-raced.yaml"
    entry_path = queue_repo.root / entry_rel
    queue_repo.seed_entry(
        "2026-04-01-raced.yaml", status="closed", closed_at="2026-04-01", closed_by="prior-em",
    )
    before = queue_repo.commit_count()

    real_revalidate = queue_close_mod._revalidate_closed_for_resume

    def _racing_revalidate(path, repo_root):
        # Simulate a concurrent writer flipping the entry back to "open"
        # between the dirty-probe (forced True below) and this function's
        # own lock-held re-read.
        text = entry_path.read_text(encoding="utf-8")
        entry_path.write_text(text.replace("status: closed", "status: open", 1), encoding="utf-8")
        return real_revalidate(path, repo_root)

    monkeypatch.setattr(queue_close_mod, "_entry_dirty", lambda *a, **k: True)
    monkeypatch.setattr(queue_close_mod, "_revalidate_closed_for_resume", _racing_revalidate)

    result = _run(_handler(
        {"entry_path": entry_rel, "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert result["closed"] is False
    assert result["archived"] is False
    assert result["committed_sha"] is None
    assert result["resumed"] is False
    assert result["error"] is not None
    assert "resume re-validation failed" in result["error"]
    # No commit landed, and the archive delegation was never reached.
    assert queue_repo.commit_count() == before


# ---------------------------------------------------------------------------
# (d) Already-closed MISSING closed_at/closed_by -> backfill, never overwrite
# ---------------------------------------------------------------------------


def test_already_closed_missing_fields_backfills_without_overwrite(queue_repo):
    queue_repo.seed_entry("2026-04-05-legacy.yaml", status="closed", closed_at="2026-04-02")
    # closed_by intentionally absent -- mirrors 24 of 36 real on-disk entries.

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-05-legacy.yaml", "closed_by": "backfill-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["closed"] is True
    assert result["archived"] is True
    assert result["committed_sha"] is not None  # a real backfill write landed

    dest = queue_repo.root / result["dest"]
    body = dest.read_text(encoding="utf-8")
    # closed_at NOT overwritten (kept its original value)...
    assert "closed_at: 2026-04-02" in body
    # ...closed_by backfilled.
    assert "closed_by: backfill-em" in body


# ---------------------------------------------------------------------------
# (e) Quoted status: "closed" normalizes to unquoted on write
# ---------------------------------------------------------------------------


def test_quoted_status_normalizes_to_unquoted(queue_repo):
    queue_repo.seed_entry("2026-04-06-quoted.yaml", status="closed", quoted_status=True)

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-06-quoted.yaml", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["archived"] is True
    dest = queue_repo.root / result["dest"]
    body = dest.read_text(encoding="utf-8")
    assert "status: closed\n" in body
    assert 'status: "closed"' not in body


# ---------------------------------------------------------------------------
# (f) "deferred" is refused, never silently promoted
# ---------------------------------------------------------------------------


def test_deferred_status_is_refused(queue_repo):
    queue_repo.seed_entry("2026-04-07-deferred.yaml", status="deferred")
    before = queue_repo.commit_count()

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-07-deferred.yaml", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 0
    assert result["closed"] is False
    assert result["archived"] is False
    assert result["skipped_reason"] == "deferred"
    assert queue_repo.commit_count() == before  # no commit at all
    assert queue_repo.path_exists("state/improvement-queue/2026-04-07-deferred.yaml")


# ---------------------------------------------------------------------------
# (g)-(i) Usage / setup errors
# ---------------------------------------------------------------------------


def test_entry_path_escaping_queue_dir_is_rejected(queue_repo):
    result = _run(_handler(
        {"entry_path": "state/handoffs/not-a-queue-entry.md", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))
    assert result["exit_code"] == 2
    assert "escapes" in result["error"]


def test_entry_path_symlink_escaping_queue_dir_is_rejected(queue_repo, tmp_path):
    """A symlink under state/improvement-queue/ that resolves outside it must
    be rejected -- contained_path's .resolve() follows the symlink, and the
    resolved target is not under the queue-dir allowed root."""
    outside_target = tmp_path / "outside-queue-entry.yaml"
    outside_target.write_text("status: open\n", encoding="utf-8")

    symlink_path = queue_repo.root / "state" / "improvement-queue" / "escape-link.yaml"
    try:
        symlink_path.symlink_to(outside_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    result = _run(_handler(
        {
            "entry_path": "state/improvement-queue/escape-link.yaml",
            "closed_by": "test-em",
        },
        repo_root=queue_repo.common_dir,
    ))
    assert result["exit_code"] == 2
    assert "escapes" in result["error"]


def test_entry_path_absolute_outside_worktree_is_rejected(queue_repo, tmp_path):
    """An absolute path pointing entirely outside the worktree must be
    rejected by the same containment guard -- not just a differently-named
    subtree inside the worktree (that's test (g))."""
    outside_file = tmp_path / "elsewhere" / "not-in-repo.yaml"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_text("status: open\n", encoding="utf-8")

    result = _run(_handler(
        {"entry_path": str(outside_file), "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))
    assert result["exit_code"] == 2
    assert "escapes" in result["error"]


def test_missing_entry_path_is_a_usage_error(queue_repo):
    result = _run(_handler({"closed_by": "test-em"}, repo_root=queue_repo.common_dir))
    assert result["exit_code"] == 2
    assert "entry_path" in result["error"]


def test_missing_closed_by_is_a_usage_error(queue_repo):
    result = _run(_handler(
        {"entry_path": "state/improvement-queue/whatever.yaml"}, repo_root=queue_repo.common_dir,
    ))
    assert result["exit_code"] == 2
    assert "closed_by" in result["error"]


def test_repo_root_none_is_a_setup_error():
    result = _run(_handler(
        {"entry_path": "state/improvement-queue/whatever.yaml", "closed_by": "test-em"},
        repo_root=None,
    ))
    assert result["exit_code"] == 1
    assert "repo_root is None" in result["error"]


# ---------------------------------------------------------------------------
# (j) Source already gone -> archive step's own idempotent no-op
# ---------------------------------------------------------------------------


def test_already_gone_source_is_a_vacuous_noop(queue_repo):
    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-01-01-never-existed.yaml", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))
    assert result["exit_code"] == 0
    assert result["closed"] is False
    assert result["archived"] is False


# ---------------------------------------------------------------------------
# (k) Stamp-commit failure surfaces loud; archive step never reached
# ---------------------------------------------------------------------------


def test_stamp_commit_failure_surfaces_loud_and_skips_archive(queue_repo, monkeypatch):
    queue_repo.seed_entry("2026-04-08-fails.yaml", status="open")

    import coordinator_core.ops.queue_close as qc
    from coordinator_core.ops.ceremony.git_native import GitResult

    def _fail(*_a, **_kw):
        return GitResult(returncode=1, stdout="", stderr="simulated commit failure")

    monkeypatch.setattr(qc.git_native, "commit_authored_content", _fail)

    result = _run(_handler(
        {"entry_path": "state/improvement-queue/2026-04-08-fails.yaml", "closed_by": "test-em"},
        repo_root=queue_repo.common_dir,
    ))

    assert result["exit_code"] == 1
    assert "committing it failed" in result["error"]
    assert "simulated commit failure" in result["error"]
    assert result["archived"] is False
    # The flip itself already landed on disk (write happens before commit) --
    # this only pins the op's own reporting contract, not a rollback of the write.
    assert "status: closed" in queue_repo.read_entry("2026-04-08-fails.yaml")
    # But the archive step was never reached: the source file is still present.
    assert queue_repo.path_exists("state/improvement-queue/2026-04-08-fails.yaml")


# ---------------------------------------------------------------------------
# (l) Comparator pin: quoted status: "closed" DOES match `where="status = closed"`
# ---------------------------------------------------------------------------


def test_quoted_closed_status_matches_where_clause(queue_repo):
    queue_repo.seed_entry("2026-05-01-quoted-where.yaml", status="closed", quoted_status=True)

    from coordinator_core.ops.queue_family import load_family_records

    records = load_family_records(
        "improvement-queue", queue_repo.common_dir, where="status = closed",
    )
    paths = [r["path"] for r in records]
    assert any(p.endswith("2026-05-01-quoted-where.yaml") for p in paths), (
        "load_family_records(where='status = closed') must match a YAML-quoted "
        "status: \"closed\" scalar -- records_query._clause_matches compares "
        "str(parse_yaml(...)['status']) against the unquoted RHS, and parse_yaml "
        "already strips the quotes, so this is NOT the latent bug the dispatch "
        "brief asked to settle by execution (see this op's queue_close.py "
        "docstring / DR-270 for the write-up)."
    )
