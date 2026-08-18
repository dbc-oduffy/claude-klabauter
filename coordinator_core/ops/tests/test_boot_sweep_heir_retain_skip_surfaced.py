"""
coordinator_core.ops.tests.test_boot_sweep_heir_retain_skip_surfaced

Regression test for the "succession heir never archives — the retain is
invisible" defect (state/bug-backlog/2026-08-14-a-live-session-is-running-
plugin-dir-les-2ee0e6522f92.yaml; cross-repo/inbox/2026-08-14-example-retrieval-repo-em-
succession-heir-never-archives.md, "Second defect — the retain is invisible").

archive_handoffs._is_terminal's Branch-A heir path (module docstring "Heir
branch", H4) RETAINS a heir candidate whose own shipped_in is absent or
unresolvable — but that verdict is a plain `return False, ...` from
_is_terminal, so archive_handoffs._handle_preview_handoffs never appends the
candidate to candidates[] at all (the module's own "H3 falsifiability
diagnostic" section only covers the H3 promoter-owned shape, not H4).
session.boot_sweep._sweep_consumed_handoffs previously built consumed_skipped
ONLY from recency_skipped + awaiting_adjudication_skipped +
dest_conflict_skipped (plus, on the live path, _handle_act_handoffs's own
skipped[]) — none of which an H4-retained heir ever reaches, since it never
became a preview candidate in the first place.
coordinator/bin/sweep-consumed-handoffs.py's own module docstring promises
every skip is "always surfaced — never silently swallowed"; an H4-retained
heir broke that promise silently, with no signal at any cadence gate.

Fix locus: coordinator_core/ops/session/boot_sweep.py's
_sweep_consumed_handoffs — a new read-only heir_retained_skipped[] pass
(reusing the SAME _classify_heir_children / _shipped_in_resolvable /
_is_promoter_owned_spinoff_roadmap primitives the pre-existing heir pre-stamp
pass already calls) folds the H4-retained verdict into every
consumed_handoffs.skipped return point. Deliberately does NOT touch
coordinator_core/ops/fleet/archive_handoffs.py (_is_terminal's H4 gate and its
FIX-1 rationale are unchanged, load-bearing doctrine — this surfaces the
verdict, it does not change it) nor coordinator/bin/sweep-consumed-
handoffs.py (that CLI already prints whatever consumed_handoffs.skipped[]
contains unconditionally; it needs no change and this file does not exercise
it — see the dispatch brief's AC2, confirmed manually by running that CLI
against an equivalent fixture, not from pytest).

Coverage:
  (a) AC1 — a heir candidate retained by the H4 gate (no resolvable
      shipped_in, no scope field for the pre-stamp pass to supply one) appears
      in session.sweep_consumed_handoffs's consumed_handoffs.skipped[] with
      its retain reason string, on a LIVE (dry_run=false) run.
  (b) AC3 — the SAME candidate, SAME reason, appears in the dry_run:true
      preview's consumed_handoffs.skipped[] too — dry-run/live agreement for
      a fixture where the best-effort pre-stamp attempt cannot succeed on
      either path (no scope, no shipped_in), so the outcome does not diverge.
  (c) Control — a plain non-heir consumed+in_flight candidate keeps its
      pre-existing DR-084 awaiting-adjudication disposition, unaffected by
      the new heir-retain pass — proves the pass is heir-scoped, not a
      blanket behavior change.

AC5 (both-directions confirmation — this file's tests fail against the
pre-fix boot_sweep.py and pass after) is confirmed by the dispatching agent
via a local git-stash round-trip, not automated in this file; see the run
report for the recorded confirmation.

Negative-spec:
  - Does NOT re-test the full heir-archival matrix (resolvable shipped_in,
    WARN markers, two-repo routing, H3 promoter-owned carve-out) — that
    matrix already lives in coordinator_core/ops/session/tests/
    test_boot_sweep.py; this file's scope is exclusively the NEW
    skip-surfacing pass.
  - Does NOT assert against coordinator/bin/sweep-consumed-handoffs.py's own
    stdout formatting — out of this dispatch's file scope (AC2), exercised
    manually instead.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.sweep_consumed_handoffs  # noqa: F401,E501 — fires register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session.sweep_consumed_handoffs import _handler

# Real git spawn is load-bearing: the sweep reads real HEAD/status/log state
# to decide archival; runs at cadence gates, not per-commit.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "session.sweep_consumed_handoffs"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.sweep_consumed_handoffs @register_op did not fire"
)

_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    """Mirrors test_sweep_consumed_handoffs.py's identically-named fixture —
    stamp_shipped_in-adjacent machinery resolves the caller as this session."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class HeirRetainRepo:
    """Minimal temporary git repo for this file's tests.

    Deliberately self-contained (not shared with test_boot_sweep.py's
    BootRepo or test_sweep_consumed_handoffs.py's ConsumedRepo) per the
    session test package convention — see those fixtures' own docstrings.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        args_list = list(args)
        if (
            len(args_list) >= 3
            and args_list[0] == "commit"
            and args_list[1] == "-m"
            and "Session-Id:" not in args_list[2]
        ):
            args_list[2] = f"{args_list[2]}\n\nSession-Id: {_DEFAULT_TEST_SESSION_ID}"
        return subprocess.run(
            ["git"] + args_list, cwd=str(self.root), capture_output=True, check=True,
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root), capture_output=True, check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_handoff(self, name: str, status: str, extra_frontmatter: str = "") -> Path:
        """Write and commit a state/handoffs/<name>.md. Returns its absolute path."""
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["title: \"Test Handoff\"", f"status: {status}", "created: 2026-01-01"]
        if extra_frontmatter.strip():
            fm_lines.append(extra_frontmatter.strip())
        fm_block = "\n".join(fm_lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def seed_successor(self, name: str, predecessor_path: Path) -> Path:
        """Write and commit a LIVE (status:active) successor naming
        predecessor_path via the `predecessor:` succession edge — this is
        what makes predecessor_path's own handoff a heir candidate (mirrors
        test_boot_sweep.py's heir-fixture shape)."""
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            'title: "Successor"\n'
            "status: active\n"
            f"predecessor: {predecessor_path}\n"
            "created: 2026-01-01\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add successor {name}")
        return path


