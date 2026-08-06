"""
coordinator_core.ops.tests.test_handoff_archive_transition

Tests for the handoff.archive_transition op — the 4-mode (chain / stamp_shipped /
stamp_only / supersede) native-Python port of example-doctrine-repo's coordinator-handoff-archive.sh,
Position A architecture (PM-ratified 2026-07-15): no branch-tip fallback, no
Session-Id trailer-correction walk — shipped_in is scope-path-resolved or left
unset.

Import guard: coordinator_core.ops.handoff_archive_transition MUST be imported at
module load time to fire @register_op("handoff.archive_transition") and populate
_REGISTRY — otherwise a registry-completeness assertion passes vacuously.

Coverage:
  (a) op registered
  (b) chain mode — guard safe -> git mv + commit into archive/handoffs/YYYY-MM/, no stamp
  (c) chain mode — guard has-live-children (exit_code 0) -> retained, NO move
  (d) chain mode — guard indeterminate (exit_code 2) -> retained, NO move
  (e) stamp_shipped mode — stamps shipped_in (scope-resolved) + archives
  (f) stamp_only mode — stamps + deployment_state:shipped, NO git mv, file retained
  (g) supersede mode — status:consumed + deployment_state:abandoned BEFORE the git mv
  (h) Position A — scope resolves to a real commit -> shipped_in set to that SHA
      (stamp_shipped and stamp_only)
  (i) Position A — scope resolves to nothing -> shipped_in stays UNSET, a warning
      is surfaced, exit_code still 0 (stamp_shipped and stamp_only)
  (j) mutual-exclusion / invalid mode -> usage error (exit_code 2)

Spec backlink: cross-repo example-doctrine-repo 7-bug route item 4 (this op); DR-059 (engine-tier
bash bugs route to claude-klabauter); claude-klabauter Position A (PM-ratified 2026-07-15); closed
backlog entry state/bug-backlog/2026-07-14-handoff-archive-stamp-only-stamps-
sibling-sha.yaml (status: closed-not-reproducible).
Port of: coordinator-handoff-archive.sh (example-doctrine-repo c47b0268, 2026-07-19)
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("handoff.archive_transition") as a side-effect.
# MUST precede any test function so the registry is populated before assertions.
# ---------------------------------------------------------------------------
import coordinator_core.ops.handoff_archive_transition  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_archive_transition import _handler, _sha_canonically_matches
from coordinator_core.frontmatter.primitives import (
    read_fm_field,
    read_fm_field_unquoted,
    split_frontmatter,
)

# test_archive_stamp_first_import_no_cycle below spawns a fresh interpreter
# that imports coordinator_core.archive_stamp. That child inherits cwd but
# NOT pytest's rootdir sys.path insertion, so it can only resolve the package
# when cwd is (or is under) the repo root -- from any other cwd it dies with
# ModuleNotFoundError before it can write anything to stdout. Pinning cwd to
# the repo root derived from this file's own path makes the subprocess
# resolvable regardless of the invoking shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_OP_NAME = "handoff.archive_transition"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.handoff_archive_transition @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio dependency needed."""
    return asyncio.run(coro)


# Ownership guard (2026-07-22): stamp_shipped_in now refuses to stamp a
# DERIVED sha unless its Session-Id trailer matches the CALLING session's own
# id. `handoff_repo`'s underlying HandoffRepo.seed_handoff (conftest.py, shared
# across 8 test files in this package) does not stamp trailers on its commits —
# rather than widen this fix into that shared fixture, the 4 tests here that
# actually exercise the derived-sha stamp path amend HEAD locally via
# `_add_session_trailer_to_head` after seeding, and `_default_caller_session_id`
# (autouse, this file only) resolves the caller as that same id.
_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    """See _DEFAULT_TEST_SESSION_ID above — mirrors coordinator_core/
    test_archive_stamp.py's identically-named fixture."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _add_session_trailer_to_head(repo, sid: str = _DEFAULT_TEST_SESSION_ID) -> None:
    """Amends HEAD to append a `Session-Id: <sid>` trailer, so the ownership
    guard resolves this commit as the CALLING session's own. Call right after
    seeding the handoff whose scope points at itself (or at another file
    committed in the same commit) — amending changes HEAD's sha, so any
    `_head_sha`/`_path_sha` capture must happen AFTER this call, not before."""
    msg = _git(repo, "log", "-1", "--format=%B").stdout.decode()
    _git(repo, "commit", "--amend", "-m", f"{msg.strip()}\n\nSession-Id: {sid}")


def _git(repo, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo.root),
        capture_output=True,
        check=True,
    )


def _head_sha(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def _path_sha(repo, relpath: str) -> str:
    """The SHA of the most recent commit touching `relpath` — mirrors
    stamp_shipped_in's own `git log -n1 -- <scope>` scope-path resolution."""
    return _git(repo, "log", "--format=%H", "-n1", "--", relpath).stdout.decode().strip()


def _deployment_state(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "deployment_state")


def _status(repo, name: str) -> str:
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field(split.fm_text, "status")


def _shipped_in_kind(repo, name: str) -> str:
    """Read shipped_in_kind (DR-096 discriminant) as a comparable string."""
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, "shipped_in_kind")


def _shipped_in(repo, name: str) -> str:
    """Read shipped_in as a comparable SHA, not as raw on-disk YAML text.

    stamp_shipped_in writes with numeric_quoting=True, so an all-digit or
    scientific-notation-looking sha8 lands single-quoted; a raw read returns the
    quotes and every equality assertion against a bare sha flakes (~13% of
    commits). read_fm_field_unquoted is the comparison-safe reader.
    """
    text = repo.read_text(name)
    split = split_frontmatter(text)
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, "shipped_in")


def _archive_glob(repo, name: str):
    return [p for p in (repo.root / "archive" / "handoffs").rglob("*.md") if p.name == name]


def _committed_blob(repo, path) -> str:
    """The git-committed blob content for `path` (relative to repo root or an
    absolute Path under it) — as opposed to the on-disk bytes, which a rename
    alone never changes. Used to catch Finding 1 (restage_src): a stale
    private-index entry means the archive commit's blob diverges from the
    on-disk content even though `git status` looks clean post-mv."""
    rel = str(Path(path).relative_to(repo.root)) if isinstance(path, Path) else str(path)
    return _git(repo, "show", f"HEAD:{rel}").stdout.decode()


def _git_status_clean(repo) -> bool:
    out = _git(repo, "status", "--porcelain").stdout.decode()
    return out.strip() == ""


# ---------------------------------------------------------------------------
# (a) registration
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY, f"present ops: {sorted(_REGISTRY)}"


# ---------------------------------------------------------------------------
# Review: code-reviewer F4 — regression test pinning the import-cycle fix.
#
# archive_stamp.py imports coordinator_core.ops.session_context at its own
# module top-level, which triggers coordinator_core.ops/__init__.py's eager
# import sweep, which in turn reaches handoff_archive_transition — so a
# top-level `from coordinator_core.archive_stamp import stamp_shipped_in`
# in THIS module would deadlock on archive_stamp's own
# partially-initialized module (ImportError: cannot import name
# 'stamp_shipped_in' from partially initialized module). The in-process
# test suite masks this because it always imports coordinator_core.ops
# (this test module itself) BEFORE coordinator_core.archive_stamp, which is
# the entry order that does NOT trigger the deadlock. This test exercises
# the actual failure mode: a fresh subprocess importing
# coordinator_core.archive_stamp as the FIRST coordinator_core.* import,
# mirroring a real entry point like bin/archive-stamp-cli.
# ---------------------------------------------------------------------------


