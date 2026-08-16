"""
coordinator_core.tests.test_hooks_bookkeeping — Round-trip contract tests for the 4
bookkeeping hook ops (pcore-08 C5).

Tests assert:
  - On-disk write side-effects (path + byte format) under a tmp .git/coordinator-sessions/ fixture.
  - Dedup idempotency: C1 (touched.txt) and C4 (dispatched-agents.txt).
  - 60-second mtime throttle no-op: C2 (session_heartbeat).
  - Single-line compact jsonl with no embedded newline: C3 (agent_completion_log).
  - Collision→AMBIGUOUS rewrite: C4 (track_dispatched_agents).
  - C4 branch matrix: 2 agent-id op-level shapes (bare hex, teammate canonical — the 3-pass
    manifest extraction collapses to these 2 at the op boundary), 4-source model cascade
    (via flat-scalar contract),
    <!-- Review: code-reviewer F7 — 3-pass extraction is manifest-side; op accepts 2 valid shapes -->
    collision vs. non-collision, dedup idempotency, golden byte-match of tab-delimited col-1.
  - Concurrent invocation safety (asyncio.gather) for C1 and C4 (D6 write-atomicity).
  - Golden-snapshot normalizer self-test (two captures → byte-identical normalized output);
    path normalizer covers BOTH POSIX and Windows .git/coordinator-sessions/ shapes.
  - Source-level grep: no bare blocking I/O sits unwrapped in the 4 async handler bodies.

All handlers are async; we use asyncio.run() in sync test functions — no pytest-asyncio.

Spec backlink: pln-pcore-08-async-bookkeeping-hoo-7920d5 § C5
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import types
import unittest.mock as mock
from pathlib import Path

import pytest

# Declared, not excused: a subset of this file's tests (the defect-A heartbeat
# self-heal path) spawn real git via `_git_init` because the property under test is
# the hook's own session-hub RESOLUTION against a real committed repo, which no mock
# stands in for. `_git_init` is called per-test, not hoisted, because those tests also
# mutate the fixture's `.git/coordinator-sessions/` tree under test.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class _FakeCtx:
    """Minimal ServiceContext stub exposing repo_root as a Path-like string."""

    def __init__(self, repo_root: str) -> None:
        self.repo_root = repo_root


def _cs_dir(repo_root: Path) -> Path:
    """Return the .git/coordinator-sessions/ directory under repo_root."""
    return repo_root / ".git" / "coordinator-sessions"


def _git_init(repo: Path) -> None:
    """Initialise a minimal committed git repo at repo (for tests needing a real
    session-hub resolution, e.g. the defect-A heartbeat self-heal)."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)


def _make_session(
    repo_root: Path,
    sid: str,
    *,
    create_meta: bool = False,
    meta_mtime_offset: float = 0.0,
) -> tuple[Path, Path]:
    """Create session dir + touched.txt; optionally write a meta.json.

    Args:
        meta_mtime_offset: seconds relative to now (negative = in the past).
            Only used when create_meta=True.

    Returns:
        (session_dir, touched_file)
    """
    session_dir = _cs_dir(repo_root) / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    touched = session_dir / "touched.txt"
    touched.touch()
    if create_meta:
        meta = {"session_id": sid, "last_activity": "2026-01-01T00:00:00Z"}
        meta_path = session_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, separators=(",", ":")))
        if meta_mtime_offset:
            ts = time.time() + meta_mtime_offset
            os.utime(str(meta_path), (ts, ts))
    return session_dir, touched


# ---------------------------------------------------------------------------
# Golden-snapshot normalizer
# ---------------------------------------------------------------------------

_ISO_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_EPOCH_INT_RE = re.compile(r"\b\d{10,11}\b")
_HEX_SHA_RE = re.compile(r"\b[0-9a-f]{40,64}\b")
_HEX_SID_RE = re.compile(r"\b[0-9a-f]{12,39}\b")
# POSIX path: anything ending with .git/coordinator-sessions/<rest>
_POSIX_CS_PATH_RE = re.compile(r"[^\s\"']+\.git/coordinator-sessions[^\s\"',\n]*")
# Windows path: backslash separators
_WIN_CS_PATH_RE = re.compile(r"[^\s\"']+\.git\\coordinator-sessions[^\s\"',\n]*")


def normalize_snapshot(text: str) -> str:
    """Normalize volatile fields in a captured log/file output for golden comparison.

    Replaces: ISO timestamps → <TS>, Unix epoch ints → <EPOCH>, long hex strings → <SHA>/<SID>,
    POSIX .git/coordinator-sessions paths → <CS-POSIX-PATH>,
    Windows .git\\coordinator-sessions paths → <CS-WIN-PATH>.

    Stable fields (relative file paths, literal strings, column separators) are
    preserved unchanged — the normalized form is the expected golden shape.
    """
    text = _ISO_TS_RE.sub("<TS>", text)
    text = _EPOCH_INT_RE.sub("<EPOCH>", text)
    text = _HEX_SHA_RE.sub("<SHA>", text)
    text = _HEX_SID_RE.sub("<SID>", text)
    text = _POSIX_CS_PATH_RE.sub("<CS-POSIX-PATH>", text)
    text = _WIN_CS_PATH_RE.sub("<CS-WIN-PATH>", text)
    return text


# ---------------------------------------------------------------------------
# Normalizer self-tests
# ---------------------------------------------------------------------------

class TestNormalizer:
    """Golden-snapshot normalizer must produce byte-identical output for two equivalent captures."""

    def test_two_captures_normalize_identically(self) -> None:
        """Two captures differing only in timestamp/epoch/session-id normalize identically."""
        # Use all-lowercase-hex agent ids so the hex normalizer matches both captures.
        capture_a = (
            '{"logged_at":"2026-07-04T12:00:00Z","agentId":"abc123def456abc1",'
            '"subagent_type":"executor"}\t1720000000\n'
        )
        capture_b = (
            '{"logged_at":"2026-07-04T15:30:00Z","agentId":"000111222333000a",'
            '"subagent_type":"executor"}\t1720005000\n'
        )
        assert normalize_snapshot(capture_a) == normalize_snapshot(capture_b)

    def test_posix_cs_path_normalized(self) -> None:
        """POSIX .git/coordinator-sessions/ paths are replaced with <CS-POSIX-PATH>."""
        text = "/repo/.git/coordinator-sessions/abc123/touched.txt"
        assert "<CS-POSIX-PATH>" in normalize_snapshot(text)
        assert "/repo/" not in normalize_snapshot(text)

    def test_windows_cs_path_normalized(self) -> None:
        r"""Windows .git\coordinator-sessions\ paths are replaced with <CS-WIN-PATH>."""
        text = r"C:\repo\.git\coordinator-sessions\abc123\touched.txt"
        assert "<CS-WIN-PATH>" in normalize_snapshot(text)
        assert r"C:\repo" not in normalize_snapshot(text)

    def test_stable_fields_preserved(self) -> None:
        """Stable string fields (e.g. subagent_type, literal keys) pass through unchanged."""
        text = '{"subagent_type":"executor","description":"test agent"}'
        n = normalize_snapshot(text)
        assert "executor" in n
        assert "description" in n
        assert "test agent" in n

    def test_tab_separators_preserved(self) -> None:
        """Tab delimiters in dispatched-agents.txt survive normalization."""
        # Review: code-reviewer F8 — test data updated to a pure lowercase hex string so that
        # _HEX_SID_RE matches (≥12 hex chars, no non-hex suffix like the original 'ghi').
        # The old data "abc123def456ghi" was not normalized because 'ghi' breaks hex-match.
        text = "abcdef1234567890\tclaude-sonnet-4-5\texecutor\t1720000000\n"
        n = normalize_snapshot(text)
        # Pin the complete normalized form — catches normalizers that drop or reorder columns.
        assert n == "<SID>\tclaude-sonnet-4-5\texecutor\t<EPOCH>\n"


# ---------------------------------------------------------------------------
# C1 — hooks.track_touched_files
# ---------------------------------------------------------------------------