@pytest.fixture
def heir_repo(tmp_path) -> HeirRetainRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args), cwd=str(repo_root), capture_output=True, check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "heir-retain-test@claude-klabauter.test")
    _git("config", "user.name", "Heir Retain Test")
    _git("config", "commit.gpgsign", "false")
    (repo_root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo_root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return HeirRetainRepo(repo_root)


# ---------------------------------------------------------------------------
# AC1 — live run surfaces the H4-retained heir in consumed_handoffs.skipped[]
# ---------------------------------------------------------------------------


def test_heir_retained_no_shipped_in_surfaces_in_skipped_live(heir_repo):
    """AC1 — a heir candidate retained by _is_terminal's H4 gate (no
    resolvable shipped_in, no scope field for the pre-stamp pass to fill one
    in) must appear in consumed_handoffs.skipped[] with its retain reason —
    on a LIVE (dry_run=false) run. This is the defect: before the fix, this
    candidate is neither archived nor skipped — it vanishes."""
    parent_path = heir_repo.seed_handoff(
        "2026-08-14-heir-retained.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
        # No scope field -> the pre-stamp pass finds nothing to stamp; no
        # shipped_in field either -> H4 eligibility gate fails -> RETAIN.
    )
    parent_rel = "state/handoffs/2026-08-14-heir-retained.md"
    heir_repo.seed_successor("2026-08-14-heir-retained-child.md", parent_path)

    result = _run(_handler({"dry_run": False}, repo_root=heir_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"a heir candidate with no resolvable shipped_in must NOT be archived "
        f"(H4 gate, archive_handoffs._is_terminal); got archived_ids={archived_ids!r}"
    )

    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert parent_rel in skip_map, (
        f"AC1: a heir candidate retained by the H4 gate must appear in "
        f"consumed_handoffs.skipped[] — this is the silent-drop this test "
        f"guards against; got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    reason = skip_map[parent_rel]
    assert "no resolvable" in reason and "shipped_in" in reason, (
        f"skip reason must name the H4 gate's own retain rationale; got {reason!r}"
    )
    assert "2026-08-14-heir-retained-child.md" in reason, (
        f"skip reason must name the succeeding child (mirrors "
        f"archive_handoffs._HEIR_NOTE_PREFIX's own note shape); got {reason!r}"
    )


# ---------------------------------------------------------------------------
# AC3 — dry-run preview reports the same skip a live run would
# ---------------------------------------------------------------------------


def test_heir_retained_no_shipped_in_surfaces_in_skipped_dry_run(heir_repo):
    """AC3 — the identical skip (same id, same reason) appears in the
    dry_run:true preview as well, for this fixture where the best-effort
    pre-stamp attempt cannot succeed on either path (no scope, no
    shipped_in), so dry-run and live do not diverge on this candidate."""
    parent_path = heir_repo.seed_handoff(
        "2026-08-14-heir-retained-dry.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    parent_rel = "state/handoffs/2026-08-14-heir-retained-dry.md"
    heir_repo.seed_successor("2026-08-14-heir-retained-dry-child.md", parent_path)

    dry = _run(_handler({"dry_run": True}, repo_root=heir_repo.common_dir))
    live = _run(_handler({"dry_run": False}, repo_root=heir_repo.common_dir))

    assert dry["exit_code"] == 0 and live["exit_code"] == 0, (
        f"expected exit_code:0 on both paths; dry={dry!r} live={live!r}"
    )

    dry_skip_map = {s["id"]: s["reason"] for s in dry["consumed_handoffs"]["skipped"]}
    live_skip_map = {s["id"]: s["reason"] for s in live["consumed_handoffs"]["skipped"]}

    assert parent_rel in dry_skip_map, (
        f"AC3: dry-run preview must report the same heir-retain skip a live "
        f"run would; got dry skipped={dry['consumed_handoffs']['skipped']!r}"
    )
    assert parent_rel in live_skip_map, (
        f"AC3: live run must also report the skip; "
        f"got live skipped={live['consumed_handoffs']['skipped']!r}"
    )
    assert dry_skip_map[parent_rel] == live_skip_map[parent_rel], (
        f"AC3: dry-run and live paths must agree on the retain reason; "
        f"dry={dry_skip_map[parent_rel]!r} live={live_skip_map[parent_rel]!r}"
    )


# ---------------------------------------------------------------------------
# Control — a non-heir candidate is unaffected by the new pass
# ---------------------------------------------------------------------------


def test_non_heir_candidate_unaffected(heir_repo):
    """Control — a plain non-heir consumed+in_flight candidate (no
    referencing successor at all) keeps its pre-existing DR-084
    awaiting-adjudication disposition, unaffected by the new heir-retain
    pass — proves the pass is heir-scoped, not a blanket behavior change."""
    heir_repo.seed_handoff(
        "2026-08-14-non-heir.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    non_heir_rel = "state/handoffs/2026-08-14-non-heir.md"

    result = _run(_handler({"dry_run": False}, repo_root=heir_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert skip_map.get(non_heir_rel) == "awaiting-adjudication-dr084", (
        f"a non-heir candidate must keep its pre-existing DR-084 disposition, "
        f"unaffected by the new heir-retain skip pass; got skip_map={skip_map!r}"
    )


# ---------------------------------------------------------------------------
# Branch-B attribution — a terminal deployment_state never reached H4
# ---------------------------------------------------------------------------


def test_continued_predecessor_reports_cascade_not_h4(heir_repo):
    """A `continued` predecessor with a live successor qualifies via
    archive_handoffs._is_terminal's Branch B (checked FIRST, before the
    Branch-A heir path), so H4 never ran and is not what retained it — the
    live-successor check is. Reporting it under the H4 note misattributes
    the gate and, via "retained for reaper", names an actor that will never
    take it: reap-orphaned-in-flight-handoffs.py stamps shipped_in only for
    a dead-holder orphan that provably shipped. The clearing actor is the
    cascade (archive_handoffs module docstring "CASCADE archival").

    Spec backlink: cross-repo/archive/2026-08-18-example-retrieval-repo-em-consumed-
    handoff-archive-retained-for-absent-reaper.md.
    """
    parent_path = heir_repo.seed_handoff(
        "2026-08-14-continued-predecessor.md", "consumed",
        extra_frontmatter="deployment_state: continued",
    )
    parent_rel = "state/handoffs/2026-08-14-continued-predecessor.md"
    heir_repo.seed_successor("2026-08-14-continued-child.md", parent_path)

    result = _run(_handler({"dry_run": False}, repo_root=heir_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"a Branch-B candidate with a LIVE successor is retained by Check 3, "
        f"not archived; got archived_ids={archived_ids!r}"
    )

    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert parent_rel in skip_map, (
        f"the retention must still be surfaced (sweep-consumed-handoffs' "
        f"never-silently-swallowed contract); got "
        f"skipped={result['consumed_handoffs']['skipped']!r}"
    )
    reason = skip_map[parent_rel]
    assert "reaper" not in reason, (
        f"a `continued` record is never collected by the reaper — the note must "
        f"not promise that actor; got {reason!r}"
    )
    assert "shipped_in" not in reason, (
        f"H4's ship-evidence rationale did not run for this candidate and must "
        f"not be cited as the retain reason; got {reason!r}"
    )
    assert "cascade" in reason and "deployment_state=continued" in reason, (
        f"the note must name the actual gate and the clearing actor; got {reason!r}"
    )
    assert "2026-08-14-continued-child.md" in reason, (
        f"the note must name the live successor it is waiting on; got {reason!r}"
    )
