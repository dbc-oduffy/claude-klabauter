"""Tests for the hard-deny-path counter's write-guard leg (`docs/plans/
2026-08-13-em-exercisable-in-band-grant-route.md` chunk C6, AC-8).

`record_advisory_fire` is advisory-path only (both its call sites sit on
the advisory path); `record_deny_fire` mirrors it for the deny path at
`engine.evaluate`'s hard-deny-phase loop, the single seam every
`fail_closed=True` guard's returned/cleared envelope passes through.

Exercises that seam end-to-end with a fake `_Guard` (via
`engine._discover_guards` monkeypatched), covering both branches AC-8
names:

  (a) an uncleared hard-deny (no `_consume_unlock` grant) appends exactly
      one `{"guard", "session", "at", "cleared": false}` record and the
      deny envelope is still returned to the caller, unchanged in shape.
  (b) a cleared hard-deny (`_consume_unlock` returns True) appends exactly
      one record with `"cleared": true`, and the chain continues past that
      guard rather than returning its deny.
  (c) an unresolvable `session_id` is a silent no-op, same posture as
      `record_advisory_fire` — the guard's own returned envelope is
      unaffected either way.
"""

from __future__ import annotations

import json

from coordinator_core.write_guards import engine

_DENY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard-deny for testing",
    }
}


def _payload(session_id="sess-c6-deny", cwd=None):
    p = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/repo/some_file.py"},
        "session_id": session_id,
    }
    if cwd is not None:
        p["cwd"] = cwd
    return p


def _fake_hard_deny(name="fake-hard-deny-guard"):
    return engine._Guard(name, "hard-deny", ["Write"], 5, lambda payload: dict(_DENY_ENVELOPE))


def _deny_counts_path(git_root, session_id):
    return git_root / "state" / "subagent-share" / session_id / "deny-fire-counts.jsonl"


class TestUnclearedHardDenyProducesOneRecord:
    def test_one_uncleared_record_appended_and_deny_still_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_hard_deny()], []))
        monkeypatch.setattr(engine, "_consume_unlock", lambda session_id, guard_name: False)
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="sess-c6-deny", cwd=str(tmp_path)))

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        path = _deny_counts_path(tmp_path, "sess-c6-deny")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert set(record.keys()) == {"guard", "session", "at", "cleared"}
        assert record["guard"] == "fake-hard-deny-guard"
        assert record["session"] == "sess-c6-deny"
        assert record["cleared"] is False

    def test_no_record_when_session_id_unresolvable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_hard_deny()], []))
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="", cwd=str(tmp_path)))

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert not (tmp_path / "state" / "subagent-share").exists()


class TestClearedHardDenyProducesOneRecordAndChainContinues:
    def test_cleared_record_appended_and_chain_continues(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "_discover_guards", lambda: ([_fake_hard_deny()], []))
        monkeypatch.setattr(engine, "_consume_unlock", lambda session_id, guard_name: True)
        monkeypatch.setattr(
            "coordinator_core.guard_advisory_counter.resolve_git_root_cheap",
            lambda cwd=None: str(tmp_path),
        )

        out = engine.evaluate(_payload(session_id="sess-c6-cleared", cwd=str(tmp_path)))

        # The cleared hard-deny must not be returned -- the chain continues
        # past it (no other guard registered here), so evaluate() falls
        # through to None (ALLOW).
        assert out is None
        path = _deny_counts_path(tmp_path, "sess-c6-cleared")
        lines = path.read_text(encoding="utf-8").strip("\n").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["cleared"] is True
        assert record["guard"] == "fake-hard-deny-guard"


class TestDenyCounterNeverReadBack:
    """KEEP IT COUNT-AND-LOG: nothing in `engine.py` or
    `guard_advisory_counter.py` reads `deny-fire-counts.jsonl` back."""

    def test_module_source_has_no_read_of_deny_counts_file(self):
        import coordinator_core.write_guards.engine as engine_mod
        import coordinator_core.guard_advisory_counter as counter_mod
        import inspect

        # Scoped to the actual claim -- no line that reads a file (`.open("r"`,
        # `read_text`, `readlines`) also names the deny-counts file or its
        # filename constant. A blanket ban on the `read_text` substring is a
        # text-match gate on WHERE reads happen, not WHAT is read; it broke
        # the moment `_cheap_guard_metadata` (engine.py) started reading
        # guard-module *source* files for lazy discovery -- an unrelated read
        # with no connection to `deny-fire-counts.jsonl`.
        deny_markers = ("deny-fire-counts", "_DENY_COUNTS_FILENAME", "deny_counts")
        read_markers = ('.open("r"', ".open('r'", "read_text", "readlines")
        for mod in (engine_mod, counter_mod):
            src = inspect.getsource(mod)
            for line in src.splitlines():
                if any(marker in line for marker in read_markers):
                    assert not any(marker in line for marker in deny_markers), (
                        f"deny-counts file appears to be read back in {mod.__name__}: {line.strip()}"
                    )