class TestTrackTouchedFiles:
    """Round-trip and dedup tests for hooks.track_touched_files."""

    @pytest.fixture(autouse=True)
    def _isolate_em_session_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hermeticity: this suite's own process may itself be a dispatched
        session with CLAUDE_CODE_SESSION_ID set in its real environment. Piece 2
        (C7) reads that var, so every test in this class must start from a clean
        slate — individual tests opt back in via monkeypatch.setenv.
        """
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    def _ctx(self, tmp_path: Path) -> _FakeCtx:
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        return _FakeCtx(str(tmp_path / ".git"))

    def test_session_keyed_write(self, tmp_path: Path) -> None:
        """Handler appends a T-event for file_path to
        .git/coordinator-sessions/<sid>/touched.txt."""
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0011"
        _make_session(tmp_path, sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write", "file_path": "src/foo.py", "agent_id": ""},
            repo_root=ctx.repo_root,
        ))
        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        lines = [l for l in touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        assert any(verb == "T" and path == "src/foo.py" for verb, _ts, path in events)

    def test_dedup_idempotency_same_path_twice(self, tmp_path: Path) -> None:
        """Same file_path submitted twice → TWO T-events (dedup retired; append-only
        last-event-wins is the invariant now), and the projection still resolves the
        path as held.

        Inverted, not deleted, per docs/plans/2026-08-03-track-touched-files-emits-
        t-events.md § C2 — this test pins the retired-invariant REPLACEMENT, not the
        original whole-line dedup contract.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0022"
        _make_session(tmp_path, sid)
        params = {"session_id": sid, "tool_name": "Edit", "file_path": "lib/util.py", "agent_id": ""}
        _run(_handler(params, repo_root=ctx.repo_root))
        _run(_handler(params, repo_root=ctx.repo_root))
        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        lines = [l for l in touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        matches = [(verb, path) for verb, _ts, path in events if path == "lib/util.py"]
        assert len(matches) == 2, (
            f"Expected exactly TWO 'lib/util.py' T-events (append-only, dedup retired); got {lines}"
        )
        assert all(verb == "T" for verb, _path in matches)
        # The projection still resolves the path as held: the LAST event for
        # this path is 'T'.
        last_verb, last_path = matches[-1]
        assert last_verb == "T" and last_path == "lib/util.py"

    def test_meta_json_backfilled_when_dir_precreated_without_meta(self, tmp_path: Path) -> None:
        """Regression (defect A, 2026-07-24): meta.json is backfilled even when
        another bookkeeping writer created the session dir first (dir present,
        meta.json absent). Before the fix the handler short-circuited on dir
        existence, leaving meta.json — and Layer-1 liveness — permanently
        unwritten, so a killed session read live for the full 30-min Layer-2
        recency window and held /pickup claims hostage.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0033"
        # Simulate the race: dir + touched.txt exist (e.g. push-failure cursor
        # writer won), but NO meta.json.
        session_dir, _ = _make_session(tmp_path, sid, create_meta=False)
        assert not (session_dir / "meta.json").exists(), "precondition: no meta.json"

        _run(_handler(
            {"session_id": sid, "tool_name": "Edit", "file_path": "src/foo.py", "agent_id": ""},
            repo_root=ctx.repo_root,
        ))

        meta_path = session_dir / "meta.json"
        assert meta_path.is_file(), "meta.json must be backfilled on first edit"
        meta = json.loads(meta_path.read_text())
        assert meta["session_id"] == sid

    def test_needs_session_init_gate_goes_quiet_once_stable_pid_present(self, tmp_path: Path) -> None:
        """The bootstrap gate re-fires while meta.json is absent or unstamped,
        then goes quiet once a stable_pid is present — so the hot edit path pays
        no git-subprocess cost in steady state.
        """
        from coordinator_core.hooks.track_touched_files import _needs_session_init
        sid = "aabbccddeeff0044"
        session_dir = _cs_dir(tmp_path) / sid
        meta_path = session_dir / "meta.json"

        assert _needs_session_init(str(session_dir), str(meta_path)) is True  # dir absent
        session_dir.mkdir(parents=True, exist_ok=True)
        assert _needs_session_init(str(session_dir), str(meta_path)) is True  # meta absent
        meta_path.write_text(json.dumps({"session_id": sid}))
        assert _needs_session_init(str(session_dir), str(meta_path)) is True  # no stable_pid
        meta_path.write_text(json.dumps({"session_id": sid, "stable_pid": "1234"}))
        assert _needs_session_init(str(session_dir), str(meta_path)) is False  # steady state

    def test_agent_keyed_write(self, tmp_path: Path) -> None:
        """Subagent fire: writes a T-event to .agents/<canonical_id>/touched.txt as well."""
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0033"
        _make_session(tmp_path, sid)
        # Bare hex agent_id (≥12 chars) → canonical id is the agent_id unchanged
        agent_id = "deadbeef12340000"
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/ops/foo.py", "agent_id": agent_id},
            repo_root=ctx.repo_root,
        ))
        agent_touched = _cs_dir(tmp_path) / ".agents" / agent_id / "touched.txt"
        assert agent_touched.exists(), "Agent-keyed touched.txt must be created"
        lines = [l for l in agent_touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        assert any(
            verb == "T" and path == "coordinator_core/ops/foo.py" for verb, _ts, path in events
        )

    def test_agent_keyed_write_canonical_shape_rewritten_against_live_session(
        self, tmp_path: Path
    ) -> None:
        """/clear regression: a subagent-context fire carrying an already-
        canonical <name>@session-<short> agent_id (the harness's own stale-
        embedded-short shape, not the a<name>-16hex raw form) must key
        .agents/ by the LIVE session_id, not the harness's stale short — the
        same join key track_dispatched_agents now writes (see
        TestTrackDispatchedAgents.test_teammate_agent_id_short_rewritten_
        against_live_session), so a cross-writer join finds the same directory.

        Root cause: docs/research/spike-verdicts/2026-08-10-session-scoped-
        hooks-inside-a-teammate-session.md.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        ctx = self._ctx(tmp_path)
        live_sid = "f91c46a7bbbbbbbb"
        _make_session(tmp_path, live_sid)
        boot_short = "5ee0cb12"
        stale_agent_id = f"hookprobe-named@session-{boot_short}"
        _run(_handler(
            {"session_id": live_sid, "tool_name": "Write",
             "file_path": "coordinator_core/ops/foo.py", "agent_id": stale_agent_id},
            repo_root=ctx.repo_root,
        ))
        expected_dir = _cs_dir(tmp_path) / ".agents" / f"hookprobe-named@session-{live_sid[:8]}"
        agent_touched = expected_dir / "touched.txt"
        assert agent_touched.exists(), (
            f"Agent-keyed write must land under the LIVE-short directory {expected_dir}"
        )
        lines = [l for l in agent_touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        assert any(
            verb == "T" and path == "coordinator_core/ops/foo.py" for verb, _ts, path in events
        )
        stale_dir = _cs_dir(tmp_path) / ".agents" / stale_agent_id
        assert not stale_dir.exists(), "Must not also create a stale-short-keyed directory"

    def test_agent_absent_skips_agent_write(self, tmp_path: Path) -> None:
        """No agent_id → no .agents/ write; only session-keyed write fires."""
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0044"
        _make_session(tmp_path, sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write", "file_path": "docs/README.md"},
            repo_root=ctx.repo_root,
        ))
        agents_dir = _cs_dir(tmp_path) / ".agents"
        # No .agents/ entry should have been created
        if agents_dir.exists():
            assert list(agents_dir.iterdir()) == []

    def test_non_edit_tool_no_write(self, tmp_path: Path) -> None:
        """tool_name not in Write|Edit|MultiEdit|NotebookEdit → no write, returns {}."""
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0055"
        _make_session(tmp_path, sid)
        result = _run(_handler(
            {"session_id": sid, "tool_name": "Read", "file_path": "foo.py"},
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        assert touched.read_text() == ""

    def test_returns_no_advisory(self, tmp_path: Path) -> None:
        """Handler always returns {} (no advisory)."""
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0066"
        _make_session(tmp_path, sid)
        result = _run(_handler(
            {"session_id": sid, "tool_name": "Edit", "file_path": "x.py"},
            repo_root=ctx.repo_root,
        ))
        assert result == {}

    def test_path_normalized_relative_passthrough(self, tmp_path: Path) -> None:
        """Relative paths pass through normalization unchanged, in the written T-event."""
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0077"
        _make_session(tmp_path, sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/new.py"},
            repo_root=ctx.repo_root,
        ))
        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        lines = [l for l in touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        assert any(
            verb == "T" and path == "coordinator_core/hooks/new.py" for verb, _ts, path in events
        )

    def test_agent_dir_born_here_carries_an_owner(self, tmp_path: Path) -> None:
        """An agent dir created by a subagent's FIRST write must carry the
        em-session-id.txt back-pointer, not just touched.txt.

        Regression, 2026-08-03. This hook and track_dispatched_agents create the
        same .agents/<id>/ directory from opposite sides; only the latter wrote
        the owner record. A dir born here was therefore ownerless, and
        cs_compute_scope withholds every path an ownerless dir claims from ALL
        sessions for 30 minutes -- so an EM could not commit work its own
        subagent had done. Exercises the unnamed-agent path (bare-hex agent_id),
        which is the shape that produced all 343 ownerless dirs found on-disk.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0088"
        aid = "d00dfeed12345678"
        _make_session(tmp_path, sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/owned.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        agent_dir = _cs_dir(tmp_path) / ".agents" / aid
        assert (agent_dir / "touched.txt").read_text().strip() != "", (
            "precondition: the agent-keyed touch must have been recorded"
        )
        bp = agent_dir / "em-session-id.txt"
        assert bp.exists(), (
            "agent dir has recorded touches but no owner -- cs_compute_scope "
            "will withhold these paths from every session for 30 minutes"
        )
        assert bp.read_text().strip() == sid

    def test_back_pointer_never_overwrites_a_dispatch_record(
        self, tmp_path: Path
    ) -> None:
        """A real dispatch-time owner wins over this hook's write.

        The two writers race on the same file by design; this one must be the
        loser, since track_dispatched_agents sees the authoritative EM identity
        at dispatch. Pins the idempotent (non-empty file wins) semantics of the
        shared _write_backpointer_sync writer at THIS call site.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff0099"
        aid = "beefcafe87654321"
        _make_session(tmp_path, sid)
        agent_dir = _cs_dir(tmp_path) / ".agents" / aid
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "em-session-id.txt").write_text("dispatch-time-owner\n")
        _run(_handler(
            {"session_id": sid, "tool_name": "Edit",
             "file_path": "coordinator_core/hooks/owned.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        assert (agent_dir / "em-session-id.txt").read_text().strip() == (
            "dispatch-time-owner"
        )

    # -----------------------------------------------------------------
    # Piece 2 (C7) — Workflow-internal agent-spawn attribution via the
    # CLAUDE_CODE_SESSION_ID env var. Spec:
    # docs/plans/2026-08-03-scope-guard-peer-claim-release.md § C7
    # -----------------------------------------------------------------

    def test_workflow_internal_spawn_attributed_via_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Workflow-internal spawn's env-inherited CLAUDE_CODE_SESSION_ID
        (distinct from the firing session_id) is written as the back-pointer.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff00aa"
        em_sid = "1234567890abcdef"
        aid = "cafebabe12345678"
        _make_session(tmp_path, sid)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", em_sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/workflow_owned.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        bp = _cs_dir(tmp_path) / ".agents" / aid / "em-session-id.txt"
        assert bp.read_text().strip() == em_sid

    def test_env_equal_to_firing_session_not_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env == firing session_id -> that is this hook's own firing session,
        not a distinct dispatching parent; the guard must skip the env-derived
        write. Falls through to the session_id fallback, so the back-pointer is
        still written -- just with session_id, not misattributed via env.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff00bb"
        aid = "cafebabe23456789"
        _make_session(tmp_path, sid)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/self_owned.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        bp = _cs_dir(tmp_path) / ".agents" / aid / "em-session-id.txt"
        assert bp.read_text().strip() == sid

    def test_env_unset_not_written_via_piece2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No CLAUDE_CODE_SESSION_ID in the environment -> Piece 2 is a no-op;
        the pre-existing session_id fallback still fires.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff00cc"
        aid = "cafebabe34567890"
        _make_session(tmp_path, sid)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/env_absent.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        bp = _cs_dir(tmp_path) / ".agents" / aid / "em-session-id.txt"
        assert bp.read_text().strip() == sid

    def test_existing_back_pointer_not_overwritten_by_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real dispatch-time owner wins over the Piece 2 env-derived write too."""
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff00dd"
        em_sid = "fedcba0987654321"
        aid = "cafebabe45678901"
        _make_session(tmp_path, sid)
        agent_dir = _cs_dir(tmp_path) / ".agents" / aid
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "em-session-id.txt").write_text("dispatch-time-owner\n")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", em_sid)
        _run(_handler(
            {"session_id": sid, "tool_name": "Edit",
             "file_path": "coordinator_core/hooks/already_owned.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        assert (agent_dir / "em-session-id.txt").read_text().strip() == (
            "dispatch-time-owner"
        )

    def test_backpointer_oserror_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OSError from the env-derived back-pointer write is swallowed --
        the hook never raises, matching its fail-open bookkeeping contract.

        Real failure path, not mocked: em-session-id.txt pre-exists as an EMPTY
        DIRECTORY (so `_write_backpointer_sync`'s "already written" os.stat
        size-check doesn't short-circuit), so its temp+rename `os.replace` onto
        that path raises a real OSError (IsADirectoryError on POSIX) -- caught
        and swallowed inside `_write_backpointer_sync` itself, never propagating
        through this hook's `await asyncio.to_thread(...)` call site.
        """
        from coordinator_core.hooks.track_touched_files import _handler
        ctx = self._ctx(tmp_path)
        sid = "aabbccddeeff00ee"
        em_sid = "0011223344556677"
        aid = "cafebabe56789012"
        _make_session(tmp_path, sid)
        agent_dir = _cs_dir(tmp_path) / ".agents" / aid
        (agent_dir / "em-session-id.txt").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", em_sid)

        result = _run(_handler(
            {"session_id": sid, "tool_name": "Write",
             "file_path": "coordinator_core/hooks/boom.py", "agent_id": aid},
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        # Bookkeeping otherwise unaffected -- the touched-file write still lands.
        agent_touched = agent_dir / "touched.txt"
        assert "coordinator_core/hooks/boom.py" in agent_touched.read_text()


# ---------------------------------------------------------------------------
# C2 — hooks.session_heartbeat
# ---------------------------------------------------------------------------

class TestSessionHeartbeat:
    """Round-trip and throttle tests for hooks.session_heartbeat."""

    def test_absent_meta_json_no_last_activity_write(self, tmp_path: Path) -> None:
        """meta.json absent → mtime returns -1 → the last_activity write is
        skipped (update_last_activity NOT called). In this non-git fixture the
        defect-A bootstrap (core.init) also degrades to a no-op — core.init
        returns False when it cannot resolve a git session hub — so nothing is
        written; the real-git bootstrap path is covered by
        test_absent_meta_with_existing_dir_bootstraps below."""
        from coordinator_core.hooks.session_heartbeat import _handler
        import coordinator_core.hooks.session_heartbeat as hb_mod
        sid = "heartbeat0000001"
        _make_session(tmp_path, sid, create_meta=False)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        # Review: code-reviewer F4 — verify update_last_activity is NOT called;
        # result == {} alone does not catch a missing absent-file guard.
        with mock.patch.object(hb_mod, "update_last_activity") as mock_ula:
            result = _run(_handler({"session_id": sid}, repo_root=ctx.repo_root))
        assert result == {}
        mock_ula.assert_not_called()

    def test_absent_meta_with_existing_dir_bootstraps(self, tmp_path: Path) -> None:
        """Regression (defect A, 2026-07-24): when the session dir EXISTS but
        meta.json is absent (another writer won the create race), the heartbeat
        self-heals by bootstrapping meta.json via core.init — the earliest-firing
        heal, since Bash precedes most edits. Real git repo so core.init resolves
        the session hub."""
        from coordinator_core.hooks.session_heartbeat import _handler
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        sid = "heartbeat0000005"
        sdir = _cs_dir(repo) / sid
        sdir.mkdir(parents=True, exist_ok=True)  # dir present, meta.json absent
        assert not (sdir / "meta.json").exists(), "precondition: poisoned"
        result = _run(_handler({"session_id": sid}, repo_root=str(repo)))
        assert result == {}
        assert (sdir / "meta.json").is_file(), "heartbeat must backfill meta.json"
        assert json.loads((sdir / "meta.json").read_text())["session_id"] == sid

    def test_absent_meta_and_absent_dir_does_not_resurrect(self, tmp_path: Path) -> None:
        """A session dir that does NOT exist (archived/reaped or never created)
        must NOT be resurrected by the heartbeat — only an existing-dir poisoned
        state is healed."""
        from coordinator_core.hooks.session_heartbeat import _handler
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        sid = "heartbeat0000006"
        sdir = _cs_dir(repo) / sid
        assert not sdir.exists()
        result = _run(_handler({"session_id": sid}, repo_root=str(repo)))
        assert result == {}
        assert not sdir.exists(), "absent session dir must not be resurrected"

    def test_throttle_no_op_within_60s(self, tmp_path: Path) -> None:
        """meta.json mtime within 60 s → throttle fires → update_last_activity NOT called."""
        from coordinator_core.hooks.session_heartbeat import _handler
        import coordinator_core.hooks.session_heartbeat as hb_mod
        sid = "heartbeat0000002"
        # mtime_offset=0 → now → within 60 s throttle
        _make_session(tmp_path, sid, create_meta=True, meta_mtime_offset=0.0)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        with mock.patch.object(hb_mod, "update_last_activity") as mock_ula:
            result = _run(_handler({"session_id": sid}, repo_root=ctx.repo_root))
        assert result == {}
        mock_ula.assert_not_called()

    def test_write_fires_after_60s(self, tmp_path: Path) -> None:
        """meta.json mtime > 60 s ago → throttle cold → update_last_activity IS called."""
        from coordinator_core.hooks.session_heartbeat import _handler
        import coordinator_core.hooks.session_heartbeat as hb_mod
        sid = "heartbeat0000003"
        # mtime 61 s in the past → stale → write fires
        _make_session(tmp_path, sid, create_meta=True, meta_mtime_offset=-61.0)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        captured_args: list = []
        def _capture_ula(session_dir: str, iso: str) -> None:
            captured_args.append((session_dir, iso))
        with mock.patch.object(hb_mod, "update_last_activity", side_effect=_capture_ula):
            result = _run(_handler({"session_id": sid}, repo_root=ctx.repo_root))
        assert result == {}
        assert len(captured_args) == 1, "update_last_activity must be called exactly once"
        called_session_dir, called_iso = captured_args[0]
        assert sid in called_session_dir, "session_dir arg must contain session_id"
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", called_iso), (
            f"iso arg must be ISO-8601 UTC: {called_iso!r}"
        )

    def test_missing_session_id_returns_no_advisory(self, tmp_path: Path) -> None:
        """session_id absent → returns {} immediately (nothing to stamp)."""
        from coordinator_core.hooks.session_heartbeat import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        result = _run(_handler({}, repo_root=ctx.repo_root))
        assert result == {}

    def test_always_returns_no_advisory(self, tmp_path: Path) -> None:
        """Handler always returns {} regardless of throttle path taken."""
        from coordinator_core.hooks.session_heartbeat import _handler
        import coordinator_core.hooks.session_heartbeat as hb_mod
        sid = "heartbeat0000004"
        _make_session(tmp_path, sid, create_meta=True, meta_mtime_offset=-61.0)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        with mock.patch.object(hb_mod, "update_last_activity"):
            result = _run(_handler({"session_id": sid}, repo_root=ctx.repo_root))
        assert result == {}


# ---------------------------------------------------------------------------
# C3 — hooks.agent_completion_log
# ---------------------------------------------------------------------------

class TestAgentCompletionLog:
    """Round-trip tests for hooks.agent_completion_log."""

    def _log_path(self, tmp_path: Path) -> Path:
        return _cs_dir(tmp_path) / "logs" / "agent-audit.jsonl"

    def test_appends_compact_json_line(self, tmp_path: Path) -> None:
        """Handler appends exactly one compact JSON line to logs/agent-audit.jsonl."""
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        result = _run(_handler(
            {
                "description": "test executor agent",
                "subagent_type": "executor",
                "dispatched_agent_id": "abc123def456ghi7",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        log = self._log_path(tmp_path)
        assert log.exists(), "agent-audit.jsonl must be created"
        lines = log.read_text().splitlines()
        assert len(lines) == 1, f"Expected 1 line; got {len(lines)}"

    def test_single_line_no_embedded_newline(self, tmp_path: Path) -> None:
        """The appended record must be a single compact JSON line with no embedded newline."""
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _run(_handler(
            {
                "description": "line\nbreak\ntest",
                "subagent_type": "general-purpose",
                "dispatched_agent_id": "deadbeef12345678",
            },
            repo_root=ctx.repo_root,
        ))
        log = self._log_path(tmp_path)
        raw = log.read_bytes()
        # Exactly one newline (the terminating newline) — no embedded newlines in the JSON
        assert raw.count(b"\n") == 1, (
            f"Expected exactly 1 newline (line terminator); raw bytes: {raw!r}"
        )

    def test_compact_json_format(self, tmp_path: Path) -> None:
        """The line must be valid compact JSON (no pretty-print whitespace) with required keys."""
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        aid = "feedbabe00001111"
        _run(_handler(
            {
                "description": "format check",
                "subagent_type": "executor",
                "name": "my-worker",
                "dispatched_agent_id": aid,
            },
            repo_root=ctx.repo_root,
        ))
        log = self._log_path(tmp_path)
        line = log.read_text().strip()
        record = json.loads(line)  # must parse as JSON
        assert "logged_at" in record
        assert "description" in record
        assert "subagent_type" in record
        assert "agentId" in record  # camelCase — mirrors jq output + runtime-tripwire grep
        assert record["agentId"] == aid
        # Review: code-reviewer F6 — "  " doesn't catch default-sep single-space; check colon-space.
        # Compact JSON uses separators=(",",":"); default json.dumps produces "key": "value"
        # (colon-space) — that is NOT compact even though it has no double-space.
        assert ": " not in line, (
            f"Line contains colon-space — not compact JSON "
            f"(separators=(',',':') missing?): {line!r}"
        )

    def test_no_tmp_write_path(self, tmp_path: Path) -> None:
        """The op MUST NOT write to /tmp or any path outside .git/coordinator-sessions/."""
        from coordinator_core.hooks.agent_completion_log import _handler
        # Review: code-reviewer F11 — import module object first; avoids __import__ in wraps=.
        import coordinator_core.hooks.agent_completion_log as acl_mod
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        # We verify by checking that no /tmp file was created during the call.
        # The op is always repo-keyed; /tmp fallback is explicitly dropped (C3).
        with mock.patch("coordinator_core.hooks.agent_completion_log._append_audit_entry",
                        wraps=acl_mod._append_audit_entry) as spy:
            _run(_handler({"description": "x", "subagent_type": "executor"}, repo_root=ctx.repo_root))
        # All calls must have the first arg (log_file) containing coordinator-sessions
        for call in spy.call_args_list:
            log_file_arg = call.args[0]
            assert "coordinator-sessions" in log_file_arg or \
                   "coordinator-sessions" in log_file_arg.replace("\\", "/"), (
                f"log_file arg must be under .git/coordinator-sessions/; got {log_file_arg!r}"
            )

    def test_defaults_fill_absent_fields(self, tmp_path: Path) -> None:
        """Absent description/subagent_type fall back to 'unknown'/'general-purpose'."""
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _run(_handler({}, repo_root=ctx.repo_root))
        log = self._log_path(tmp_path)
        record = json.loads(log.read_text().strip())
        assert record["description"] == "unknown"
        assert record["subagent_type"] == "general-purpose"
        assert record["agentId"] is None
        assert record["name"] is None

    def test_multiple_appends_produce_multiple_lines(self, tmp_path: Path) -> None:
        """Two handler calls → two lines in agent-audit.jsonl."""
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        _run(_handler({"description": "first", "subagent_type": "executor"}, repo_root=ctx.repo_root))
        _run(_handler({"description": "second", "subagent_type": "code-reviewer"}, repo_root=ctx.repo_root))
        log = self._log_path(tmp_path)
        lines = [l for l in log.read_text().splitlines() if l]
        assert len(lines) == 2

    def test_snake_fallback_agent_id_logged(self, tmp_path: Path) -> None:
        """F2: dispatched_agent_id absent, dispatched_agent_id_snake present → snake value logged as agentId.

        Named-teammate dispatch returns carry snake_case agent_id; without this fallback,
        completions log a null id. Manifest f4f150a1d advertises dispatched_agent_id_snake
        as a second input; this test asserts the op consumes it.
        """
        from coordinator_core.hooks.agent_completion_log import _handler
        ctx = _FakeCtx(str(tmp_path / ".git"))
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        snake_id = "deadbeef99887766"
        _run(_handler(
            {
                "description": "named-teammate completion",
                "subagent_type": "executor",
                # dispatched_agent_id intentionally absent — only snake variant present
                "dispatched_agent_id_snake": snake_id,
            },
            repo_root=ctx.repo_root,
        ))
        log = self._log_path(tmp_path)
        record = json.loads(log.read_text().strip())
        assert record["agentId"] == snake_id, (
            f"Snake fallback not consumed: expected agentId={snake_id!r}, got {record['agentId']!r}"
        )


# ---------------------------------------------------------------------------
# C4 — hooks.track_dispatched_agents
# ---------------------------------------------------------------------------

class TestTrackDispatchedAgents:
    """Branch-matrix round-trip tests for hooks.track_dispatched_agents."""

    # Valid hex agent-id: ≥12 lowercase hex chars
    _HEX_AID = "abcdef1234567890"
    # Valid teammate canonical id: <name>@session-<short>
    _TEAMMATE_AID = "the Director of Engineering@session-abcd1234"

    def _ctx(self, tmp_path: Path) -> _FakeCtx:
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        return _FakeCtx(str(tmp_path / ".git"))

    def _dispatched(self, tmp_path: Path, sid: str) -> Path:
        return _cs_dir(tmp_path) / sid / "dispatched-agents.txt"

    # --- (a) Agent-id shape: bare hex (agentId pass) ---

    def test_hex_agent_id_accepted(self, tmp_path: Path) -> None:
        """Bare hex agent_id (≥12 chars) is accepted and written to dispatched-agents.txt."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000001"
        result = _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        disp = self._dispatched(tmp_path, sid)
        assert disp.exists()
        lines = [l for l in disp.read_text().splitlines() if l]
        assert any(l.split("\t")[0] == self._HEX_AID for l in lines), (
            f"Expected hex agent_id in col-1; lines={lines!r}"
        )

    # --- (a2) Agent-id shape: teammate canonical id (agent_id / regex pass) ---

    def test_teammate_agent_id_accepted(self, tmp_path: Path) -> None:
        """Teammate canonical id (<name>@session-<short>) is accepted, and its
        embedded short is rewritten against the LIVE session_id (normalize_
        teammate_agent_id) rather than recorded verbatim — see
        test_teammate_agent_id_short_rewritten_against_live_session below for
        the dedicated /clear regression this guards."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000002"
        result = _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._TEAMMATE_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "code-reviewer"},
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        expected_id = f"the Director of Engineering@session-{sid[:8]}"
        assert any(l.split("\t")[0] == expected_id for l in lines), (
            f"Expected rewritten id {expected_id!r} in col-1; lines={lines!r}"
        )
        assert not any(l.split("\t")[0] == self._TEAMMATE_AID for l in lines), (
            "Harness-stale short must not be recorded verbatim"
        )

    def test_teammate_agent_id_short_rewritten_against_live_session(self, tmp_path: Path) -> None:
        """/clear regression: a teammate's harness-embedded <short> is stamped once
        at team creation and never refreshed. Dispatching that SAME teammate again
        under a NEW live session_id (post-/clear) must key the row — and therefore
        the .agents/<id>/ directory every other bookkeeping writer joins against —
        by the LIVE short, not the stale one the harness keeps handing back.

        Root cause: docs/research/spike-verdicts/2026-08-10-session-scoped-hooks-
        inside-a-teammate-session.md.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        boot_sid = "5ee0cb12aaaaaaaa"
        live_sid = "f91c46a7bbbbbbbb"
        harness_agent_id = f"hookprobe-named@session-{boot_sid[:8]}"

        # Dispatch fires under the NEW live session — as it does post-/clear —
        # but the harness still hands back the id it minted at team creation.
        _run(_handler(
            {"session_id": live_sid, "dispatched_agent_id": harness_agent_id,
             "dispatched_model": "claude-opus-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))

        disp = self._dispatched(tmp_path, live_sid)
        assert disp.exists(), "row must land in the LIVE session's dispatched-agents.txt"
        lines = [l for l in disp.read_text().splitlines() if l]
        expected_id = f"hookprobe-named@session-{live_sid[:8]}"
        assert any(l.split("\t")[0] == expected_id for l in lines), (
            f"Expected LIVE-short-keyed id {expected_id!r}; lines={lines!r}"
        )
        assert not any(harness_agent_id in l for l in lines), (
            "Harness's stale boot-session short must not survive into the record"
        )

    # --- Agent-id shape: invalid → rejected ---

    def test_invalid_agent_id_rejected(self, tmp_path: Path) -> None:
        """Invalid agent_id format → op exits early, no write."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000003"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": "INVALID-ID",
             "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        # Session dir may or may not be created; dispatched-agents.txt must not have the id
        disp = self._dispatched(tmp_path, sid)
        if disp.exists():
            assert "INVALID-ID" not in disp.read_text()

    # --- (b) Model cascade: dispatched_model present → used as column 2 ---

    def test_model_present_used_as_column2(self, tmp_path: Path) -> None:
        """dispatched_model present → column 2 of dispatched-agents.txt is that model string."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000004"
        model = "claude-sonnet-4-5"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": model, "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        assert len(lines) == 1
        cols = lines[0].split("\t")
        assert cols[1] == model, f"Expected model {model!r} in col-2; cols={cols!r}"

    def test_model_absent_falls_back_to_unknown(self, tmp_path: Path) -> None:
        """dispatched_model absent ('' per _payload contract) → column 2 is 'unknown'."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000005"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        assert lines[0].split("\t")[1] == "unknown"

    def test_model_key_absent_falls_back_to_unknown(self, tmp_path: Path) -> None:
        """dispatched_model key entirely absent from params dict → column 2 is 'unknown'.

        Distinct from the empty-string case: the field() helper normalizes key-absent
        to "" (same as empty-string per the flat-scalar contract — an unforwarded or
        unresolvable field arrives as ""), so both absence and empty-string resolve to
        "unknown". This test documents that the key-absent contract matches empty-string.

        Review: code-reviewer F2 (P1) — 4-cascade branch matrix gap: key-absent was not
        separately tested from empty-string. The op uses field(params, key) which calls
        params.get(key, "") — key-absent == "" == "unknown" fallback.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses000000KEY0005"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "subagent_type": "executor"},  # dispatched_model key absent entirely
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        assert len(lines) == 1
        assert lines[0].split("\t")[1] == "unknown", (
            f"Key-absent dispatched_model must fall back to 'unknown'; "
            f"cols={lines[0].split(chr(9))!r}"
        )

    # --- (c) Collision → AMBIGUOUS ---

    def test_collision_different_subagent_type_marks_ambiguous(self, tmp_path: Path) -> None:
        """Same agent_id + different subagent_type → existing row col-3 marked AMBIGUOUS."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000006"
        # First dispatch: executor
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        # Second dispatch: same agent_id, different subagent_type → collision
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "code-reviewer"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        content = disp.read_text()
        assert "AMBIGUOUS" in content, (
            f"Expected AMBIGUOUS in dispatched-agents.txt after collision; content={content!r}"
        )

    # --- (c2) Two-phase write: "unknown" is a placeholder, not a collision ---

    def _row(self, tmp_path: Path, sid: str) -> list[str]:
        lines = [l for l in self._dispatched(tmp_path, sid).read_text().splitlines() if l]
        assert len(lines) == 1, f"Expected exactly 1 row; got {len(lines)}: {lines!r}"
        return lines[0].split("\t")

    def test_two_phase_placeholder_enriched_not_marked_ambiguous(self, tmp_path: Path) -> None:
        """Identity-only create then a typed enrich resolves the row in place.

        A caller that knows the agent_id before it knows the type (SubagentStart supplies
        neither model nor subagent_type) lands a placeholder row; the later typed call must
        fill it in rather than stamp the collision sentinel. Stamping AMBIGUOUS here would
        disarm the four bash guards that read it, on every dispatch.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000021"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID},
            repo_root=ctx.repo_root,
        ))
        assert self._row(tmp_path, sid)[2] == "unknown", "create should land a placeholder type"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "coordinator:executor"},
            repo_root=ctx.repo_root,
        ))
        cols = self._row(tmp_path, sid)
        assert cols[2] == "coordinator:executor", f"type not enriched; row={cols!r}"
        assert cols[1] == "claude-opus-4-5", (
            f"model not enriched; a permanently-'unknown' col-2 silently undercounts opus "
            f"in coordinator-session-loe and drops the opus runtime tripwire; row={cols!r}"
        )

    def test_two_phase_enrich_preserves_dispatched_at(self, tmp_path: Path) -> None:
        """Enrichment keeps column 4 from the create call, which is closer to the true
        dispatch moment than the enriching call — the runtime tripwire measures against it."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000022"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID},
            repo_root=ctx.repo_root,
        ))
        created_at = self._row(tmp_path, sid)[3]
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "coordinator:executor"},
            repo_root=ctx.repo_root,
        ))
        assert self._row(tmp_path, sid)[3] == created_at

    def test_late_placeholder_does_not_downgrade_resolved_row(self, tmp_path: Path) -> None:
        """A placeholder arriving AFTER a resolved row is a no-op, never a downgrade.

        The two calls race on a machine running dozens of concurrent sessions, so arrival
        order is not guaranteed to match dispatch order.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000023"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "coordinator:executor"},
            repo_root=ctx.repo_root,
        ))
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID},
            repo_root=ctx.repo_root,
        ))
        cols = self._row(tmp_path, sid)
        assert cols[2] == "coordinator:executor", f"resolved type was downgraded; row={cols!r}"
        assert cols[1] == "claude-opus-4-5", f"resolved model was downgraded; row={cols!r}"

    def test_collision_after_enrich_still_marks_ambiguous(self, tmp_path: Path) -> None:
        """The guards stay armed downstream of an enrichment: once a row carries a REAL
        type, a second real differing type is still a genuine collision."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000024"
        for params in (
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID},
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "coordinator:executor"},
            {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
             "dispatched_model": "claude-opus-4-5", "subagent_type": "coordinator:code-reviewer"},
        ):
            _run(_handler(params, repo_root=ctx.repo_root))
        assert self._row(tmp_path, sid)[2] == "AMBIGUOUS"

    def test_legacy_short_record_is_not_a_placeholder(self) -> None:
        """A legacy short record carries "" in column 3, which is NOT the placeholder
        sentinel — it stays on the collision arm, and padding does not grow a trailing
        empty column it never had."""
        from coordinator_core.hooks.track_dispatched_agents import _resolve_row_collision
        cols = _resolve_row_collision([self._HEX_AID], "claude-opus-4-5", "coordinator:executor")
        assert cols == [self._HEX_AID, "", "AMBIGUOUS"]

    def test_both_write_arms_agree_on_the_branch_table(self) -> None:
        """The POSIX (locked_rmw) and non-POSIX arms must not drift apart on dedup /
        enrich / collision — they are mirrors of one table, and only one of them runs
        on any given platform."""
        import tempfile
        from coordinator_core.hooks.track_dispatched_agents import (
            _make_dispatch_mutate, _process_dispatched_sync,
        )
        aid = self._HEX_AID
        sequences = [
            [("unknown", "unknown"), ("claude-opus-4-5", "coordinator:executor")],
            [("claude-opus-4-5", "coordinator:executor"), ("unknown", "unknown")],
            [("claude-opus-4-5", "coordinator:reviewer"), ("claude-opus-4-5", "coordinator:executor")],
            [("claude-opus-4-5", "coordinator:executor"), ("claude-opus-4-5", "coordinator:executor")],
        ]
        for seq in sequences:
            text = ""
            for model, stype in seq:
                text = _make_dispatch_mutate(aid, model, stype)(text)
            path = Path(tempfile.mkdtemp()) / "dispatched-agents.txt"
            for model, stype in seq:
                _process_dispatched_sync(str(path), aid, model, stype)
            mirrored = path.read_text(encoding="utf-8")
            # Column 4 is a write-time epoch; compare the identity columns only.
            assert [l.split("\t")[:3] for l in text.splitlines()] == \
                   [l.split("\t")[:3] for l in mirrored.splitlines()], (
                f"Write arms disagree on {seq!r}: locked={text!r} sync={mirrored!r}"
            )

    # --- (c) Non-collision append ---

    def test_non_collision_different_agents_appended(self, tmp_path: Path) -> None:
        """Two distinct agent_ids → both appended, each as a separate row."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000007"
        aid1 = "aabbccddeeff0011"
        aid2 = "bbccddee00112233"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": aid1,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": aid2,
             "dispatched_model": "claude-haiku-4-5", "subagent_type": "code-reviewer"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        assert len(lines) == 2, f"Expected 2 rows; got {len(lines)}: {lines!r}"

    # --- (d) Dedup idempotency ---

    def test_dedup_same_agent_same_type_idempotent(self, tmp_path: Path) -> None:
        """Same agent_id + same subagent_type submitted twice → exactly one row."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000008"
        params = {
            "session_id": sid, "dispatched_agent_id": self._HEX_AID,
            "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor",
        }
        _run(_handler(params, repo_root=ctx.repo_root))
        _run(_handler(params, repo_root=ctx.repo_root))
        disp = self._dispatched(tmp_path, sid)
        lines = [l for l in disp.read_text().splitlines() if l]
        assert len(lines) == 1, f"Dedup failed: {lines!r}"

    # --- (e) Golden byte-match of tab-delimited col-1 format ---

    def test_golden_col1_format(self, tmp_path: Path) -> None:
        """Column-1 (agent_id) must be the exact agent_id string.

        A runtime-tripwire consumer greps column-1 of dispatched-agents.txt.
        This test pins the byte-exact col-1 format to match that grep expectation.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000009"
        aid = "cafebabe00001234"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": aid,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        raw = disp.read_text()
        lines = [l for l in raw.splitlines() if l]
        assert len(lines) == 1
        cols = lines[0].split("\t")
        # Column-1: must be exactly the agent_id
        assert cols[0] == aid, (
            f"Golden col-1 mismatch: expected {aid!r}, got {cols[0]!r}"
        )
        # Must have 4 columns: agentId, model, subagent_type, epoch
        assert len(cols) == 4, f"Expected 4 tab-separated columns; cols={cols!r}"
        # Column-4 must be a Unix epoch (digits only)
        assert cols[3].isdigit(), f"Column-4 must be Unix epoch digits; got {cols[3]!r}"
        # Review: code-reviewer F1 (P2) — previous disjunction was tautological: "<SID>" in
        # normalized is always True for a 16-char hex id, so the disjunction always passes.
        # Replaced with two unconditional assertions + a concrete golden template equality.
        normalized = normalize_snapshot(raw)
        assert aid not in normalized, (
            f"Literal agent_id must be replaced by normalizer; found {aid!r} in: {normalized!r}"
        )
        assert "<SID>" in normalized, (
            f"Normalizer must replace agent_id with <SID> sentinel; got: {normalized!r}"
        )
        assert normalize_snapshot(lines[0]) == "<SID>\tclaude-sonnet-4-5\texecutor\t<EPOCH>", (
            f"Golden template mismatch; normalized line: {normalize_snapshot(lines[0])!r}"
        )

    def test_back_pointer_written(self, tmp_path: Path) -> None:
        """em-session-id.txt back-pointer written under .agents/<agent_id>/."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000010"
        aid = "cafe000012345678"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": aid,
             "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
            repo_root=ctx.repo_root,
        ))
        bp = _cs_dir(tmp_path) / ".agents" / aid / "em-session-id.txt"
        assert bp.exists(), "em-session-id.txt back-pointer must be created"
        content = bp.read_text().strip()
        assert content == sid, f"Back-pointer must contain session_id; got {content!r}"

    def test_tab_delimited_row_structure(self, tmp_path: Path) -> None:
        """The written row must be tab-delimited with newline terminator — exact wire format."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses0000000000011"
        aid = "0011223344556677"
        _run(_handler(
            {"session_id": sid, "dispatched_agent_id": aid,
             "dispatched_model": "claude-haiku-4-5", "subagent_type": "general-purpose"},
            repo_root=ctx.repo_root,
        ))
        disp = self._dispatched(tmp_path, sid)
        raw = disp.read_bytes()
        assert b"\t" in raw, "Row must use tab as column separator"
        assert raw.endswith(b"\n"), "Row must be newline-terminated"

    def test_snake_fallback_agent_id_recorded_in_col1(self, tmp_path: Path) -> None:
        """F2: dispatched_agent_id absent, dispatched_agent_id_snake present → snake value in col-1.

        Named-teammate dispatch returns carry snake_case agent_id; without this fallback,
        completions exit early (agent_id == "" → no_advisory) and the dispatch goes untracked.
        Manifest f4f150a1d advertises dispatched_agent_id_snake as a second input; this test
        asserts the op consumes it and records the value in column-1 of dispatched-agents.txt.
        """
        from coordinator_core.hooks.track_dispatched_agents import _handler
        ctx = self._ctx(tmp_path)
        sid = "ses000SNAKE00012"
        snake_id = "aabbcc9988776655"
        result = _run(_handler(
            {
                "session_id": sid,
                # dispatched_agent_id intentionally absent — only snake variant present
                "dispatched_agent_id_snake": snake_id,
                "dispatched_model": "claude-sonnet-4-5",
                "subagent_type": "executor",
            },
            repo_root=ctx.repo_root,
        ))
        assert result == {}
        disp = self._dispatched(tmp_path, sid)
        assert disp.exists(), "dispatched-agents.txt must be created when snake fallback fires"
        lines = [l for l in disp.read_text().splitlines() if l]
        assert len(lines) == 1, f"Expected 1 row; got {len(lines)}: {lines!r}"
        assert lines[0].split("\t")[0] == snake_id, (
            f"Snake fallback not recorded in col-1: expected {snake_id!r}, "
            f"got {lines[0].split(chr(9))[0]!r}"
        )

    # --- AC6/AC7: LockTimeout vs MutateAbort — the drop-visibility split ---

    def test_lock_timeout_produces_drop_signal_not_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """LockTimeout: a lost write leaves a greppable stderr signal naming the agent id and
        the dropped file, does NOT call _process_dispatched_sync (no unserialised fallback
        write while a peer process holds the lock), and does not raise into the hook's caller.
        """
        import coordinator_core.hooks.track_dispatched_agents as tda_mod
        from coordinator_core.locked_write import LockTimeout

        ctx = self._ctx(tmp_path)
        sid = "ses000LOCKTIME001"
        with mock.patch.object(tda_mod, "locked_rmw", side_effect=LockTimeout("timed out")), \
                mock.patch.object(tda_mod, "_process_dispatched_sync") as mock_sync:
            result = _run(tda_mod._handler(
                {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
                 "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
                repo_root=ctx.repo_root,
            ))
        assert result == {}, "hook must not raise into the caller on LockTimeout"
        mock_sync.assert_not_called()
        captured = capsys.readouterr()
        assert self._HEX_AID in captured.err, (
            f"drop signal must name the agent id; stderr={captured.err!r}"
        )
        assert "LockTimeout" in captured.err or "dropped" in captured.err, (
            f"drop signal must be greppable for the drop; stderr={captured.err!r}"
        )
        disp = self._dispatched(tmp_path, sid)
        assert str(disp) in captured.err, (
            f"drop signal must name the file that was not written; stderr={captured.err!r}"
        )

    def test_mutate_abort_stays_silent_no_fallback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """MutateAbort is a clean abort — nothing needed writing. It stays silent (no stderr
        signal) and, like LockTimeout, does NOT call _process_dispatched_sync. Pins the split
        itself, not just the LockTimeout arm.
        """
        import coordinator_core.hooks.track_dispatched_agents as tda_mod
        from coordinator_core.locked_write import MutateAbort

        ctx = self._ctx(tmp_path)
        sid = "ses000MUTATEABT01"
        with mock.patch.object(tda_mod, "locked_rmw", side_effect=MutateAbort("declined")), \
                mock.patch.object(tda_mod, "_process_dispatched_sync") as mock_sync:
            result = _run(tda_mod._handler(
                {"session_id": sid, "dispatched_agent_id": self._HEX_AID,
                 "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
                repo_root=ctx.repo_root,
            ))
        assert result == {}, "hook must not raise into the caller on MutateAbort"
        mock_sync.assert_not_called()
        captured = capsys.readouterr()
        assert captured.err == "", f"MutateAbort must stay silent; stderr={captured.err!r}"


# ---------------------------------------------------------------------------
# CONCURRENCY — D6 write-atomicity
# ---------------------------------------------------------------------------

class TestConcurrency:
    """Concurrent asyncio.gather invocations must not lose or corrupt entries (D6)."""

    def test_c1_concurrent_dedup_no_lost_entry(self, tmp_path: Path) -> None:
        """Two concurrent track_touched_files calls on the same touched.txt — no lost entries."""
        from coordinator_core.hooks.track_touched_files import _handler, _FILE_LOCKS
        from coordinator_core.session.scope import parse_touch_event
        # Review: code-reviewer F10 (nit) — make structural check explicit rather than relying
        # on ImportError; if _FILE_LOCKS is renamed, this gives a clear assertion failure.
        assert isinstance(_FILE_LOCKS, dict), "_FILE_LOCKS lock registry must be a dict (D6 write-atomicity)"
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "concurrency00001"
        _make_session(tmp_path, sid)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        touched = _cs_dir(tmp_path) / sid / "touched.txt"

        async def _concurrent():
            path_a = "src/module_a.py"
            path_b = "src/module_b.py"
            await asyncio.gather(
                _handler({"session_id": sid, "tool_name": "Write",
                          "file_path": path_a}, repo_root=ctx.repo_root),
                _handler({"session_id": sid, "tool_name": "Write",
                          "file_path": path_b}, repo_root=ctx.repo_root),
            )

        asyncio.run(_concurrent())
        lines = [l for l in touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        paths = [path for verb, _ts, path in events if verb == "T"]
        # Both entries must appear
        assert "src/module_a.py" in paths, f"path_a missing; lines={lines!r}"
        assert "src/module_b.py" in paths, f"path_b missing; lines={lines!r}"

    def test_c1_concurrent_same_path_no_duplicate(self, tmp_path: Path) -> None:
        """Two concurrent calls with the SAME path → TWO T-events, both intact
        (dedup retired — append-only last-event-wins; D6 still guarantees no
        torn/corrupted write, not a single-entry collapse)."""
        from coordinator_core.hooks.track_touched_files import _handler
        from coordinator_core.session.scope import parse_touch_event
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "concurrency00002"
        _make_session(tmp_path, sid)
        ctx = _FakeCtx(str(tmp_path / ".git"))
        touched = _cs_dir(tmp_path) / sid / "touched.txt"

        async def _concurrent():
            path = "src/shared_module.py"
            await asyncio.gather(
                _handler({"session_id": sid, "tool_name": "Write",
                          "file_path": path}, repo_root=ctx.repo_root),
                _handler({"session_id": sid, "tool_name": "Write",
                          "file_path": path}, repo_root=ctx.repo_root),
            )

        asyncio.run(_concurrent())
        lines = [l for l in touched.read_text().splitlines() if l]
        events = [parse_touch_event(l) for l in lines]
        matches = [(verb, path) for verb, _ts, path in events if path == "src/shared_module.py"]
        assert len(matches) == 2, (
            f"Expected TWO T-events for src/shared_module.py (append-only, dedup retired) — "
            f"no lost/corrupted entry; lines={lines!r}"
        )
        assert all(verb == "T" for verb, _path in matches)

    def test_c4_concurrent_no_lost_entry(self, tmp_path: Path) -> None:
        """Two concurrent track_dispatched_agents calls on the same file — both entries present."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "concurrency00003"
        ctx = _FakeCtx(str(tmp_path / ".git"))
        aid1 = "aabbccddeeff1100"
        aid2 = "aabbccddeeff2200"

        async def _concurrent():
            await asyncio.gather(
                _handler({"session_id": sid, "dispatched_agent_id": aid1,
                          "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"},
                         repo_root=ctx.repo_root),
                _handler({"session_id": sid, "dispatched_agent_id": aid2,
                          "dispatched_model": "claude-haiku-4-5", "subagent_type": "code-reviewer"},
                         repo_root=ctx.repo_root),
            )

        asyncio.run(_concurrent())
        disp = _cs_dir(tmp_path) / sid / "dispatched-agents.txt"
        lines = [l for l in disp.read_text().splitlines() if l]
        col1s = [l.split("\t")[0] for l in lines]
        assert aid1 in col1s, f"aid1 missing from concurrent write; col1s={col1s!r}"
        assert aid2 in col1s, f"aid2 missing from concurrent write; col1s={col1s!r}"

    def test_c4_concurrent_collision_rewrite_no_corruption(self, tmp_path: Path) -> None:
        """Two concurrent calls with the SAME agent_id + different types → AMBIGUOUS (no corruption)."""
        from coordinator_core.hooks.track_dispatched_agents import _handler
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "concurrency00004"
        ctx = _FakeCtx(str(tmp_path / ".git"))
        aid = "ccddee0011223344"

        async def _concurrent():
            await asyncio.gather(
                _handler({"session_id": sid, "dispatched_agent_id": aid,
                          "dispatched_model": "m", "subagent_type": "executor"},
                         repo_root=ctx.repo_root),
                _handler({"session_id": sid, "dispatched_agent_id": aid,
                          "dispatched_model": "m", "subagent_type": "code-reviewer"},
                         repo_root=ctx.repo_root),
            )

        asyncio.run(_concurrent())
        disp = _cs_dir(tmp_path) / sid / "dispatched-agents.txt"
        content = disp.read_text()
        lines = [l for l in content.splitlines() if l]
        # Review: code-reviewer F5 (P2) — the row-loop accepted any rows with correct col-1,
        # which passes even if BOTH coroutines wrote independent rows (2-row corruption).
        # The correct assertion: either dedup won (1 row) OR collision was detected (AMBIGUOUS).
        # A 2-row outcome without AMBIGUOUS IS the race-condition corruption this test catches.
        assert len(lines) == 1 or "AMBIGUOUS" in content, (
            f"Corruption detected: {len(lines)} rows with no AMBIGUOUS marker — "
            f"concurrent dedup/collision guard failed; content={content!r}"
        )
        if len(lines) == 1:
            assert len(lines[0].split("\t")) == 4, (
                f"Single dedup-winner row must have 4 tab-separated columns; got {lines[0]!r}"
            )

