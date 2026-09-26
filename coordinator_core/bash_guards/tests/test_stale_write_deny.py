
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import touch_record


@pytest.fixture()
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


def _write_file(root: str, rel: str, text: str) -> str:
    abs_path = os.path.join(root, rel)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return abs_path


def _record_baseline(root: str, sid: str, rel: str, abs_path: str) -> None:
    h = touch_record.compute_content_hash(abs_path)
    touch_record.append_touch_claims([rel], sid, root, content_hashes={rel: h})


class TestStaleWriteDeny:
    def test_whole_file_rewrite_over_divergence_denies(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-A", repo
        )

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "F.txt" in reason
        assert "git log --oneline -3 -- F.txt" in reason

    def test_bare_redirect_over_divergence_denies(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write("echo hi > F.txt", "sess-A", repo)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bare_tee_over_divergence_denies(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write("echo hi | tee F.txt", "sess-A", repo)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_no_divergence_allows(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)

        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-A", repo
        )

        assert result is None

    def test_own_earlier_sed_i_is_never_compared(self, repo):
        """Falsifier arm 3: the session's own `sed -i` standing in for a
        peer's commit is still a SURGICAL write and is never compared."""
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "sed -i already changed this\n")

        result = dispatch_checks.check_stale_write("sed -i 's/x/y/' F.txt", "sess-A", repo)

        assert result is None

    def test_surgical_write_over_divergence_allows(self, repo):
        """Falsifier arm 4: a peer edits a DIFFERENT region of F and
        commits; this session's own `sed -i` on its own line of F must
        ALLOW -- the write is surgical and destroys nothing this session
        never saw. The discriminator is write shape, never read shape."""
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer edited elsewhere\n")

        result = dispatch_checks.check_stale_write("sed -i 's/y/z/' F.txt", "sess-A", repo)

        assert result is None

    def test_append_redirect_is_never_compared(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write("echo hi >> F.txt", "sess-A", repo)

        assert result is None

    def test_tee_append_is_never_compared(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write("echo hi | tee -a F.txt", "sess-A", repo)

        assert result is None

    def test_no_recorded_baseline_allows(self, repo):
        _write_file(repo, "F.txt", "original\n")

        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-never-read", repo
        )

        assert result is None

    def test_message_never_asserts_who_changed_the_file(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-A", repo
        )

        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "this file changed since your session last read it" in reason

    def test_dispatched_worker_claim_names_the_specific_case(self, repo, monkeypatch):
        monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)

        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)

        agent_dir = Path(repo) / ".git" / "coordinator-agents" / "agent-1"
        agent_dir.mkdir(parents=True)
        (agent_dir / "em-session-id.txt").write_text("sess-A\n", encoding="utf-8")
        touch_record.append_event(
            touch_record.sink_path(str(agent_dir)),
            session_id="worker-w1",
            agent_id="agent-1",
            verb=touch_record.VERB_TOUCH,
            path="F.txt",
        )

        _write_file(repo, "F.txt", "worker changed this\n")

        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-A", repo
        )

        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert (
            "a worker this session dispatched changed this file after your last read"
            in reason
        )

    def test_never_raises_on_bad_command(self, repo):
        assert dispatch_checks.check_stale_write("", "sess-A", repo) is None
        assert dispatch_checks.check_stale_write("cat > F.txt", "", repo) is None

    def test_override_env_allows(self, repo):
        abs_path = _write_file(repo, "F.txt", "original\n")
        _record_baseline(repo, "sess-A", "F.txt", abs_path)
        _write_file(repo, "F.txt", "peer changed this\n")

        payload = {"env": {"COORDINATOR_OVERRIDE_STALE_WRITE": "1"}}
        result = dispatch_checks.check_stale_write(
            "cat > F.txt <<'EOF'\nfoo\nEOF", "sess-A", repo, payload=payload
        )

        assert result is None
