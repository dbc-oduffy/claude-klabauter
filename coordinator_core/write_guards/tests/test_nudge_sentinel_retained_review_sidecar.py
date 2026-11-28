"""Behavioral tests for
coordinator_core.write_guards.nudge_sentinel_retained_review_sidecar
-- the sentinel-retained-but-filled advisory guard (see the module's own
docstring).

Spec backlink: cross-repo memo
  cross-repo/inbox/2026-08-06-example-market-data-repo-em-append-integrator-dispositions-refuses-every-reviewer-sidecar.md
  and commit 347a6a98f532.
"""

from __future__ import annotations

import builtins
import os
import time

import pytest

from coordinator_core.ops.review_findings_ledger import _stamp_frontmatter_key
from coordinator_core.write_guards import (
    nudge_sentinel_retained_review_sidecar as guard,
)
from pathlib import Path


_TARGET_FILE = "coordinator_core/write_guards/block_priority_ledger_edit.py"
_TARGET_BASENAME = "block_priority_ledger_edit.py"

_SENTINEL = (
    "<!-- One entry per finding: `- [severity] <finding> "
    "— disposition: accepted | rejected | deferred — rationale: ...` -->"
)

_FINDINGS_FRONTMATTER = (
    "---\n"
    "status: open\n"
    "agent_type: coordinator:code-reviewer\n"
    "spawned_at: 2026-07-27T00:00:00Z\n"
    "lead_session_id: sess-abc\n"
    "divergence:\n"
    "  diverged: false\n"
    "commits: []\n"
    "dispatch_feed: null\n"
    "---\n\n"
)


def _sentinel_retained_filled_body(mentions_target: bool = True) -> str:
    """Sentinel comment still present AND real findings content -- the
    coverage gap population this guard fires on."""
    citation = (
        f"- [P2] `{_TARGET_BASENAME}:42` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
        if mentions_target
        else "- [P2] `unrelated_module.py:10` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
    )
    return "## Findings\n\n" + _SENTINEL + "\n\n" + citation


def _pristine_scaffold_body() -> str:
    """Sentinel present, nothing else -- genuinely unfilled scaffold."""
    return "## Findings\n\n" + _SENTINEL + "\n\n"


def _sentinel_absent_filled_body() -> str:
    """Sentinel removed, real findings present -- sibling's own deny
    territory, not this guard's population."""
    return (
        "## Findings\n\n"
        f"- [P2] `{_TARGET_BASENAME}:42` unused import — disposition: accepted — "
        "rationale: dead import, safe to drop.\n\n"
    )


#: See the sibling suite's note -- both share roots, never one.
SHARE_ROOTS = (".coordinator-local", "state")


def _write_sidecar(
    tmp_path,
    session_id: str,
    filename: str,
    *,
    agent_type: str = "coordinator:code-reviewer",
    body: str,
    share_root: str = ".coordinator-local",
) -> None:
    sidecar_dir = tmp_path / share_root / "subagent-share" / session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = _FINDINGS_FRONTMATTER.replace(
        "agent_type: coordinator:code-reviewer", f"agent_type: {agent_type}"
    )
    (sidecar_dir / filename).write_text(frontmatter + body, encoding="utf-8")


def _payload(
    tmp_path,
    file_path: str = _TARGET_FILE,
    *,
    agent_id: str = "",
    session_id: str = "sess-abc",
    tool_name: str = "Edit",
) -> dict:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path, "old_string": "x", "new_string": "y"},
        "cwd": str(tmp_path),
        "session_id": session_id,
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)


def _advise(payload):
    result = guard.check(payload)
    assert result is not None, "expected advisory"
    hso = result["hookSpecificOutput"]
    assert "additionalContext" in hso
    assert "permissionDecision" not in hso
    return result


def _silent(payload) -> None:
    result = guard.check(payload)
    assert result is None, f"expected silent (None), got {result!r}"