class TestFileLockHeldAwareEviction:
    """C9: _get_lock's eviction (stale sweep + hard-cap) must never evict a HELD lock.

    docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C9:
    the prior FIFO/held-unaware eviction could pop a lock a peer dispatch currently
    holds; the next _get_lock(same path) call then creates a FRESH asyncio.Lock, so
    the held peer and the new caller serialise on DIFFERENT lock objects for the SAME
    path — i.e. they do not serialise at all. `_MAX_FILE_LOCKS` had no prior coverage.
    """

    def test_hard_cap_never_evicts_a_held_lock(self, tmp_path: Path) -> None:
        """Every entry held at cap → table grows past _MAX_FILE_LOCKS rather than
        evicting a held lock; the identity of every held lock object is preserved."""
        import coordinator_core.hooks.track_touched_files as ttf

        async def _run():
            ttf._FILE_LOCKS.clear()
            held_paths = [str(tmp_path / f"held_{i}.txt") for i in range(ttf._MAX_FILE_LOCKS)]
            held_locks = [ttf._get_lock(p) for p in held_paths]
            # Acquire every one of them and keep it held across the new _get_lock calls
            # below — mirrors a peer dispatch mid-`async with lock:`.
            for lock in held_locks:
                await lock.acquire()
            try:
                assert len(ttf._FILE_LOCKS) == ttf._MAX_FILE_LOCKS
                identities_before = {p: ttf._FILE_LOCKS[p] for p in held_paths}

                # Request MORE locks than the cap while every existing entry is held.
                new_path = str(tmp_path / "new_over_cap.txt")
                new_lock = ttf._get_lock(new_path)

                # The table must grow past the cap rather than evict any held entry.
                assert len(ttf._FILE_LOCKS) == ttf._MAX_FILE_LOCKS + 1, (
                    "table must grow past _MAX_FILE_LOCKS when every existing entry "
                    "is held, not evict a held lock"
                )
                for p in held_paths:
                    assert p in ttf._FILE_LOCKS, f"held lock for {p} was evicted"
                    assert ttf._FILE_LOCKS[p] is identities_before[p], (
                        f"held lock object for {p} was replaced — a peer holding the "
                        f"old object and a new caller resolving the new object would "
                        f"serialise on two different locks for the same path"
                    )
                assert new_path in ttf._FILE_LOCKS
                assert ttf._FILE_LOCKS[new_path] is new_lock
            finally:
                for lock in held_locks:
                    lock.release()
                ttf._FILE_LOCKS.clear()

        asyncio.run(_run())

    def test_hard_cap_evicts_unheld_before_growing(self, tmp_path: Path) -> None:
        """At cap with a mix of held and unheld entries, eviction prefers an unheld
        entry over growing the table — held-aware, not held-blind growth-always."""
        import coordinator_core.hooks.track_touched_files as ttf

        async def _run():
            ttf._FILE_LOCKS.clear()
            unheld_path = str(tmp_path / "unheld_victim.txt")
            other_paths = [str(tmp_path / f"other_{i}.txt") for i in range(ttf._MAX_FILE_LOCKS - 1)]
            ttf._get_lock(unheld_path)  # left unheld — eviction-eligible
            held_locks = [ttf._get_lock(p) for p in other_paths]
            for lock in held_locks:
                await lock.acquire()
            try:
                assert len(ttf._FILE_LOCKS) == ttf._MAX_FILE_LOCKS

                new_path = str(tmp_path / "new_within_cap.txt")
                ttf._get_lock(new_path)

                # Table stays at the cap — the unheld entry was evicted, not grown past.
                assert len(ttf._FILE_LOCKS) == ttf._MAX_FILE_LOCKS
                assert unheld_path not in ttf._FILE_LOCKS, "unheld entry should be evicted first"
                assert new_path in ttf._FILE_LOCKS
                for p in other_paths:
                    assert p in ttf._FILE_LOCKS, f"held lock for {p} must survive eviction"
            finally:
                for lock in held_locks:
                    lock.release()
                ttf._FILE_LOCKS.clear()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# CROSS-PROCESS CONCURRENCY — locked_rmw flock layer
