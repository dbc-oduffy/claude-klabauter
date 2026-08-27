"""
coordinator_core.tests.test_handoff_backfill_claim_stamp

Coverage for the "handoff.backfill_claim_stamp" op — reconstructs a missing
claim stamp (claimed_at/claimed_by) on a handoff that was worked but never
formally claimed, from caller-supplied, git-verified evidence.

Spec backlink: pln-a-claim-stamp-backfill-verb-an-a345d2,
chunk C1, AC1-AC7.

Deliberately placed alongside
coordinator_core/tests/test_archival_claimed_or_shipped.py, which covers the
`archival.claimed_or_shipped[_at_path]` predicate this op consumes READ-ONLY
(AC4) — that file is untouched here; this file only asserts the op's own
behaviour, treating the predicate as a black box.

`locked_rmw` resolves its lock directory via `git_common_dir`, which shells
out to `git rev-parse`, and AC2 requires a real `git cat-file -e` evidence
check — there is no git-free way to exercise this op end-to-end, so a real
(not mocked) throwaway repo is used, mirroring
coordinator_core/ops/tests/test_handoff_reconcile_close_terminal_defects.py's
own fixture model (one throwaway repo per test, explicit construction).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_backfill_claim_stamp  # noqa: F401 — fires @register_op
from coordinator_core.frontmatter.primitives import read_fm_field, read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_backfill_claim_stamp import _handler as _backfill_handler

# Repo-relative path to the CLI forwarder under test (coordinator/bin/) —
# resolved from this test file's own location rather than the process cwd,
# so it works regardless of pytest's invocation directory.
_CLI_PATH = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "handoff-backfill-claim-stamp.py"
)

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.backfill_claim_stamp"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_backfill_claim_stamp @register_op did not fire"
)


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )
        return Path(result.stdout.decode().strip()).resolve()

    def head_sha(self) -> str:
        result = self._git("rev-parse", "HEAD")
        return result.stdout.decode().strip()

    def seed_handoff(
        self,
        name: str,
        *,
        status: str = "open",
        deployment_state: str = "ready_to_fire",
        claimed_at: "str | None" = None,
        claimed_by: "str | None" = None,
        status_reason: "str | None" = None,
    ) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            f'title: "Test Handoff {name}"',
            "created: 2026-01-01",
            "branch: work/test/2026-01-01",
            f"status: {status}",
            "predecessor: null",
            f"deployment_state: {deployment_state}",
        ]
        if claimed_at is not None:
            lines.append(f"claimed_at: {claimed_at}")
        if claimed_by is not None:
            lines.append(f"claimed_by: {claimed_by}")
        if status_reason is not None:
            lines.append(f'status_reason: "{status_reason}"')
        fm_block = "\n".join(lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def abs_path(self, name: str) -> str:
        return str(self.root / "state" / "handoffs" / name)

    def fm(self, name: str) -> str:
        text = (self.root / "state" / "handoffs" / name).read_text(encoding="utf-8")
        split = split_frontmatter(text)
        assert split is not None
        return split.fm_text


@pytest.fixture
def repo(tmp_path) -> _Repo:
    root = tmp_path / "repo"
    root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "backfill-claim-stamp-test@claude-klabauter.test")
    _git("config", "user.name", "backfill-claim-stamp Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(result):
    """`_backfill_handler` is a plain `def` (sync branch, dispatch-timeout
    enforceable — see its own docstring); this passthrough keeps every call
    site below unchanged rather than rewriting each `_run(_backfill_handler(...))`
    into a bare call."""
    return result


# ---------------------------------------------------------------------------
# AC1 — writes claimed_at/claimed_by from evidence; refuses without evidence.
# ---------------------------------------------------------------------------


def test_backfill_writes_claimed_at_and_claimed_by(repo):
    name = "2026-08-11-unstamped.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-abc",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    fm = repo.fm(name)
    assert read_fm_field_unquoted(fm, "claimed_at")
    assert read_fm_field(fm, "claimed_by") == "test-session-abc"


def test_backfill_refuses_with_no_evidence_commits(repo):
    name = "2026-08-11-no-evidence.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [],
                "attested_by": "test-session-abc",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 2, result
    fm = repo.fm(name)
    assert read_fm_field_unquoted(fm, "claimed_at") is None, "no write must occur"


# ---------------------------------------------------------------------------
# AC2 — every evidence commit must resolve in this repo; refuses on any that
# does not, with no write.
# ---------------------------------------------------------------------------


def test_backfill_refuses_on_unresolvable_evidence_commit(repo):
    name = "2026-08-11-bad-evidence.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": ["deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"],
                "attested_by": "test-session-abc",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 1, result
    fm = repo.fm(name)
    assert read_fm_field_unquoted(fm, "claimed_at") is None, "no write must occur"


# ---------------------------------------------------------------------------
# AC3 — status_reason carries the attesting session and evidence SHAs; no
# new frontmatter key is introduced.
# ---------------------------------------------------------------------------


def test_backfill_status_reason_names_session_and_evidence(repo):
    name = "2026-08-11-status-reason.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-xyz",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    fm = repo.fm(name)
    reason = read_fm_field_unquoted(fm, "status_reason")
    assert reason is not None
    assert "test-session-xyz" in reason
    assert sha in reason
    # Anti-scope: no new frontmatter key beyond claimed_at/claimed_by/status_reason.
    assert "consumed_at" not in fm  # sanity: nothing exotic got inserted


def test_backfill_appends_to_existing_status_reason_rather_than_clobbering(repo):
    name = "2026-08-11-existing-status-reason.md"
    repo.seed_handoff(
        name, deployment_state="ready_to_fire", status_reason="prior human note"
    )
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-xyz",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    fm = repo.fm(name)
    reason = read_fm_field_unquoted(fm, "status_reason")
    assert "prior human note" in reason
    assert "test-session-xyz" in reason


# ---------------------------------------------------------------------------
# AC4 — already claimed_or_shipped is an idempotent no-op at exit 0, never a
# second stamp over an existing one.
# ---------------------------------------------------------------------------


def test_backfill_is_noop_on_already_claimed(repo):
    name = "2026-08-11-already-claimed.md"
    repo.seed_handoff(
        name,
        deployment_state="in_flight",
        status="claimed",
        claimed_at="2026-01-02T00:00:00Z",
        claimed_by="original-session",
    )
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "different-session",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is False
    assert result["already_claimed_or_shipped"] is True
    fm = repo.fm(name)
    # Original claim data untouched.
    assert read_fm_field(fm, "claimed_by") == "original-session"
    assert read_fm_field_unquoted(fm, "claimed_at") == "2026-01-02T00:00:00Z"


def test_backfill_recheck_inside_lock_catches_race_pre_lock_check_missed(repo, monkeypatch):
    """P2 TOCTOU regression guard: simulates a legitimate claim landing in the
    window between the pre-lock `claimed_or_shipped_at_path` fast-path check
    and lock acquisition, by forcing that pre-lock check to (falsely) report
    "not claimed" while the on-disk record already carries a real claim.
    Without the in-lock recheck in `mutate()`, this reaches the write path
    and appends a "backfilled" note over a now-legitimately-claimed record,
    reporting `applied: True` — a false record on the file itself. Fails
    against the pre-fix (unlocked-check-only) code; passes against the fix
    because `mutate()` re-verifies the predicate against the freshly-read,
    lock-held frontmatter text before building any write."""
    name = "2026-08-11-race-already-claimed.md"
    repo.seed_handoff(
        name,
        deployment_state="in_flight",
        status="claimed",
        claimed_at="2026-01-02T00:00:00Z",
        claimed_by="racing-session",
    )
    sha = repo.head_sha()

    import coordinator_core.ops.handoff_backfill_claim_stamp as mod

    monkeypatch.setattr(mod, "claimed_or_shipped_at_path", lambda path: False)

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "different-session",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is False, (
        "in-lock recheck must catch the race and report no write, even "
        "though the pre-lock fast-path check was fooled"
    )
    assert result["already_claimed_or_shipped"] is True

    fm = repo.fm(name)
    # Original claim data untouched, and no "backfilled" note appended —
    # the pre-fix code would append one here.
    assert read_fm_field(fm, "claimed_by") == "racing-session"
    assert read_fm_field_unquoted(fm, "claimed_at") == "2026-01-02T00:00:00Z"
    reason = read_fm_field_unquoted(fm, "status_reason")
    assert reason is None or "backfilled" not in reason


def test_backfill_is_noop_on_already_shipped(repo):
    name = "2026-08-11-already-shipped.md"
    repo.seed_handoff(name, deployment_state="shipped")
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-abc",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["already_claimed_or_shipped"] is True
    fm = repo.fm(name)
    assert read_fm_field_unquoted(fm, "claimed_at") is None


# ---------------------------------------------------------------------------
# AC6 — shipped_in is never written by this op.
# ---------------------------------------------------------------------------


def test_backfill_never_writes_shipped_in(repo):
    name = "2026-08-11-no-shipped-in.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    sha = repo.head_sha()

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-abc",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    fm = repo.fm(name)
    assert read_fm_field_unquoted(fm, "shipped_in") is None


# ---------------------------------------------------------------------------
# Defect regression — the op must repair a legacy record that predates a
# schema addition, not refuse it via full-document validation.
# ---------------------------------------------------------------------------


def test_backfill_repairs_legacy_record_missing_a_later_required_field(repo):
    """The op's entire purpose is repairing a handoff that predates a schema
    addition. `predecessor` is `required` at the schema's top level today
    (see `coordinator_core/frontmatter/schemas/handoff.schema.json`); a
    genuinely legacy record written before that requirement lands omits it
    entirely — confirmed directly against
    `coordinator_core.frontmatter.schema_validate.validate_frontmatter` in
    this same test session, which reports exactly one error,
    `{'field': 'predecessor', 'error': 'required field missing'}`, for the
    fixture below.

    Pre-fix, `handoff_transition._validate_fm` ran that SAME full-document
    validation as this op's post-mutation gate, so it refused to write the
    claim stamp onto exactly the record it exists to repair — the defect
    this test guards against. Post-fix, `_validate_backfilled_fields` checks
    only the three fields this op writes (`claimed_at`/`claimed_by`/
    `status_reason`), so the repair lands despite the record's legacy shape.
    """
    name = "2026-08-11-legacy-no-predecessor.md"
    path = repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately omits `predecessor:` — the legacy shape this op exists to
    # repair (contrast with `_Repo.seed_handoff`, which always writes it).
    content = (
        "---\n"
        'title: "Legacy handoff, no predecessor field"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        "deployment_state: ready_to_fire\n"
        "---\n\n# Handoff\n\nBody.\n"
    )
    path.write_text(content, encoding="utf-8")
    repo._git("add", str(path))
    repo._git("commit", "-m", f"add legacy handoff {name}")
    sha = repo.head_sha()

    # Sanity: confirm this fixture genuinely fails full-document validation
    # (the pre-fix gate) before asserting the op repairs it anyway.
    import yaml as _yaml

    from coordinator_core.frontmatter.schema_validate import validate_frontmatter
    from coordinator_core.ops.handoff_transition import _SCHEMA_PATH

    fm_dict = _yaml.safe_load(repo.fm(name)) or {}
    fm_dict.update(
        {"claimed_at": "2026-08-12T00:00:00Z", "claimed_by": "sess", "status_reason": "x"}
    )
    pre_fix_errors = validate_frontmatter(fm_dict, _SCHEMA_PATH)
    assert pre_fix_errors, (
        "fixture setup bug: expected this legacy record to fail full-"
        "document validation (missing 'predecessor') — it did not, so this "
        "test would not actually exercise the defect"
    )

    result = _run(
        _backfill_handler(
            {
                "handoff_path": f"state/handoffs/{name}",
                "evidence_commit": [sha],
                "attested_by": "test-session-legacy",
            },
            repo.common_dir,
        )
    )

    assert result["exit_code"] == 0, result
    assert result["applied"] is True, result
    fm = repo.fm(name)
    assert read_fm_field(fm, "claimed_by") == "test-session-legacy"
    assert read_fm_field_unquoted(fm, "claimed_at")


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


def test_backfill_usage_error_missing_handoff_path(repo):
    result = _run(
        _backfill_handler(
            {"handoff_path": "", "evidence_commit": ["abc123"]},
            repo.common_dir,
        )
    )
    assert result["exit_code"] == 2, result


# ---------------------------------------------------------------------------
# CLI seam — coordinator/bin/handoff-backfill-claim-stamp.py, exercised as a
# real subprocess so a broken forwarder (e.g. repo_root plumbing dropped
# between the CLI and the op) cannot pass while every op-level test above
# stays green. `_handler` tests above never cross this seam; these do.
# ---------------------------------------------------------------------------


def _run_cli(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI_PATH), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def test_cli_missing_handoff_gives_op_level_refusal_not_transport_error(repo):
    """Control discipline: the exact bug this test guards against was the CLI
    forwarding no repo_root, which surfaces as the op's OWN "repo_root is
    required (handler called without socket-authoritative common_dir)"
    transport-shaped refusal rather than a real op-level "handoff not found"
    refusal. Confirmed failing (asserting the WRONG/broken message) against
    the pre-fix forwarder, and passing (asserting the CORRECT message)
    post-fix — see this chunk's run-report sidecar for both transcripts."""
    proc = _run_cli(
        ["state/handoffs/does-not-exist.md", "--evidence-commit", "HEAD"],
        cwd=repo.root,
    )

    assert proc.returncode == 1, proc.stderr
    assert "repo_root is required" not in proc.stderr, (
        "CLI is not forwarding repo_root to the op — regressed to the "
        f"pre-fix transport-shaped refusal:\n{proc.stderr}"
    )
    assert "handoff not found" in proc.stderr, (
        f"expected a real op-level 'handoff not found' refusal, got:\n{proc.stderr}"
    )


def test_cli_end_to_end_success_stamps_the_record(repo):
    """The operator-facing success path, through the CLI seam: a real
    throwaway handoff, a real evidence commit, and the CLI's own exit code
    plus its post-write disk re-read — not an inference from the op's
    envelope alone."""
    name = "2026-08-11-cli-e2e.md"
    repo.seed_handoff(name, deployment_state="ready_to_fire")
    sha = repo.head_sha()

    proc = _run_cli(
        [
            f"state/handoffs/{name}",
            "--evidence-commit",
            sha,
            "--attested-by",
            "cli-e2e-test-session",
        ],
        cwd=repo.root,
    )

    assert proc.returncode == 0, proc.stderr
    fm = repo.fm(name)
    assert read_fm_field(fm, "claimed_by") == "cli-e2e-test-session"
    assert read_fm_field_unquoted(fm, "claimed_at")
    reason = read_fm_field_unquoted(fm, "status_reason")
    assert sha in reason
