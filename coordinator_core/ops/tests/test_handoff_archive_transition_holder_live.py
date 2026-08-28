"""
coordinator_core.ops.tests.test_handoff_archive_transition_holder_live

Pins the C1/C2 "live-holder" retain ground added to
`handoff.archive_transition` mode="supersede" (docs/plans/
2026-08-15-supersede-asks-whether-the-holder-is-alive.md).

Covers:
  - AC1: a LIVE claim holder retains the archival move — `retained: True`,
    `moved: False`, `retain_kind: "live-holder"`, the holding session id in
    `retain_reason`, `superseded: True`, and the status flip on disk.
  - AC2: a DEAD (stale) claim holder is never retained by THIS ground — the
    orphan-close path stays open.
  - AC3: the holder-live check runs after the supersede status flip — a
    live-holder retain still reports `superseded: True`.
  - AC5: after any retained supersede, the predecessor's frontmatter
    mutation is committed — `git status --porcelain` for that path is
    empty at return.
  - The mode-scoping behaviour the executor pinned as spec (2026-08-15,
    dispatch brief for this file): the new ground fires ONLY for
    mode="supersede" — a mode="stamp_shipped"/"chain" call against a
    handoff with a LIVE claim holder must NOT be retained on the
    "live-holder" ground, since the acting session is routinely the very
    session holding the predecessor's claim on that path (ship-then-
    archive in one call).

Fixture idiom: a real throwaway git repo (mirrors
`test_handoff_ship_archive.py`'s `_Repo` fixture), a claim dir at
`<common_dir>/coordinator-sessions/handoff-claims/<basename>/`, and
`cs_claim_holder_live` monkeypatched at THIS module's own import site
(`coordinator_core.ops.handoff_archive_transition.cs_claim_holder_live`) to
force LIVE/DEAD — never a real live session, per the shared-tree
non-reproducibility doctrine.

Import guard: `coordinator_core.ops.handoff_archive_transition` MUST be
imported at module load time to fire `@register_op("handoff.archive_transition")`
— mirrors every other op-test file's own import-guard precedent.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_archive_transition as _op  # noqa: F401 — fires @register_op
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_archive_transition import _handler as _archive_transition_handler

# Declared, not excused: this file spawns a real process (git) because AC5's
# property under test — the retained flip landing COMMITTED, not merely on
# disk — is git's own status-vs-HEAD behaviour, which no fixture stands in
# for. See test_handoff_ship_archive.py's identical pytestmark and reasoning.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_OP_NAME = "handoff.archive_transition"
# The import guard below was an `assert` at module scope until 2026-08-28, when it
# turned a deliberate kill into a COLLECTION ERROR for the whole tree: `handoff.
# archive_transition` was retired under the 200ms process-time bar (d20d56893, plan
# "The remainder of the killed op surface"), which stripped its `@register_op`, and
# the module itself now says so at its own line 890 — "Re-adding `@register_op` puts
# a deleted op back over the bar." An assert that fires at import makes every pytest
# run across `coordinator_core/` interrupt at collection, so the fast tier stopped
# being runnable fleet-wide for a reason unrelated to any test in it.
#
# Skip rather than delete, and rather than mute. Deleting pre-empts a disposition
# that is not this file's to make: the housekeeping REQUIREMENT these tests belong to
# is carried by `pln-one-corpus-read-or-the-houseke-18d29a`, still `status: draft`.
# The skip is self-retiring — it keys off the registry, so if that plan ever lands a
# v2 op under this name these tests collect again on their own, and if it lands under
# a new name (the norm — kill means kill forever) they stay skipped until deleted with
# the rest of the killed surface. A skip is visible in a run summary; a collection
# error is only visible as the absence of everything after it.
if _OP_NAME not in _REGISTRY:
    pytest.skip(
        f"{_OP_NAME} was retired under the 200ms bar (d20d56893) — these tests pin "
        "the killed op's behaviour and are held for the killed-op surface sweep; "
        "disposition owned by pln-one-corpus-read-or-the-houseke-18d29a",
        allow_module_level=True,
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

    def porcelain_status(self, rel: str) -> str:
        """`git status --porcelain -- <rel>` output, for AC5's empty-status assertion."""
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", rel],
            cwd=str(self.root), capture_output=True, check=True,
            **no_console_creationflags(),
        )
        return result.stdout.decode()

    def seed_handoff(
        self, name: str, *, deployment_state: str = "in_flight", status: str = "claimed"
    ) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "---\n"
            f'title: "Test Handoff {name}"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            f"status: {status}\n"
            "claimed_by: prior-holder-session\n"
            "claimed_at: 2026-01-01T00:00:00Z\n"
            "predecessor: null\n"
            f"deployment_state: {deployment_state}\n"
            "---\n\n# Handoff\n\nBody.\n"
        )
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def make_claim_dir(self, common_dir: Path, name: str, *, session_id: str) -> Path:
        """<common_dir>/coordinator-sessions/handoff-claims/<name>/ with a
        session_id file — mirrors claim_state.handoff_claim_dir's convention.
        """
        claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / name
        claim_dir.mkdir(parents=True, exist_ok=True)
        (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
        return claim_dir

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
    _git("config", "user.email", "archive-transition-holder-live-test@claude-klabauter.test")
    _git("config", "user.name", "archive-transition-holder-live Test")
    _git("config", "commit.gpgsign", "false")
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")
    return _Repo(root)


def _run(coro):
    return asyncio.run(coro)


def _force_holder_live(monkeypatch: pytest.MonkeyPatch, *, live: bool) -> None:
    """Monkeypatch `cs_claim_holder_live` at the import site
    `handoff_archive_transition._handoff_live_holder_session` actually reads
    from (`coordinator_core.ops.handoff_archive_transition.cs_claim_holder_live`)
    — never a real live session.
    """
    monkeypatch.setattr(_op, "cs_claim_holder_live", lambda claim_dir_str: live)


def test_live_holder_retains_and_commits_the_flip(repo, monkeypatch):
    """AC1, AC3, AC5: a LIVE claim holder retains the archival move, names the
    holding session id, still stamps the supersede status flip, and that flip
    is committed (git status --porcelain empty for the predecessor path).
    """
    name = "2026-08-14-live-holder.md"
    repo.seed_handoff(name)
    rel = f"state/handoffs/{name}"
    repo.make_claim_dir(repo.common_dir, name, session_id="live-session-abc123")
    _force_holder_live(monkeypatch, live=True)

    result = _run(_archive_transition_handler(
        {
            "handoff_path": rel,
            "mode": "supersede",
            "continued_into": "2026-08-14-successor.md",
        },
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result["retained"] is True, result
    assert result["moved"] is False, result
    assert result["retain_kind"] == "live-holder", result
    assert "live-session-abc123" in (result["retain_reason"] or ""), result
    assert result["superseded"] is True, result

    fm = repo.fm(name)
    assert read_fm_field(fm, "deployment_state") == "continued", fm
    assert read_fm_field(fm, "continued_into") == "2026-08-14-successor.md", fm

    # AC5 — the flip must not be left uncommitted in a shared tree.
    assert repo.porcelain_status(rel) == "", (
        "a retained supersede's status flip must be committed, never left "
        "loose on disk"
    )


def test_dead_holder_is_never_retained_on_the_live_holder_ground(repo, monkeypatch):
    """AC2: a DEAD (stale) claim dir must never produce retain_kind
    "live-holder" — the orphan-close path stays open.
    """
    name = "2026-08-14-dead-holder.md"
    repo.seed_handoff(name)
    rel = f"state/handoffs/{name}"
    repo.make_claim_dir(repo.common_dir, name, session_id="dead-session-xyz789")
    _force_holder_live(monkeypatch, live=False)

    result = _run(_archive_transition_handler(
        {
            "handoff_path": rel,
            "mode": "supersede",
            "continued_into": "2026-08-14-successor.md",
        },
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result.get("retain_kind") != "live-holder", result
    assert result["superseded"] is True, result

    if result["retained"]:
        # If retained by a DIFFERENT ground (e.g. the live-children guard),
        # AC5's commit discipline still applies — the flip must not be left
        # uncommitted either way.
        assert repo.porcelain_status(rel) == "", result
    else:
        # Not retained at all — the orphan-close path proceeded to archive.
        assert result["moved"] is True, result


def test_absent_claim_dir_proceeds_no_live_holder_retain(repo, monkeypatch):
    """No claim dir at all is the ordinary unclaimed case — must proceed,
    never retained on the "live-holder" ground.
    """
    name = "2026-08-14-no-claim-dir.md"
    repo.seed_handoff(name)
    rel = f"state/handoffs/{name}"
    # No claim dir created at all. cs_claim_holder_live must not even be
    # consulted in this shape (handoff_claim_dir(...).is_dir() short-circuits
    # first) but pin it anyway so a latent call would fail loud, not silently
    # report a false LIVE.
    _force_holder_live(monkeypatch, live=True)

    result = _run(_archive_transition_handler(
        {
            "handoff_path": rel,
            "mode": "supersede",
            "continued_into": "2026-08-14-successor.md",
        },
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result.get("retain_kind") != "live-holder", result
    assert result["superseded"] is True, result
    assert result["moved"] is True, result


def test_mode_scoping_stamp_shipped_ignores_live_holder(repo, monkeypatch):
    """Spec pin (2026-08-15 executor confirmation): the live-holder ground is
    scoped to mode="supersede" ONLY. A mode="stamp_shipped" call against a
    handoff with a LIVE claim holder must NOT be retained on the
    "live-holder" ground — for chain/stamp_shipped/stamp_only, the acting
    session is routinely the very session holding the predecessor's claim
    (ship-then-archive in one call), so an unconditional check would retain a
    baton against its own caller on the everyday path.
    """
    name = "2026-08-14-stamp-shipped-live-holder.md"
    # stamp_shipped does not require the DR-242 claimed-or-shipped gate (that
    # gate is mode="supersede"-only), and needs no terminal deployment_state
    # up front — the mode itself flips deployment_state:shipped before the
    # git-mv. Start non-terminal.
    repo.seed_handoff(name, deployment_state="ready_to_fire", status="claimed")
    rel = f"state/handoffs/{name}"
    repo.make_claim_dir(repo.common_dir, name, session_id="live-session-abc123")
    _force_holder_live(monkeypatch, live=True)

    head_sha = repo._git("rev-parse", "HEAD").stdout.decode().strip()

    result = _run(_archive_transition_handler(
        # Explicit --sha: Position A never falls back to a branch-tip guess,
        # so a scope-path miss (this fixture's handoff has no dedicated
        # commit under `scope:`) needs an override to resolve shipped_in at
        # all — unrelated to the live-holder mode-scoping this test pins.
        {"handoff_path": rel, "mode": "stamp_shipped", "sha": head_sha},
        repo.common_dir,
    ))

    assert result["exit_code"] == 0, result
    assert result.get("retain_kind") != "live-holder", (
        "mode='stamp_shipped' must never be retained on the live-holder "
        f"ground (scoped to mode='supersede' only); got {result!r}"
    )