#
# These tests spawn real OS subprocesses to validate that concurrent writes from
# DIFFERENT PROCESSES are serialised by locked_rmw. The existing asyncio.gather
# tests in TestConcurrency cover intra-process concurrency (asyncio.Lock / D6);
# these cover the cross-process layer added by C5.
#
# Each test requires a real git repository (git init) in the tmp_path so that
# git_common_dir succeeds inside locked_rmw. Without a real git repo, locked_rmw
# raises RuntimeError and the fallback path is taken (intra-process only).
# ---------------------------------------------------------------------------

# Path to the project root (coordinator_core's parent) — embedded in subprocess scripts
# so they can import coordinator_core without relying on a pre-configured PYTHONPATH.
_PROJECT_ROOT = str(Path(__file__).parent.parent.parent.resolve())

_NO_WIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git_init_lenient(path: Path) -> None:
    """Run git init in path — required for locked_rmw (needs git_common_dir to succeed)."""
    subprocess.run(
        ["git", "init", str(path)],
        capture_output=True,
        creationflags=_NO_WIN,
    )
    # Non-fatal if git unavailable — locked_rmw falls back to in-process path.
    # Test assertions hold for the fallback path too.


class TestCrossProcessConcurrency:
    """Concurrent OS-process writes to the same bookkeeping files are serialised by locked_rmw.

    Existing TestConcurrency tests cover asyncio.gather within one process (asyncio.Lock / D6).
    These tests spawn real subprocesses to exercise the cross-process flock layer added in C5.

    Uses a git-init'd tmp_path so locked_rmw can resolve git_common_dir and create the
    coordinator-locks sidecar. Tests remain meaningful on non-POSIX or non-git paths
    (locked_rmw fallback path), though cross-process protection is absent there.
    """

    def test_c1_cross_process_same_path_no_duplicate(self, tmp_path: Path) -> None:
        """Two subprocesses writing the same file_path to touched.txt → both T-events land
        intact (dedup retired — append-only last-event-wins).

        Validates the locked_rmw cross-process layer: even when two separate Python
        processes race on the same touched.txt, the flock serialises their reads and
        writes so BOTH appends land without corruption or loss — neither collapses the
        other, and neither torn-writes into the file.
        """
        _git_init_lenient(tmp_path)
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "xproc10000000001"
        _make_session(tmp_path, sid)
        repo_root_str = str(tmp_path)  # working-tree root — git_common_dir resolves from here
        file_path = "src/cross_proc_shared.py"

        script = textwrap.dedent(f"""
import sys
sys.path.insert(0, {_PROJECT_ROOT!r})
import asyncio
from coordinator_core.hooks.track_touched_files import _handler
asyncio.run(_handler(
    {{"session_id": {sid!r}, "tool_name": "Write",
     "file_path": {file_path!r}, "agent_id": ""}},
    repo_root={repo_root_str!r},
))
""")

        p1 = subprocess.Popen(
            [sys.executable, "-c", script], creationflags=_NO_WIN
        )
        p2 = subprocess.Popen(
            [sys.executable, "-c", script], creationflags=_NO_WIN
        )
        p1.wait()
        p2.wait()

        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        assert touched.exists(), "touched.txt must exist after subprocess writes"
        lines = [ln for ln in touched.read_text().splitlines() if ln]
        from coordinator_core.session.scope import parse_touch_event
        events = [parse_touch_event(l) for l in lines]
        matches = [(verb, path) for verb, _ts, path in events if path == file_path]
        assert len(matches) == 2, (
            f"Expected TWO T-events for {file_path} across the two subprocesses "
            f"(append-only, dedup retired) — no lost/corrupted entry; got {lines!r}"
        )
        assert all(verb == "T" for verb, _path in matches)

    def test_c1_cross_process_distinct_paths_both_recorded(self, tmp_path: Path) -> None:
        """Two subprocesses writing different file_paths → both entries present, no loss.

        Validates that the cross-process lock does not cause one write to silently drop
        when both processes touch different paths.
        """
        _git_init_lenient(tmp_path)
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "xproc10000000002"
        _make_session(tmp_path, sid)
        repo_root_str = str(tmp_path)
        path_a = "src/module_xproc_a.py"
        path_b = "src/module_xproc_b.py"

        def _script(fp: str) -> str:
            return textwrap.dedent(f"""
import sys
sys.path.insert(0, {_PROJECT_ROOT!r})
import asyncio
from coordinator_core.hooks.track_touched_files import _handler
asyncio.run(_handler(
    {{"session_id": {sid!r}, "tool_name": "Edit",
     "file_path": {fp!r}, "agent_id": ""}},
    repo_root={repo_root_str!r},
))
""")

        p1 = subprocess.Popen([sys.executable, "-c", _script(path_a)], creationflags=_NO_WIN)
        p2 = subprocess.Popen([sys.executable, "-c", _script(path_b)], creationflags=_NO_WIN)
        p1.wait()
        p2.wait()

        touched = _cs_dir(tmp_path) / sid / "touched.txt"
        assert touched.exists()
        lines = [ln for ln in touched.read_text().splitlines() if ln]
        from coordinator_core.session.scope import parse_touch_event
        events = [parse_touch_event(l) for l in lines]
        paths = [path for verb, _ts, path in events if verb == "T"]
        assert path_a in paths, f"path_a missing from cross-process write; lines={lines!r}"
        assert path_b in paths, f"path_b missing from cross-process write; lines={lines!r}"

    def test_c4_cross_process_same_agent_no_duplicate(self, tmp_path: Path) -> None:
        """Two subprocesses dispatching the same agent_id → exactly one row in dispatched-agents.txt.

        Validates the locked_rmw cross-process dedup layer for track_dispatched_agents: two
        separate processes racing on the same dispatched-agents.txt produce exactly one row
        (dedup wins) rather than two rows (lost-write corruption).
        """
        _git_init_lenient(tmp_path)
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "xproc40000000001"
        (_cs_dir(tmp_path) / sid).mkdir(parents=True, exist_ok=True)
        repo_root_str = str(tmp_path)
        aid = "aabbccddeeff1234"

        script = textwrap.dedent(f"""
import sys
sys.path.insert(0, {_PROJECT_ROOT!r})
import asyncio
from coordinator_core.hooks.track_dispatched_agents import _handler
asyncio.run(_handler(
    {{"session_id": {sid!r}, "dispatched_agent_id": {aid!r},
     "dispatched_model": "claude-sonnet-4-5", "subagent_type": "executor"}},
    repo_root={repo_root_str!r},
))
""")

        p1 = subprocess.Popen([sys.executable, "-c", script], creationflags=_NO_WIN)
        p2 = subprocess.Popen([sys.executable, "-c", script], creationflags=_NO_WIN)
        p1.wait()
        p2.wait()

        disp = _cs_dir(tmp_path) / sid / "dispatched-agents.txt"
        assert disp.exists(), "dispatched-agents.txt must exist after subprocess writes"
        lines = [ln for ln in disp.read_text().splitlines() if ln]
        assert len(lines) == 1, (
            f"Cross-process dedup failed — expected 1 row; got {len(lines)}: {lines!r}"
        )
        assert lines[0].split("\t")[0] == aid, (
            f"Col-1 mismatch: expected {aid!r}; got {lines[0].split(chr(9))[0]!r}"
        )

    def test_c4_cross_process_distinct_agents_both_recorded(self, tmp_path: Path) -> None:
        """Two subprocesses dispatching different agent_ids → both rows recorded, no loss."""
        _git_init_lenient(tmp_path)
        _cs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        sid = "xproc40000000002"
        (_cs_dir(tmp_path) / sid).mkdir(parents=True, exist_ok=True)
        repo_root_str = str(tmp_path)
        aid1 = "aabbccddeeff1111"
        aid2 = "aabbccddeeff2222"

        def _script(aid: str) -> str:
            return textwrap.dedent(f"""
import sys
sys.path.insert(0, {_PROJECT_ROOT!r})
import asyncio
from coordinator_core.hooks.track_dispatched_agents import _handler
asyncio.run(_handler(
    {{"session_id": {sid!r}, "dispatched_agent_id": {aid!r},
     "dispatched_model": "claude-haiku-4-5", "subagent_type": "code-reviewer"}},
    repo_root={repo_root_str!r},
))
""")

        p1 = subprocess.Popen([sys.executable, "-c", _script(aid1)], creationflags=_NO_WIN)
        p2 = subprocess.Popen([sys.executable, "-c", _script(aid2)], creationflags=_NO_WIN)
        p1.wait()
        p2.wait()

        disp = _cs_dir(tmp_path) / sid / "dispatched-agents.txt"
        assert disp.exists()
        lines = [ln for ln in disp.read_text().splitlines() if ln]
        col1s = [ln.split("\t")[0] for ln in lines]
        assert aid1 in col1s, f"aid1 missing from cross-process write; col1s={col1s!r}"
        assert aid2 in col1s, f"aid2 missing from cross-process write; col1s={col1s!r}"