def test_archive_stamp_first_import_no_cycle():
    proc = subprocess.run(
        [sys.executable, "-c", "import coordinator_core.archive_stamp"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    assert proc.returncode == 0, (
        "importing coordinator_core.archive_stamp as the first "
        "coordinator_core.* import must not deadlock on a partially "
        f"initialized module — stdout: {proc.stdout!r} stderr: {proc.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (b) chain mode — guard safe -> archived, no stamp
# ---------------------------------------------------------------------------


def test_chain_mode_archives_when_safe(handoff_repo):
    # Terminal deployment_state — chain mode stamps nothing itself (see the
    # terminal-state precondition test group below), so the candidate must
    # already be terminal on disk for chain to move it at all.
    name = "2026-07-19-chain-safe.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["mode"] == "chain"
    assert result["moved"] is True
    assert result["retained"] is False
    assert result["stamped"] is False
    assert not (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 1


# ---------------------------------------------------------------------------
# (c) chain mode — guard has-live-children -> retained, no move
# ---------------------------------------------------------------------------


def test_chain_mode_retains_with_live_children(handoff_repo):
    name = "2026-07-19-chain-has-children.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    # A live, non-terminal child naming `name` as its predecessor.
    handoff_repo.seed_handoff(
        "2026-07-19-child.md", "claimed", deployment_state="in_flight", predecessor=name
    )

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["retained"] is True
    assert result["moved"] is False
    assert result["retain_kind"] == "live-parent"
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


# ---------------------------------------------------------------------------
# (d) chain mode — guard indeterminate -> retained, no move
# ---------------------------------------------------------------------------


def test_chain_mode_retains_on_indeterminate_guard(handoff_repo, monkeypatch):
    import coordinator_core.ops.handoff_archive_transition as hat

    name = "2026-07-19-chain-indeterminate.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    async def _boom_guard(params, repo_root):
        return {"exit_code": 2, "error": "simulated indeterminate"}

    monkeypatch.setattr(hat, "_handoff_has_live_children", _boom_guard)

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["retained"] is True
    assert result["moved"] is False
    assert "indeterminate" in result["retain_reason"].lower()
    assert result["retain_kind"] == "indeterminate"
    assert any(
        "not a deliberate retain" in w.lower() for w in result["warnings"]
    ), result["warnings"]
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


# ---------------------------------------------------------------------------
# (e) stamp_shipped mode
# ---------------------------------------------------------------------------


def test_stamp_shipped_mode_stamps_and_archives(handoff_repo):
    name = "2026-07-19-stamp-shipped.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        extra=f"scope:\n  - state/handoffs/{name}",
    )
    _add_session_trailer_to_head(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_shipped"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert result["moved"] is True
    archived = _archive_glob(handoff_repo, name)
    assert len(archived) == 1
    # deployment_state must flip to shipped alongside shipped_in — the archived
    # record is the terminal shipped contract (status:consumed +
    # deployment_state:shipped + shipped_in:<sha>). Regression guard for the
    # 2026-07-21 cross-repo bug where stamp_shipped git-mv'd + stamped shipped_in
    # but left deployment_state:in_flight on the archived handoff.
    split = split_frontmatter(archived[0].read_text(encoding="utf-8"))
    assert read_fm_field(split.fm_text, "deployment_state") == "shipped"
    # Finding 1 (restage_src) regression: the archived commit's blob must
    # carry the SAME content the stamp just wrote to disk, not the stale
    # pre-stamp blob a non-restaged git-mv would silently commit.
    assert _git_status_clean(handoff_repo)
    assert _committed_blob(handoff_repo, archived[0]) == archived[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# (f) stamp_only mode
# ---------------------------------------------------------------------------


def test_stamp_only_mode_stamps_without_moving(handoff_repo):
    name = "2026-07-19-stamp-only.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        extra=f"scope:\n  - state/handoffs/{name}",
    )
    _add_session_trailer_to_head(handoff_repo)

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert result["moved"] is False
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0
    assert _deployment_state(handoff_repo, name) == "shipped"


# ---------------------------------------------------------------------------
# (g) supersede mode
# ---------------------------------------------------------------------------


def test_supersede_mode_mutates_before_move(handoff_repo):
    name = "2026-07-19-supersede.md"
    # DR-242 (Finding 1, C5 review fix): mode="supersede" now gates on
    # claimed_or_shipped at the op choke point — seed a legitimately claimed
    # predecessor rather than the bare never-claimed shape DR-242 exists to
    # refuse.
    handoff_repo.seed_handoff(
        name,
        "claimed",
        deployment_state="in_flight",
        claimed_at="2026-07-19T00:00:00Z",
        claimed_by="session-test",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True
    assert result["moved"] is True
    archived = _archive_glob(handoff_repo, name)
    assert len(archived) == 1
    text = archived[0].read_text(encoding="utf-8")
    split = split_frontmatter(text)
    # DR-084: the consumed+abandoned expression retired; supersession WITH a lineage
    # edge is now claimed+continued, and the successor is recorded rather than implied.
    assert read_fm_field(split.fm_text, "status") == "claimed"
    assert read_fm_field(split.fm_text, "deployment_state") == "continued"
    assert read_fm_field(split.fm_text, "continued_into") == "hnd-successor-0001"
    # Finding 1 (restage_src) regression — see identical assertion in
    # test_stamp_shipped_mode_stamps_and_archives above.
    assert _git_status_clean(handoff_repo)
    assert _committed_blob(handoff_repo, archived[0]) == text


# ---------------------------------------------------------------------------
# (g2) supersede mode — status flip must fire independent of a live claim
# holder / live-children guard retention (2026-07-27 fix). Regression
# coverage for the cross-repo example-doctrine-repo incident: "handoff-archive-transition
# supersede silently no-ops" — the guard's live-child membership check treats
# the SUCCESSOR itself (the handoff naming the candidate as `predecessor:`)
# as a live child the instant it exists on disk non-terminal, which is the
# normal shape of every real `/handoff` call. That must retain the ARCHIVAL
# move, never suppress the status flip.
# ---------------------------------------------------------------------------


def test_supersede_status_flip_fires_with_live_claim_holder_and_successor(handoff_repo):
    """(a) predecessor with a live claim holder AND a successor -> the status
    flip (status:claimed + deployment_state:continued + continued_into)
    applies even though the successor's own existence makes the
    live-children guard retain the archival move. This is the exact shape of
    the reported bug: `claimed_by` a live session, successor already on
    disk naming the predecessor -> the whole op used to no-op silently."""
    name = "2026-07-27-supersede-live-claim-holder.md"
    handoff_repo.seed_handoff(
        name,
        "claimed",
        deployment_state="in_flight",
        claimed_by="11111111-1111-1111-1111-111111111111",  # a LIVE claim holder
    )
    successor_name = "2026-07-27-supersede-live-claim-holder-successor.md"
    # The successor names `name` as its predecessor and is itself non-terminal
    # -> handoff.has_live_children sees `name` as referenced (a live child),
    # which is precisely what must NOT suppress the status flip below.
    handoff_repo.seed_handoff(
        successor_name, "claimed", deployment_state="in_flight", predecessor=name,
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": f"state/handoffs/{successor_name}",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    # The status flip must have applied regardless of the guard's verdict.
    assert result["superseded"] is True, result
    assert _status(handoff_repo, name) == "claimed"
    assert _deployment_state(handoff_repo, name) == "continued"
    text = handoff_repo.read_text(name)
    split = split_frontmatter(text)
    assert (
        read_fm_field_unquoted(split.fm_text, "continued_into")
        == f"state/handoffs/{successor_name}"
    )


def test_supersede_archival_guard_still_defers_with_live_holder(handoff_repo):
    """(b) the archival guard still defers while a live holder/live child
    remains — the status flip firing (test above) must NOT weaken this. Same
    live-child setup as (a): the successor is on disk, non-terminal, naming
    `name` as predecessor -> guard retains -> moved stays False."""
    name = "2026-07-27-supersede-archival-still-deferred.md"
    handoff_repo.seed_handoff(
        name,
        "claimed",
        deployment_state="in_flight",
        claimed_by="11111111-1111-1111-1111-111111111111",
    )
    successor_name = "2026-07-27-supersede-archival-still-deferred-successor.md"
    handoff_repo.seed_handoff(
        successor_name, "claimed", deployment_state="in_flight", predecessor=name,
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": f"state/handoffs/{successor_name}",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["retained"] is True, result
    assert result["moved"] is False, result
    assert result["retain_kind"] == "live-parent", result
    # The candidate must still be on disk in state/handoffs/, not archived.
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_supersede_genuine_decline_is_not_silently_swallowed(handoff_repo):
    """(c) a genuine decline (missing --continued-into, i.e. the usage-error
    refusal path) emits a reason and does not look like success — the
    op-level shape of "never silently do nothing"."""
    name = "2026-07-27-supersede-genuine-decline.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="in_flight")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "supersede"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert result.get("error"), result
    assert "continued_into" in result["error"], result
    assert result["superseded"] is False, result
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_supersede_mode_refuses_without_continued_into(handoff_repo):
    """The anti-loophole tooth: no successor named, no `continued` stamp.

    DR-084 lets an automated writer stamp `continued` only on positive succession
    proof. Without `continued_into` the op must refuse rather than fall back to a
    liveness guess — that fallback is the banned abandonment shape wearing a new
    label — and it must leave the handoff untouched in place.
    """
    name = "2026-07-19-supersede-no-successor.md"
    handoff_repo.seed_handoff(name, "open", deployment_state="in_flight")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "supersede"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


# ---------------------------------------------------------------------------
# (g3) supersede mode — archived-predecessor stamp-in-place (2026-07-28,
# d6-archived-predecessor fix). The normal `/handoff` shape archives the
# predecessor via the session boot sweep BEFORE d6 ever runs, so
# `handoff_path` routinely names an already-archived path by the time
# mode="supersede" is composed — this used to be a hard usage-error refusal
# ("handoff_path escapes state/handoffs/"), making d6 a structural no-op on
# the common case.
# ---------------------------------------------------------------------------


def _archive_in_place(repo, name: str, archive_relpath: str) -> Path:
    """Move a just-seeded state/handoffs/<name> file to `archive_relpath` via
    git mv + commit — mirrors what the session boot sweep does to a
    predecessor BEFORE this op's mode="supersede" is ever composed against
    it, so these tests exercise the actual call shape rather than a
    hand-placed file that was never really "archived"."""
    src = repo.root / "state" / "handoffs" / name
    dst = repo.root / archive_relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", str(src), str(dst))
    _git(repo, "commit", "-m", f"archive {name}")
    return dst


def test_supersede_archived_predecessor_stamps_in_place_no_move(handoff_repo):
    name = "2026-07-28-archived-predecessor.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )
    archive_relpath = f"archive/handoffs/2026-07/{name}"
    _archive_in_place(handoff_repo, name, archive_relpath)
    successor_name = "2026-07-28-archived-predecessor-successor.md"

    result = _run(_handler(
        {
            "handoff_path": archive_relpath,
            "mode": "supersede",
            "continued_into": f"state/handoffs/{successor_name}",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["retained"] is False, result
    assert result["moved"] is False, result

    # Stamped IN PLACE — still at the archive path it was already at, not
    # moved anywhere (no git-mv was ever attempted for an already-archived
    # target).
    archived_file = handoff_repo.root / archive_relpath
    assert archived_file.exists()
    text = archived_file.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "status") == "claimed"
    assert read_fm_field(split.fm_text, "deployment_state") == "continued"
    assert (
        read_fm_field_unquoted(split.fm_text, "continued_into")
        == f"state/handoffs/{successor_name}"
    )


def test_supersede_live_predecessor_regression_still_archives(handoff_repo):
    """Regression guard for the archived-target widening above: a LIVE
    predecessor (state/handoffs/, guard clears, no live children) must still
    behave exactly as before — status flip AND git-mv into archive/handoffs/,
    moved:True."""
    name = "2026-07-28-live-predecessor-regression.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")
    successor_name = "2026-07-28-live-predecessor-regression-successor.md"

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": f"state/handoffs/{successor_name}",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["retained"] is False, result
    assert result["moved"] is True, result
    assert not (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 1


def test_supersede_path_outside_live_and_archive_still_refused(handoff_repo):
    """A path outside BOTH state/handoffs/ and every ARCHIVE_ROOT_SUBDIRS
    entry is still a hard usage-error refusal — the widening admits three
    named archive roots, not an arbitrary escape."""
    outside = handoff_repo.root / "docs" / "not-a-handoff.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("---\nstatus: claimed\n---\n", encoding="utf-8")

    result = _run(_handler(
        {
            "handoff_path": "docs/not-a-handoff.md",
            "mode": "supersede",
            "continued_into": "state/handoffs/successor.md",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert "escapes" in result["error"], result


def test_supersede_archived_predecessor_idempotent_same_continued_into(handoff_repo):
    """A second supersede call against an already-superseded, already-archived
    predecessor with the SAME continued_into is a clean no-op, not an error."""
    name = "2026-07-28-archived-idempotent.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )
    archive_relpath = f"archive/handoffs/2026-07/{name}"
    _archive_in_place(handoff_repo, name, archive_relpath)
    params = {
        "handoff_path": archive_relpath,
        "mode": "supersede",
        "continued_into": "state/handoffs/successor.md",
    }

    first = _run(_handler(dict(params), handoff_repo.common_dir))
    second = _run(_handler(dict(params), handoff_repo.common_dir))

    assert first["exit_code"] == 0, first
    assert second["exit_code"] == 0, second
    assert second["superseded"] is True, second
    archived_file = handoff_repo.root / archive_relpath
    text = archived_file.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert read_fm_field_unquoted(split.fm_text, "continued_into") == "state/handoffs/successor.md"


def test_supersede_archived_predecessor_conflicting_continued_into_fails_loud(handoff_repo):
    """A second supersede call naming a DIFFERENT continued_into than the one
    already recorded is a genuine conflict — refused loudly, naming both
    values, never a silent overwrite of a real succession edge."""
    name = "2026-07-28-archived-conflict.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )
    archive_relpath = f"archive/handoffs/2026-07/{name}"
    _archive_in_place(handoff_repo, name, archive_relpath)

    first = _run(_handler(
        {
            "handoff_path": archive_relpath,
            "mode": "supersede",
            "continued_into": "state/handoffs/successor-a.md",
        },
        handoff_repo.common_dir,
    ))
    assert first["exit_code"] == 0, first

    second = _run(_handler(
        {
            "handoff_path": archive_relpath,
            "mode": "supersede",
            "continued_into": "state/handoffs/successor-b.md",
        },
        handoff_repo.common_dir,
    ))

    assert second["exit_code"] == 1, second
    assert second["superseded"] is False, second
    assert "successor-a.md" in second["error"], second
    assert "successor-b.md" in second["error"], second
    # The original recorded succession edge must survive untouched.
    archived_file = handoff_repo.root / archive_relpath
    text = archived_file.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert (
        read_fm_field_unquoted(split.fm_text, "continued_into")
        == "state/handoffs/successor-a.md"
    )


# ---------------------------------------------------------------------------
# (h) Position A — scope resolves to a real commit -> shipped_in set to it
# ---------------------------------------------------------------------------


def test_position_a_stamp_shipped_uses_scope_resolved_sha(handoff_repo):
    name = "2026-07-19-position-a-stamp-shipped.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        extra=f"scope:\n  - state/handoffs/{name}",
    )
    _add_session_trailer_to_head(handoff_repo)  # trailer the PARENT's own commit
    # Live child keeps the guard retaining the candidate — the archive move is
    # not this test's concern; retention lets us inspect state/handoffs/ directly.
    handoff_repo.seed_handoff(
        "2026-07-19-position-a-stamp-shipped-child.md", "claimed",
        deployment_state="in_flight", predecessor=name,
    )
    expected_sha = _path_sha(handoff_repo, f"state/handoffs/{name}")[:8]

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_shipped"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == expected_sha
    # No --sha supplied -> shipped_in_kind="scope-derived" (legacy path,
    # DR-096) — selection of that kind is now surfaced as an observable
    # warning even on a successful stamp; it is no longer a silent default.
    assert any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]


def test_position_a_stamp_only_uses_scope_resolved_sha(handoff_repo):
    name = "2026-07-19-position-a-stamp-only.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        extra=f"scope:\n  - state/handoffs/{name}",
    )
    _add_session_trailer_to_head(handoff_repo)
    expected_sha = _head_sha(handoff_repo)[:8]

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == expected_sha
    assert any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]


# ---------------------------------------------------------------------------
# (i) Position A — scope resolves to nothing -> shipped_in stays UNSET,
# a warning is surfaced, exit_code still 0. No branch-tip guess ever lands.
# ---------------------------------------------------------------------------


def test_position_a_stamp_shipped_no_scope_leaves_shipped_in_unset(handoff_repo):
    name = "2026-07-19-position-a-no-scope-shipped.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:
    # Live child keeps the guard retaining the candidate — retention lets us
    # inspect state/handoffs/ directly regardless of the git-mv step.
    handoff_repo.seed_handoff(
        "2026-07-19-position-a-no-scope-shipped-child.md", "claimed",
        deployment_state="in_flight", predecessor=name,
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_shipped"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is False
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert any(
        "resolved no commit" in w.lower() for w in result["warnings"]
    ), result["warnings"]


def test_position_a_stamp_only_no_scope_leaves_shipped_in_unset(handoff_repo):
    """Regression test for the ship-handoff incident (2026-07-22): an
    unresolvable shipped_in must never reach the deployment_state:shipped
    flip. Prior behaviour (superseded by this test) let the flip proceed
    with shipped_in unset, guaranteeing the frontmatter validator would
    reject the write on a REAL caller — this op now refuses the flip itself,
    fail-loud, naming --sha as the fix, rather than handing the caller an
    opaque downstream validator rejection.
    """
    name = "2026-07-19-position-a-no-scope-only.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["stamped"] is False
    assert "--sha" in result["error"], result
    assert _shipped_in(handoff_repo, name) in (None, "null")
    # No half-state: deployment_state must NOT have flipped to shipped
    # without a shipped_in to accompany it.
    assert _deployment_state(handoff_repo, name) == "in_flight"


def test_stamp_only_with_explicit_sha_resolves_and_ships(handoff_repo):
    """A caller-supplied --sha resolves the unresolvable-scope case: shipped_in
    is written from the override AND deployment_state flips to shipped in the
    same call, rc=0 — the regression test for the fix itself (Defect 1+2
    composed end-to-end, mirroring `archive-stamp-cli ship-handoff <path>
    --sha <SHA>`)."""
    name = "2026-07-19-stamp-only-explicit-sha.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "sha": "deadbeef",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == "deadbeef"
    assert _deployment_state(handoff_repo, name) == "shipped"
    # kind="ship-commit" (a --sha was supplied) — the scope-derived selection
    # warning must NOT fire here; it is specific to the no-sha legacy path.
    assert not any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]


def test_stamp_shipped_no_scope_no_sha_refuses_flip(handoff_repo):
    """stamp_shipped mode variant of the same refusal — no live children, so
    the guard clears and the post-guard flip is reached."""
    name = "2026-07-19-stamp-shipped-no-scope-refuse.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_shipped"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert "--sha" in result["error"], result
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert len(_archive_glob(handoff_repo, name)) == 0
    # No --sha supplied -> the scope-derived selection warning still fires
    # even on the refuse-flip path (the selection happens before resolution
    # is known to succeed or fail).
    assert any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


def test_stamp_only_idempotent_reship_with_preexisting_shipped_in_still_succeeds(handoff_repo):
    """An already-shipped handoff (shipped_in already present from a prior
    call) calling ship-handoff/stamp_only again must still succeed — the
    refusal gate must key off the CURRENT on-disk shipped_in, not the
    `stamped` bool (which is False on this call since nothing NEW was
    written)."""
    name = "2026-07-19-stamp-only-idempotent-reship.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert _shipped_in(handoff_repo, name) == "deadbeef"
    assert _deployment_state(handoff_repo, name) == "shipped"


# ---------------------------------------------------------------------------
# AC6/AC6b/AC7 (§ S11, chunk C0, docs/plans/2026-07-28-handoff-close-path-
# fail-loud.md): a caller-supplied --sha against an already-present, NON-
# matching shipped_in refuses loudly naming --force (AC6); a same-commit
# re-stamp (full sha canonically matching the stored 8-char prior_value)
# does NOT refuse (AC6b); the no-op warning distinguishes "left unset" from
# "retained prior value <X>" (AC7). All asserted on POST-CALL frontmatter/
# disk state, per AC4 — never on the mock's call args.
# ---------------------------------------------------------------------------


def test_sha_canonically_matches_refuses_short_supplied_abbreviation():
    """Review: code-reviewer (nit F3) — a `supplied` sha SHORTER than the
    stored `prior_value` must never prefix-match; two distinct commits can
    share a short prefix, so an abbreviation-vs-abbreviation comparison is
    refused as non-matching (the AC6-safe default) rather than risking a
    false match against a different commit."""
    # prior_value is the normal 8-char stored form; supplied is a 7-char
    # abbreviation that happens to prefix it — still refused, since supplied
    # is shorter than prior_value.
    assert _sha_canonically_matches("deadbee", "deadbeef") is False
    # Full-length supplied against the 8-char stored prior_value: legitimate
    # same-commit resupply, still matches.
    assert _sha_canonically_matches("deadbeef" + "00" * 16, "deadbeef") is True
    # Equal-length, identical: matches.
    assert _sha_canonically_matches("deadbeef", "deadbeef") is True
    # Equal-length, different: does not match.
    assert _sha_canonically_matches("deadbeef", "cafef00d") is False


def test_ac6_discarded_sha_refuses_loudly_and_names_force(handoff_repo):
    name = "2026-07-28-ac6-discard-refuse.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "sha": "cafef00dcafef00dcafef00dcafef00dcafef00d",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, result
    assert "--force" in result["error"], result
    # Disk state (AC4): the already-present value survives untouched — the
    # discard is refused, not silently applied nor silently retained.
    assert _shipped_in(handoff_repo, name) == "deadbeef"
    assert _deployment_state(handoff_repo, name) == "shipped"


def test_ac6b_same_commit_resupply_does_not_refuse(handoff_repo):
    """A full-length sha that canonically matches the stored 8-char
    prior_value (same commit, different length) is a legitimate no-op — must
    NOT trip AC6's refusal. Without this guard AC6's fix false-refuses every
    legitimate same-commit re-stamp (§ S11)."""
    name = "2026-07-28-ac6b-same-commit-noop.md"
    full_sha = "deadbeef" + "00" * 16  # 40-char hex, 8-char prefix = 'deadbeef'
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "sha": full_sha,
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert _shipped_in(handoff_repo, name) == "deadbeef"
    assert _deployment_state(handoff_repo, name) == "shipped"


def test_ac7_warning_distinguishes_left_unset_from_retained_prior_value(handoff_repo):
    """AC7: the SAME no-scope/no-sha call produces a DIFFERENT warning
    depending on whether shipped_in was ever set — forced via two calls
    against the same op, not read from the message string in isolation."""
    never_set_name = "2026-07-28-ac7-left-unset.md"
    handoff_repo.seed_handoff(never_set_name, "claimed", deployment_state="in_flight")

    never_set_result = _run(_handler(
        {"handoff_path": f"state/handoffs/{never_set_name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))
    assert never_set_result["exit_code"] == 1, never_set_result  # flip-refusal, no prior value to fall back on
    assert any(
        "left unset" in w for w in never_set_result["warnings"]
    ), never_set_result["warnings"]
    assert not any(
        "retained prior value" in w for w in never_set_result["warnings"]
    ), never_set_result["warnings"]

    retained_name = "2026-07-28-ac7-retained-prior.md"
    handoff_repo.seed_handoff(
        retained_name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )
    retained_result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{retained_name}",
            "mode": "stamp_only",
            "sha": "deadbeef" + "00" * 16,  # same-commit resupply — AC6b no-op, reaches AC7's split
        },
        handoff_repo.common_dir,
    ))
    assert retained_result["exit_code"] == 0, retained_result
    assert any(
        "retained prior value" in w and "deadbeef" in w for w in retained_result["warnings"]
    ), retained_result["warnings"]
    assert not any(
        "left unset" in w for w in retained_result["warnings"]
    ), retained_result["warnings"]


# ---------------------------------------------------------------------------
# AC14 (§ S12 site (b)): a stamp transport failure during archival must
# abort with a non-zero exit, not warn-and-continue — mode=supersede variant
# (the do_stamp twin of test_stamp_transport_failure_then_refuses_flip
# above, which only exercised mode=stamp_shipped). Pre-fix, mode=supersede
# had no downstream shipped_in-required check to incidentally compose a
# non-zero exit on top of the swallowed warning, so this is the mode where
# the old warn-and-continue behaviour was most dangerous: a silently
# half-superseded handoff.
# ---------------------------------------------------------------------------


def test_ac14_supersede_stamp_transport_failure_aborts_before_flip(handoff_repo, monkeypatch):
    import coordinator_core.archive_stamp as archive_stamp_mod

    name = "2026-07-28-ac14-supersede-transport-failure.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    monkeypatch.setattr(
        archive_stamp_mod,
        "stamp_shipped_in",
        lambda *a, **k: archive_stamp_mod.StampOutcome(exit_code=1, error="transport failure"),
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "state/handoffs/ac14-successor.md",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] != 0, result
    assert result["superseded"] is False, result
    # Disk state (AC4): the status flip must NOT have landed on top of an
    # unconfirmed stamp — abort-for-retry-safety, not proceed-and-report.
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert len(_archive_glob(handoff_repo, name)) == 0


# ---------------------------------------------------------------------------
# `kind` explicit override (2026-07-26, scope-derived-retirement audit Change
# 2) — a caller with a sha in hand may explicitly tag the write
# kind="successor" instead of the module's default kind="ship-commit".
# ---------------------------------------------------------------------------


def test_stamp_only_kind_successor_with_sha_is_honored(handoff_repo):
    """An explicit kind="successor" paired with sha= overrides the default
    kind="ship-commit" — the on-disk shipped_in_kind reflects the override,
    not the module's own default."""
    name = "2026-07-26-stamp-only-kind-successor.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "sha": "cafef00d",
            "kind": "successor",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == "cafef00d"
    assert _shipped_in_kind(handoff_repo, name) == "successor"
    assert _deployment_state(handoff_repo, name) == "shipped"


def test_kind_without_sha_is_usage_error(handoff_repo):
    """An explicit `kind` with no `sha` is rejected — an explicit kind only
    makes sense paired with the sha it describes (mirrors
    archive_stamp.stamp_shipped_in's own kind/sha cross-validation)."""
    name = "2026-07-26-kind-without-sha.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "kind": "successor",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert "sha" in result["error"].lower(), result
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert _deployment_state(handoff_repo, name) == "in_flight"


def test_kind_unsupported_value_is_usage_error(handoff_repo):
    """An explicit `kind` outside {'ship-commit', 'successor'} is rejected —
    'no-commit'/'scope-derived' have no explicit-override call shape here."""
    name = "2026-07-26-kind-unsupported.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "sha": "deadbeef",
            "kind": "scope-derived",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert "scope-derived" in result["error"], result
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert _deployment_state(handoff_repo, name) == "in_flight"


# ---------------------------------------------------------------------------
# successor_path — resolves the SUCCESSOR's own sha internally (example-doctrine-repo,
# 2026-07-26), so a caller with the successor's path in hand (e.g.
# /update-docs Phase 8's lineage-predecessor archival) does not need to
# resolve that sha itself and pass it via sha=/kind="successor".
# ---------------------------------------------------------------------------


def test_successor_path_resolvable_stamps_sha_and_kind_successor(handoff_repo):
    """successor_path supplied and resolvable -> sha + kind="successor" stamped.

    The predecessor handoff carries no `scope:` (so the legacy scope-derived
    path would find nothing on its own) — the successor's own commit is what
    resolves the sha here, via successor_path, not the predecessor's scope.
    """
    name = "2026-07-26-successor-path-resolvable.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    successor_name = "2026-07-26-successor-path-successor.md"
    handoff_repo.seed_handoff(successor_name, "claimed", deployment_state="in_flight")
    expected_sha = _path_sha(handoff_repo, f"state/handoffs/{successor_name}")[:8]

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "successor_path": f"state/handoffs/{successor_name}",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == expected_sha
    assert _shipped_in_kind(handoff_repo, name) == "successor"
    assert _deployment_state(handoff_repo, name) == "shipped"
    # The successor's own resolution succeeded -> the legacy scope-derived
    # selection warning must NOT fire (stamp_kind is no longer scope-derived).
    assert not any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]


def test_successor_path_unresolvable_falls_back_honestly(handoff_repo):
    """successor_path supplied but unresolvable -> honest fallback, surfaced
    not silent (falls back to this op's existing no-sha scope-derived
    default; the predecessor also has no `scope:`, so the flip is refused —
    same Defect-2 refusal gate as the no-successor-path no-scope case)."""
    name = "2026-07-26-successor-path-unresolvable.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")  # no scope:

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "successor_path": "state/handoffs/2026-07-26-never-committed.md",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["stamped"] is False
    assert any(
        "successor_path" in w and "resolved no commit" in w for w in result["warnings"]
    ), result["warnings"]
    assert any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert _deployment_state(handoff_repo, name) == "in_flight"


def test_successor_path_omitted_behavior_unchanged(handoff_repo):
    """successor_path omitted -> behavior unchanged (scope-derived path, as
    before this param existed)."""
    name = "2026-07-26-successor-path-omitted.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        extra=f"scope:\n  - state/handoffs/{name}",
    )
    _add_session_trailer_to_head(handoff_repo)
    expected_sha = _head_sha(handoff_repo)[:8]

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["stamped"] is True
    assert _shipped_in(handoff_repo, name) == expected_sha
    assert _shipped_in_kind(handoff_repo, name) == "scope-derived"
    assert any(
        "scope-derived" in w for w in result["warnings"]
    ), result["warnings"]


def test_successor_path_with_sha_is_usage_error(handoff_repo):
    """successor_path is mutually exclusive with sha/kind — ambiguous which
    sha would win."""
    name = "2026-07-26-successor-path-with-sha.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "stamp_only",
            "successor_path": "state/handoffs/some-successor.md",
            "sha": "cafef00d",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert "successor_path" in result["error"], result
    assert _shipped_in(handoff_repo, name) in (None, "null")
    assert _deployment_state(handoff_repo, name) == "in_flight"


# ---------------------------------------------------------------------------
# Review: code-reviewer F7 — stamp transport-failure warning path.
# ---------------------------------------------------------------------------


def test_stamp_transport_failure_then_refuses_flip(handoff_repo, monkeypatch):
    """A stamp_shipped_in transport failure (rc!=0) still surfaces its own
    warning, but — since it necessarily leaves shipped_in unresolved — now
    ALSO trips the Defect-2 flip-refusal gate (same silent-corruption class
    as an honest 'no commit found' resolution miss): the transport-failure
    warning is non-fatal on its own, but the flip refusal composes on top of
    it and the call now fails loud overall. Renamed from
    test_stamp_transport_failure_warns_but_does_not_fail (its old exit_code:0
    assertion described exactly the half-state this fix closes)."""
    import coordinator_core.archive_stamp as archive_stamp_mod

    name = "2026-07-19-stamp-transport-failure.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    # stamp_shipped_in is imported function-local inside _handler (see the
    # module docstring's import-cycle note / F4 above), so it must be
    # patched at its SOURCE (coordinator_core.archive_stamp) — patching the
    # handoff_archive_transition module's namespace would not be seen by
    # the fresh `from coordinator_core.archive_stamp import stamp_shipped_in`
    # each call performs.
    monkeypatch.setattr(
        archive_stamp_mod,
        "stamp_shipped_in",
        lambda *a, **k: archive_stamp_mod.StampOutcome(exit_code=1, error="transport failure"),
    )

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_shipped"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["stamped"] is False
    assert any("stamp_shipped_in exited" in w for w in result["warnings"]), result["warnings"]
    assert "--sha" in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"


# ---------------------------------------------------------------------------
# Review: code-reviewer F7 — git-mv concurrent-failure warning path.
# ---------------------------------------------------------------------------


def test_git_mv_failure_warns_but_does_not_fail(handoff_repo, monkeypatch):
    import coordinator_core.ops.handoff_archive_transition as hat

    # Terminal deployment_state — must clear the terminal-state precondition
    # to reach the (mocked) git-mv block at all.
    name = "2026-07-19-git-mv-failure.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="shipped", shipped_in="deadbeef",
        shipped_in_kind="ship-commit",
    )

    async def _boom_archive_and_commit(worktree, moves, subject):
        return (
            [],
            [{"candidate_id": moves[0].candidate_id, "reason": "simulated git-mv collision"}],
        )

    monkeypatch.setattr(hat, "archive_and_commit", _boom_archive_and_commit)

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["moved"] is False
    assert any("git mv failed" in w for w in result["warnings"]), result["warnings"]
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


# ---------------------------------------------------------------------------
# (j) mutual-exclusion / invalid mode -> usage error
# ---------------------------------------------------------------------------


def test_invalid_mode_is_usage_error(handoff_repo):
    name = "2026-07-19-bad-mode.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "stamp_only_and_supersede"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 2, result
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_missing_handoff_path(handoff_repo):
    result = _run(_handler({}, handoff_repo.common_dir))
    assert result["exit_code"] == 1


def test_missing_repo_root(handoff_repo):
    result = _run(_handler({"handoff_path": "state/handoffs/x.md"}, None))
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# Terminal-state precondition (example-doctrine-repo, 2026-07-26, plan C7) — this is the
# load-bearing assertion of this chunk: a NON-TERMINAL baton must not reach
# archive/handoffs/ via this op under ANY mode. See module docstring §
# Terminal-state precondition. Regression coverage for the defect that let
# mode="chain" (which stamps nothing, by contract) git-mv a non-terminal
# baton into archive/handoffs/ — a state handoff_transition._resolve_path's
# live-only containment made permanently unrepairable by any transition verb.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["awaiting_gate", "ready_to_fire", "in_flight"])
def test_chain_mode_refuses_each_non_terminal_state(handoff_repo, state):
    name = f"2026-07-26-chain-refuse-{state}.md"
    # awaiting_gate carries a cross-field required-companion field
    # (gate_dependency); seed_handoff writes raw frontmatter with no schema
    # validation, but supply it anyway so the fixture stays representative
    # of a real awaiting_gate record.
    extra = "gate_dependency: some-subsystem\n" if state == "awaiting_gate" else ""
    handoff_repo.seed_handoff(name, "claimed", deployment_state=state, extra=extra)

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 1, result
    assert result["moved"] is False
    assert "deployment_state" in result["error"], result
    assert "stamp_shipped" in result["error"] and "supersede" in result["error"], result
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_chain_mode_archives_already_closed_baton(handoff_repo):
    """A terminal deployment_state reached some OTHER way (e.g. a prior
    handoff.transition close call, not this op) is sufficient — chain mode
    still stamps nothing itself, it only requires the terminal state to
    already be on disk."""
    name = "2026-07-26-chain-closed.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="closed", extra="closed_reason: stale\n",
    )

    result = _run(_handler({"handoff_path": f"state/handoffs/{name}"}, handoff_repo.common_dir))

    assert result["exit_code"] == 0, result
    assert result["moved"] is True
    assert result["stamped"] is False
    assert len(_archive_glob(handoff_repo, name)) == 1


def test_terminal_precondition_applies_to_any_mode_not_just_chain(handoff_repo, monkeypatch):
    """The precondition is not an `if mode == "chain"` special case — it runs
    unconditionally for every mode that reaches the git-mv block, so a
    hypothetical FUTURE mode that forgets to stamp a terminal state before
    falling through to the move is caught too, without this test needing to
    be revisited when that mode is added."""
    import coordinator_core.ops.handoff_archive_transition as hat

    name = "2026-07-26-future-mode-non-terminal.md"
    handoff_repo.seed_handoff(name, "claimed", deployment_state="in_flight")

    # A mode this op has never heard of, added only to prove the precondition
    # generalizes: it stamps nothing (do_stamp/do_supersede/do_stamp_only are
    # all False for any mode not literally named "stamp_shipped"/"supersede"/
    # "stamp_only"), so it behaves exactly like an under-implemented future
    # mode falling straight through to the git-mv block.
    monkeypatch.setattr(hat, "_VALID_MODES", hat._VALID_MODES | {"future_noop_mode"})

    result = _run(_handler(
        {"handoff_path": f"state/handoffs/{name}", "mode": "future_noop_mode"},
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["moved"] is False
    assert "deployment_state" in result["error"], result
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_supersede_mode_still_archives_after_terminal_precondition_added(handoff_repo):
    """Non-regression: supersede stamps deployment_state:continued (terminal)
    BEFORE reaching the precondition, so it must still clear it and archive
    normally — the precondition must not double-refuse a mode that already
    reaches a terminal state on this same call."""
    name = "2026-07-26-supersede-still-archives.md"
    # DR-242 (Finding 1, C5 review fix): mode="supersede" now gates on
    # claimed_or_shipped at the op choke point — seed a legitimately claimed
    # predecessor rather than the bare never-claimed shape DR-242 exists to
    # refuse.
    handoff_repo.seed_handoff(
        name,
        "claimed",
        deployment_state="in_flight",
        claimed_at="2026-07-26T00:00:00Z",
        claimed_by="session-test",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-0002",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True
    assert result["moved"] is True
    assert len(_archive_glob(handoff_repo, name)) == 1


# ---------------------------------------------------------------------------
# C6a — C2 regression tests (docs/plans/2026-08-02-roadmap-baton-supersession-
# hazard.md, chunk C6a). C1 (ops/handoff_children.py's blocked_by_dependents
# resolver, PIN-1) and C2 (this module's new pre-flip gate, PIN-2) are peer
# chunks NOT YET IMPLEMENTED as of this authoring pass — these tests are
# authored red-before-green against the FUTURE call shape and MUST fail
# against current HEAD (see each test's own note for its specific failure
# mode; two of the six converge/pass vacuously today because there is no
# gate yet to violate their invariant — noted explicitly, not forced red).
#
# roadmap-baton fixtures satisfy schema_validate._cf_spinoff_roadmap_
# requires_graph (kind=roadmap-baton REQUIRES roadmap_id/stub_id/wave/
# blocks/blocked_by, blocked_by=[] permitted) so _supersede_continued's
# post-mutation validate_frontmatter gate does not itself abort the call for
# an unrelated reason.
# ---------------------------------------------------------------------------


_ROADMAP_BATON_FM = (
    "kind: roadmap-baton\n"
    "roadmap_id: roadmap-xwin\n"
    "stub_id: {stub_id}\n"
    "wave: 1\n"
    "blocks: []\n"
    "blocked_by: []\n"
)


def test_c2_refuses_flip_with_live_blocked_by_dependent(handoff_repo):
    """(1) A claimed, in_flight, kind:roadmap-baton predecessor with a LIVE
    blocked_by dependent must NOT be stamped `continued` — the refusal is an
    `_err` (exit_code 1), never a retention (`retained: True`) nor
    exit_code 0. Asserts PIN-2's live-dependents substring.

    Expected to FAIL against current HEAD: neither C1's blocked_by_dependents
    resolver nor C2's gate exist yet, so this op is still blocked_by-blind
    (F1) — the call proceeds, stamps deployment_state:continued, and
    archives, exit_code 0."""
    name = "2026-08-02-roadmap-baton-live-dep.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=_ROADMAP_BATON_FM.format(stub_id="roadmap-xwin-baton-live-dep"),
    )
    handoff_repo.seed_handoff(
        "2026-08-02-dependent-on-baton.md", "claimed", deployment_state="in_flight",
        extra="blocked_by:\n  - roadmap-xwin-baton-live-dep\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-live-dep-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert result.get("retained") is not True, result
    assert "refusing supersede" in result["error"], result
    assert "live blocked_by dependent" in result["error"], result
    assert "C-1" in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert _status(handoff_repo, name) == "claimed"
    assert len(_archive_glob(handoff_repo, name)) == 0
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


def test_c2_gate_canonicalizes_pre_rename_kind_alias(handoff_repo):
    """Review: code-reviewer (P1, Finding 1) — the gate must canonicalize
    `kind` before comparing, not raw-string-compare it: a predecessor still
    carrying the pre-rename alias `kind: spinoff-roadmap` (canonical_kind()
    maps it to `roadmap-baton`) with a LIVE blocked_by dependent must be
    refused exactly like a predecessor already spelled `kind: roadmap-baton`
    — a raw `_current_kind(contained) == "roadmap-baton"` compare would skip
    this gate entirely for the pre-rename spelling and fall through to the
    unconditional flip, reopening the exact hazard this gate exists to close."""
    name = "2026-08-02-roadmap-baton-pre-rename-alias.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=_ROADMAP_BATON_FM.format(
            stub_id="roadmap-xwin-baton-pre-rename-alias"
        ).replace("kind: roadmap-baton\n", "kind: spinoff-roadmap\n"),
    )
    handoff_repo.seed_handoff(
        "2026-08-02-dependent-on-baton-pre-rename-alias.md", "claimed",
        deployment_state="in_flight",
        extra="blocked_by:\n  - roadmap-xwin-baton-pre-rename-alias\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-pre-rename-alias-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert result.get("retained") is not True, result
    assert "refusing supersede" in result["error"], result
    assert "live blocked_by dependent" in result["error"], result
    assert "C-1" in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert _status(handoff_repo, name) == "claimed"
    assert len(_archive_glob(handoff_repo, name)) == 0
    assert (handoff_repo.root / "state" / "handoffs" / name).exists()


def test_c2_refuses_flip_on_indeterminate_blocked_by_resolution(handoff_repo, monkeypatch):
    """(2) An indeterminate blocked_by resolution (C1's tri-state, or
    scan_errors) also refuses via `_err`, fail-closed — distinct message
    from the live-dependents refusal (PIN-2).

    Message updated 2026-08-05 (DR-126 § Clarifications C-1, plan
    c2-supersede-gate-chaseable-terminus chunk C1): the refusal is now
    ALWAYS a deliberate policy decline keyed on `kind` — "not a deliberate
    policy decline" became false the moment C1 landed, since even the
    indeterminate arm refuses on kind, not on the scan outcome. The scan
    failure changes only that the live-dependent list cannot be named, not
    why the gate refused — asserted below via the C-1 citation and the
    "blocked_by scan could not complete" substring instead.

    `blocked_by_dependents` is patched onto the module namespace with
    raising=False since C1 (PIN-1/PIN-2) predates this chunk — the patch
    exercises the indeterminate arm without a real scan failure."""
    import coordinator_core.ops.handoff_archive_transition as hat

    name = "2026-08-02-roadmap-baton-indeterminate.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=_ROADMAP_BATON_FM.format(stub_id="roadmap-xwin-baton-indeterminate"),
    )

    def _boom_resolver(candidate_path, worktree_root, exclude=None):
        return {
            "state": "indeterminate",
            "dependents": [],
            "identifiers": ["roadmap-xwin-baton-indeterminate"],
            "scan_errors": ["simulated: cannot scan archive/handoffs/"],
            "error": "simulated: cannot scan archive/handoffs/",
        }

    monkeypatch.setattr(hat, "blocked_by_dependents", _boom_resolver, raising=False)

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-indeterminate-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "blocked_by scan could not complete" in result["error"], result
    assert "live blocked_by dependent" not in result["error"], result
    assert "C-1" in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"


def test_c2_replay_convergence_short_circuits_before_gate(handoff_repo):
    """(3) REPLAY CONVERGENCE (C2 body, AC7): a predecessor already stamped
    deployment_state:continued with continued_into equal to the requested
    successor, that STILL has live blocked_by dependents, converges via the
    existing byte-identical no-op path on replay rather than refusing — the
    gate condition must guard the TRANSITION into continued, not the steady
    state.

    NOTE: this test is expected to PASS against current HEAD, and that is
    correct, not an oversight — there is no C1/C2 gate yet to short-circuit
    around, so nothing prevents convergence today. It becomes a load-bearing
    regression test only once C2 lands (protecting against a naive gate
    placed ahead of `_supersede_continued`'s own idempotency check, which
    would turn this exact replay into a false refusal)."""
    name = "2026-08-02-roadmap-baton-replay-convergence.md"
    successor = "hnd-successor-replay-convergence-0001"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="continued",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=(
            _ROADMAP_BATON_FM.format(stub_id="roadmap-xwin-baton-replay")
            + f"continued_into: {successor}\n"
        ),
    )
    handoff_repo.seed_handoff(
        "2026-08-02-dependent-on-baton-replay.md", "claimed", deployment_state="in_flight",
        extra="blocked_by:\n  - roadmap-xwin-baton-replay\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": successor,
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    # Already terminal (deployment_state:continued) before this call, and no
    # live-children (the OLD guard is blocked_by-blind either way) -> this
    # replay archives the file, same as any other terminal-on-entry supersede
    # call. Read the post-call state from wherever it now lives.
    archived = _archive_glob(handoff_repo, name)
    assert len(archived) == 1, archived
    text = archived[0].read_text(encoding="utf-8")
    split = split_frontmatter(text)
    assert read_fm_field(split.fm_text, "deployment_state") == "continued"
    assert read_fm_field_unquoted(split.fm_text, "continued_into") == successor


def test_c2_gate_refusal_precedes_terminal_state_precondition_message(handoff_repo):
    """(4) GATE POSITION: the refusal fires BEFORE the terminal-state
    precondition (handoff_archive_transition.py:1311) — the refusal names
    the live-dependents/indeterminate cause, NOT the precondition's
    "deployment_state is ... not terminal" message. Also guards against a
    suppressed-stamp-but-falls-through shape (the module docstring's
    GATE MUST RETURN EARLY substrate probe).

    Expected to FAIL against current HEAD for the same F1 reason as test
    (1): the call proceeds all the way to a successful supersede+archive
    (exit_code 0), demonstrating no gate fires at all, let alone ahead of
    the precondition."""
    name = "2026-08-02-roadmap-baton-gate-position.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=_ROADMAP_BATON_FM.format(stub_id="roadmap-xwin-baton-gate-position"),
    )
    handoff_repo.seed_handoff(
        "2026-08-02-dependent-gate-position.md", "claimed", deployment_state="in_flight",
        extra="blocked_by:\n  - roadmap-xwin-baton-gate-position\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-gate-position-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert "live blocked_by dependent" in result["error"], result
    assert "not terminal" not in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"


def test_ac11_envelope_keys_uniform_across_err_usage_error_and_retain(handoff_repo):
    """(5) AC11: `_err` / `_usage_error` / the retain path carry a uniform
    envelope key set — no third `retain_kind` value is introduced, and no
    caller has to branch on which keys exist depending on outcome.

    Expected to FAIL against current HEAD: `_err`/`_usage_error` (9 keys, no
    `retain_kind`/`message`) and the retain path (10 keys, no `error`) are
    NOT presently uniform — this is the pre-existing gap AC11 closes, not
    something C1/C2 introduce."""
    usage_result = _run(_handler(
        {"handoff_path": "state/handoffs/does-not-matter.md", "mode": "bogus-mode"},
        handoff_repo.common_dir,
    ))
    assert usage_result["exit_code"] == 2, usage_result

    err_result = _run(_handler(
        {"handoff_path": "state/handoffs/2026-08-02-ac11-does-not-exist.md"},
        handoff_repo.common_dir,
    ))
    assert err_result["exit_code"] == 1, err_result

    retain_name = "2026-08-02-ac11-retain-path.md"
    handoff_repo.seed_handoff(retain_name, "claimed", deployment_state="in_flight")
    handoff_repo.seed_handoff(
        "2026-08-02-ac11-retain-path-child.md", "claimed",
        deployment_state="in_flight", predecessor=retain_name,
    )
    retain_result = _run(_handler(
        {"handoff_path": f"state/handoffs/{retain_name}"}, handoff_repo.common_dir,
    ))
    assert retain_result["retained"] is True, retain_result

    assert set(usage_result.keys()) == set(err_result.keys()) == set(retain_result.keys()), (
        sorted(usage_result.keys()), sorted(err_result.keys()), sorted(retain_result.keys()),
    )


def test_c2_exclude_semantics_successor_does_not_self_block(handoff_repo):
    """(6) EXCLUDE SEMANTICS (AC3a) — INVERTED 2026-08-05, DR-126 §
    Clarifications C-1, plan c2-supersede-gate-chaseable-terminus chunk C1.

    Original assertion (pre-C1): a dependent-free roadmap baton supersedes.
    That is now the DESIGNED-RED case DR-126 forbids — after C1,
    `canonical_kind(...) == "roadmap-baton"` ALONE decides the refusal, so a
    baton with NO live blocked_by dependents (which is exactly what
    `exclude` produces here, by design) is now REFUSED, not superseded.

    This test's docstring-stated purpose was never "dependent-free batons
    supersede" — it exists to protect `exclude`'s THREADING into
    `blocked_by_dependents`: a scaffolded successor that inherits its
    predecessor's blocked_by list (containing the predecessor's own
    stub_id) must not read itself as a blocking dependent. After C1,
    `exclude` is a MESSAGE property, not a decision property (both
    "dependents" and "none" refuse identically) — so a naive flip to
    `exit_code == 1` alone would pass even if `exclude` threading were
    deleted outright (the "dependents" arm would fire instead and still
    refuse). The inversion below still asserts refusal AND pins the
    negative substring that `exclude` suppressed the successor's
    self-naming: the refusal message must be the kind-alone "none" arm
    (no "live blocked_by dependent" substring, no successor rel_id), not
    the "dependents" arm — that is the property this test protects."""
    name = "2026-08-02-roadmap-baton-exclude.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra=_ROADMAP_BATON_FM.format(stub_id="roadmap-xwin-baton-exclude"),
    )
    successor_name = "2026-08-02-roadmap-baton-exclude-successor.md"
    handoff_repo.seed_handoff(
        successor_name, "claimed", deployment_state="in_flight", predecessor=name,
        extra="blocked_by:\n  - roadmap-xwin-baton-exclude\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": f"state/handoffs/{successor_name}",
            "exclude": [handoff_repo.abs_path(successor_name)],
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 1, result
    assert result["superseded"] is False, result
    assert "C-1" in result["error"], result
    # Negative substring (AC3a): the refusal fired on the kind-alone "none"
    # arm — `exclude` suppressed the successor's own inherited blocked_by
    # entry from reading as a live dependent, so the "dependents" arm's
    # substring and the successor's own rel_id must NOT appear.
    assert "live blocked_by dependent" not in result["error"], result
    assert successor_name not in result["error"], result
    assert "roadmap-baton-exclude-successor" not in result["error"], result
    assert _deployment_state(handoff_repo, name) == "in_flight"
    assert len(_archive_glob(handoff_repo, name)) == 0


def test_c2_non_roadmap_baton_predecessor_with_dependents_still_supersedes(handoff_repo):
    """AC3c (DR-126 § Clarifications C-1, plan c2-supersede-gate-
    chaseable-terminus chunk C1): the gate did NOT widen past
    `canonical_kind(...) == "roadmap-baton"` — this plan's own thesis (§
    Problem: "Nothing in this plan changes what the guard refuses") is that
    fixing a false explanation, and now keying the SAME decision on `kind`
    alone, is not narrowing OR widening the condition beyond that one kind.
    A non-roadmap-baton predecessor (ordinary session-handoff succession)
    with a live `blocked_by` dependent must still supersede unconditionally,
    exactly as before C1 — `blocked_by_dependents` is composed ONLY inside
    the `canonical_kind(...) == "roadmap-baton"` block, so a non-roadmap-
    baton predecessor never reaches it at all, regardless of its own
    blocked_by dependents."""
    name = "2026-08-05-session-handoff-with-dependent.md"
    handoff_repo.seed_handoff(
        name, "claimed", deployment_state="in_flight",
        claimed_at="2026-08-01T00:00:00Z", claimed_by="session-test",
        extra="stub_id: session-handoff-with-dependent\nblocked_by: []\n",
    )
    handoff_repo.seed_handoff(
        "2026-08-05-dependent-on-non-baton.md", "claimed", deployment_state="in_flight",
        extra="blocked_by:\n  - session-handoff-with-dependent\n",
    )

    result = _run(_handler(
        {
            "handoff_path": f"state/handoffs/{name}",
            "mode": "supersede",
            "continued_into": "hnd-successor-non-baton-0001",
        },
        handoff_repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["superseded"] is True, result
    assert result["moved"] is True, result
    assert len(_archive_glob(handoff_repo, name)) == 1