class TestFiresOnSentinelRetainedFilledFindings:
    def test_fires_when_sentinel_retained_but_findings_real_and_covers_target(
        self, tmp_path: Path
    ) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = _advise(_payload(tmp_path))
        text = result["hookSpecificOutput"]["additionalContext"]
        assert "codereview-sliceA.md" in text
        assert "review-findings-ledger verify" in text
        assert _TARGET_FILE in text

    def test_advisory_text_explains_warn_not_block(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = _advise(_payload(tmp_path))
        text = result["hookSpecificOutput"]["additionalContext"]
        assert "warning" in text.lower()
        assert "not a block" in text.lower()


class TestSilentOnPristineScaffold:
    def test_silent_when_sidecar_still_genuinely_unfilled(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_pristine_scaffold_body(),
        )
        _silent(_payload(tmp_path))


class TestSilentWhenSentinelAbsent:
    def test_silent_when_sentinel_already_removed(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_absent_filled_body(),
        )
        _silent(_payload(tmp_path))


class TestSilentWhenDispositionsPresent:
    def test_silent_once_integrator_dispositions_block_present(self, tmp_path: Path) -> None:
        """Historical marker only — no NEW sidecar ever writes this heading
        post-retirement, but a pre-retirement sidecar must still read as
        already-closed."""
        body = (
            _sentinel_retained_filled_body()
            + "## Integrator Dispositions\n\n- F1: Applied\n"
        )
        _write_sidecar(tmp_path, "sess-abc", "codereview-sliceA.md", body=body)
        _silent(_payload(tmp_path))


class TestSilentWhenFindingsLedgerVerified:
    def test_silent_once_verified_findings_ledger_stamp_present(self, tmp_path: Path) -> None:
        """RRI-M4: the current "loop already closed" signal is a verified
        `findings_ledger:` frontmatter stamp, not the retired heading."""
        sidecar_dir = tmp_path / ".coordinator-local" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        frontmatter = _FINDINGS_FRONTMATTER
        doc = _stamp_frontmatter_key(
            frontmatter + _sentinel_retained_filled_body(),
            "findings_ledger",
            "{rows: 1, applied: 1, em_rejected: 0, suspended: 0, verified_at: 2026-09-26T00:00:00Z}",
        )
        (sidecar_dir / "codereview-sliceA.md").write_text(doc, encoding="utf-8")
        _silent(_payload(tmp_path))


class TestScopeIsEmInlineOnly:
    def test_subagent_originated_edit_silent_unconditionally(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        _silent(_payload(tmp_path, agent_id="aexecutor-teammate-1234567890abcdef"))


class TestOverrideAndToolGating:
    def test_override_env_silences(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        monkeypatch.setenv(guard._OVERRIDE_ENV_VAR, "1")
        _silent(_payload(tmp_path))

    def test_non_write_tool_silent(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        _silent(_payload(tmp_path, tool_name="Read"))


class TestCoverageHeuristic:
    def test_silent_when_sidecar_does_not_cover_target_file(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(mentions_target=False),
        )
        _silent(_payload(tmp_path))


# ---------------------------------------------------------------------------
# Roster-keyed in-scope predicate (AC5 of
# docs/plans/2026-09-23-sidecar-guard-keying.md -- AC1-AC3 mirrored here)
# ---------------------------------------------------------------------------


def _stub_roster(monkeypatch, roster, error=None) -> None:
    """Monkeypatch the `resolve_roster` seam the shared `_LazyRoster`
    resolves through -- same seam as the sibling guard's suite."""
    from coordinator_core.bash_guards import _helpers

    monkeypatch.setattr(_helpers, "resolve_roster", lambda: (roster, error))


class TestRosterKeyedInScope:
    _STUB_ROSTER = frozenset({"coordinator:staff-eng", "coordinator:enricher"})

    def test_invented_type_off_roster_fires(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_roster(monkeypatch, self._STUB_ROSTER)
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            agent_type="some-invented-type",
            body=_sentinel_retained_filled_body(),
        )
        _advise(_payload(tmp_path))

    def test_missing_agent_type_does_not_fire(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_roster(monkeypatch, self._STUB_ROSTER)
        sidecar_dir = tmp_path / ".coordinator-local" / "subagent-share" / "sess-abc"
        sidecar_dir.mkdir(parents=True, exist_ok=True)
        (sidecar_dir / "missing-type.md").write_text(
            "---\nstatus: open\n---\n\n" + _sentinel_retained_filled_body(),
            encoding="utf-8",
        )
        _silent(_payload(tmp_path))

    def test_enumerated_persona_on_roster_does_not_fire(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_roster(monkeypatch, self._STUB_ROSTER)
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            agent_type="coordinator:staff-eng",
            body=_sentinel_retained_filled_body(),
        )
        _silent(_payload(tmp_path))


class TestCostIndependentOfShareDirSize:
    """The per-call cost is bounded however many sidecars the dir holds."""

    _UNRELATED = 5000

    def _seed_unrelated(self, tmp_path: Path, count: int, base_mtime: float) -> Path:
        share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-abc"
        share.mkdir(parents=True, exist_ok=True)
        text = _FINDINGS_FRONTMATTER + _sentinel_retained_filled_body(mentions_target=False)
        for i in range(count):
            p = share / f"unrelated-{i:05d}.md"
            p.write_text(text, encoding="utf-8")
            os.utime(p, (base_mtime - 1000 - i, base_mtime - 1000 - i))
        return share

    def _count_reads(self, monkeypatch: pytest.MonkeyPatch) -> list:
        opened: list = []
        real_read_text = Path.read_text

        def counting_read_text(self, *a, **k):
            opened.append(str(self))
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", counting_read_text)
        real_open = builtins.open

        def counting_open(*a, **k):
            opened.append(str(a[0]) if a else "")
            return real_open(*a, **k)

        monkeypatch.setattr(builtins, "open", counting_open)
        return opened

    def test_5000_unrelated_sidecars_bounded_files_and_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = time.time()
        share = self._seed_unrelated(tmp_path, self._UNRELATED, now)
        covering = share / "codereview-sliceA.md"
        covering.write_text(
            _FINDINGS_FRONTMATTER + _sentinel_retained_filled_body(), encoding="utf-8"
        )
        os.utime(covering, (now, now))

        opened = self._count_reads(monkeypatch)
        start = time.process_time()
        result = guard.check(_payload(tmp_path))
        elapsed_ms = (time.process_time() - start) * 1000
        monkeypatch.undo()

        assert result is not None, "covering sidecar must still be flagged"
        assert "codereview-sliceA.md" in result["hookSpecificOutput"]["additionalContext"]
        assert len(opened) <= guard._MAX_CANDIDATES_READ
        assert len(opened) < self._UNRELATED // 10
        assert elapsed_ms <= 500, f"check() took {elapsed_ms:.1f}ms process time"

    def test_silent_check_over_5000_sidecars_opens_constant_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_unrelated(tmp_path, self._UNRELATED, time.time())
        opened = self._count_reads(monkeypatch)
        result = guard.check(_payload(tmp_path))
        monkeypatch.undo()
        assert result is None
        assert len(opened) == guard._MAX_CANDIDATES_READ

    def test_sidecar_older_than_the_window_is_not_read(self, tmp_path: Path) -> None:
        now = time.time()
        share = self._seed_unrelated(tmp_path, guard._MAX_CANDIDATES_READ + 5, now)
        old = share / "codereview-ancient.md"
        old.write_text(
            _FINDINGS_FRONTMATTER + _sentinel_retained_filled_body(), encoding="utf-8"
        )
        os.utime(old, (now - 10_000, now - 10_000))
        _silent(_payload(tmp_path))


class TestEnvelopeShape:
    def test_envelope_is_additional_context_only(self, tmp_path: Path) -> None:
        _write_sidecar(
            tmp_path,
            "sess-abc",
            "codereview-sliceA.md",
            body=_sentinel_retained_filled_body(),
        )
        result = guard.check(_payload(tmp_path))
        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": result["hookSpecificOutput"]["additionalContext"],
            }
        }