# ---------------------------------------------------------------------------
# REGISTRY — 4 new bookkeeping ops registered
# ---------------------------------------------------------------------------

class TestRegistryBookkeepingOps:
    """All 4 bookkeeping ops must be present in the IPC registry after import."""

    def test_all_four_bookkeeping_ops_registered(self) -> None:
        """import coordinator_core.ops → all 4 hooks.* bookkeeping ops in _REGISTRY."""
        import coordinator_core.ops  # noqa: F401 — triggers registration side-effects
        from coordinator_core.ipc import _REGISTRY

        expected = {
            "hooks.track_touched_files",
            "hooks.session_heartbeat",
            "hooks.agent_completion_log",
            "hooks.track_dispatched_agents",
        }
        for name in expected:
            assert name in _REGISTRY, (
                f"Bookkeeping op {name!r} not in _REGISTRY. Registered: {sorted(_REGISTRY)}"
            )


# ---------------------------------------------------------------------------
# SOURCE-LEVEL GREP — mcp-async-handler-discipline
# ---------------------------------------------------------------------------

_HANDLER_FILES = [
    "coordinator_core/hooks/track_touched_files.py",
    "coordinator_core/hooks/session_heartbeat.py",
    "coordinator_core/hooks/agent_completion_log.py",
    "coordinator_core/hooks/track_dispatched_agents.py",
]

# Blocking I/O primitives that must NEVER appear bare in an async handler body.
# Legitimate uses live in sync helper functions (outside the async def).
# Review: code-reviewer F3 (P2) — added Path method I/O patterns; Path.write_text(),
# Path.read_text(), Path.stat() etc. are synchronous blocking calls and the most
# idiomatic Python file I/O style — a handler calling path.write_text() without
# asyncio.to_thread() would have been invisible to the prior pattern set.
# Lines containing "asyncio.to_thread" are filtered downstream (legitimate wrapping).
_BLOCKED_PATTERNS = [
    re.compile(r"(?<!\w)(open)\s*\("),          # bare open() call
    re.compile(r"\bos\.stat\s*\("),             # bare os.stat()
    re.compile(r"\bos\.rename\s*\("),           # bare os.rename()
    re.compile(r"\bos\.replace\s*\("),          # bare os.replace()
    re.compile(r"\bsubprocess\.(run|call|check_output|Popen)\s*\("),  # bare subprocess
    re.compile(r"\.(write_text|write_bytes|read_text|read_bytes|stat|rename|replace)\s*\("),  # Path/file-obj method I/O
]


def _extract_async_handler_body(source: str) -> str:
    """Extract lines belonging to the `async def _handler` function in source.

    Returns a string containing only the lines of the async handler body (indented
    lines after `async def _handler`). Stops at the next top-level definition.
    """
    lines = source.splitlines()
    in_handler = False
    body_lines: list[str] = []
    for line in lines:
        if re.match(r"^async def _handler\s*\(", line):
            in_handler = True
            continue
        if in_handler:
            # Review: code-reviewer F9 (nit) — any non-indented, non-blank line ends the
            # handler body (code OR comment at indent 0). Previously comments at indent 0
            # were excluded from the break condition, causing top-level separator comments
            # between functions to be consumed into body_lines. The _assert_no_bare_io
            # comment-stripper compensated, but the extractor boundary was imprecise.
            if line and not line[0].isspace():
                break  # non-indented line (def/class/comment/assignment) → end of handler
            body_lines.append(line)
    return "\n".join(body_lines)


class TestAsyncHandlerNoBareIO:
    """Source-level grep: no bare blocking I/O inside the async def _handler bodies.

    All blocking I/O in the 4 handlers must be dispatched via asyncio.to_thread().
    The sync helpers (open, os.stat, subprocess, etc.) live in module-level sync
    functions, NOT in the async handler body itself (mcp-async-handler-discipline).
    """

    def _repo_root(self) -> Path:
        """Return the project root (three levels up from this file)."""
        return Path(__file__).parent.parent.parent.resolve()

    def _get_handler_body(self, rel_path: str) -> str:
        fpath = self._repo_root() / rel_path
        assert fpath.exists(), f"Handler file not found: {fpath}"
        source = fpath.read_text(encoding="utf-8")
        return _extract_async_handler_body(source)

    def test_track_touched_files_no_bare_io(self) -> None:
        """track_touched_files.py async handler body contains no bare blocking I/O calls."""
        body = self._get_handler_body("coordinator_core/hooks/track_touched_files.py")
        assert body, "async def _handler body must be non-empty"
        self._assert_no_bare_io(body, "track_touched_files")

    def _assert_no_bare_io(self, body: str, handler_name: str) -> None:
        """Assert no bare blocking I/O pattern in non-comment, non-to_thread lines of body."""
        for pat in _BLOCKED_PATTERNS:
            # Skip lines that are pure comments (after stripping) — comments may legitimately
            # name the wrapped function for documentation purposes (e.g. "calls subprocess.run()").
            # Skip lines that call asyncio.to_thread (these ARE the correct wrapping).
            candidate_lines = [
                l for l in body.splitlines()
                if l.strip() and
                not l.strip().startswith("#") and
                "asyncio.to_thread" not in l
            ]
            hits = [l for l in candidate_lines if pat.search(l)]
            assert not hits, (
                f"Bare blocking I/O ({pat.pattern!r}) in {handler_name} async handler:\n"
                + "\n".join(hits)
            )

    def test_session_heartbeat_no_bare_io(self) -> None:
        """session_heartbeat.py async handler body contains no bare blocking I/O calls."""
        body = self._get_handler_body("coordinator_core/hooks/session_heartbeat.py")
        assert body
        self._assert_no_bare_io(body, "session_heartbeat")

    def test_agent_completion_log_no_bare_io(self) -> None:
        """agent_completion_log.py async handler body contains no bare blocking I/O calls."""
        body = self._get_handler_body("coordinator_core/hooks/agent_completion_log.py")
        assert body
        self._assert_no_bare_io(body, "agent_completion_log")

    def test_track_dispatched_agents_no_bare_io(self) -> None:
        """track_dispatched_agents.py async handler body contains no bare blocking I/O calls."""
        body = self._get_handler_body("coordinator_core/hooks/track_dispatched_agents.py")
        assert body
        self._assert_no_bare_io(body, "track_dispatched_agents")
