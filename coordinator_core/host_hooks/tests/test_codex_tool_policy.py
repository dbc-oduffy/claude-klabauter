"""Pure native-tool projections; no resident engine, subprocess or fleet writes."""
from coordinator_core.host_hooks.codex_adapter import CodexAdapter
from coordinator_core.host_hooks.codex_tools import patch_events
from coordinator_core.host_hooks import backstop, tool_policy
from coordinator_core.host_hooks.contract import HostEvent


def _patch(text, cwd=""):
    return {"tool_name": "apply_patch", "tool_input": {"command": text}, "cwd": cwd}


def test_add_projects_entire_body_and_native_marker(tmp_path):
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Add File: a.txt\n+hello\n+world\n*** End Patch", str(tmp_path)))
    assert not warnings
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "a.txt"), "content": "hello\nworld\n"}
    assert events[0]["origin_tool_name"] == "apply_patch"


def test_unique_update_and_move_inspect_both_paths(tmp_path):
    (tmp_path / "a.txt").write_text("before\nkeep\n", encoding="utf-8")
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Update File: a.txt\n*** Move to: b.txt\n@@\n-before\n+after\n keep\n*** End Patch", str(tmp_path)))
    assert not warnings
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "a.txt")}
    assert events[1]["tool_input"] == {"file_path": str(tmp_path / "b.txt"), "content": "after\nkeep\n"}


def test_ambiguous_patch_checks_path_without_fabricating_body(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("same\nsame\n", encoding="utf-8")
    events, warnings = patch_events(_patch("*** Begin Patch\n*** Update File: CLAUDE.md\n@@\n-same\n+new\n*** End Patch", str(tmp_path)))
    assert events[0]["tool_input"] == {"file_path": str(tmp_path / "CLAUDE.md")}
    assert warnings


def test_native_patch_command_never_reaches_shell_backstop(monkeypatch):
    def unexpected(_):
        raise AssertionError("patch is not shell input")
    monkeypatch.setattr(backstop, "_command", unexpected)
    assert backstop.match(HostEvent("codex", "PreToolUse", "s", "apply_patch", {"command": "git push"})) is None


def test_codex_context_survives_without_permission_override():
    adapter = CodexAdapter()
    event = adapter.adapt_event({"hook_event_name": "PreToolUse", "tool_name": "apply_patch"})
    assert adapter.engine_event(event)["host"] == "codex"
    result = adapter.render(event, {"permissionDecision": "allow", "additionalContext": "review this"})
    assert result == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "review this"}}


def test_native_child_metadata_is_preserved_without_fabrication():
    adapter = CodexAdapter()
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "session_id": "parent",
               "agent_id": "actual-child", "agent_type": "worker", "transcript_path": "child.jsonl"}
    event = adapter.engine_event(adapter.adapt_event(payload))
    assert event["session_id"] == "parent"
    assert event["agent_id"] == "actual-child"
    assert event["transcript_path"] == "child.jsonl"
    assert "agent_id" not in adapter.engine_event(adapter.adapt_event({"hook_event_name": "PreToolUse", "session_id": "parent"}))


def test_native_patch_routes_to_shared_write_engine(tmp_path, monkeypatch):
    from coordinator_core.write_guards import engine
    calls = []
    def evaluate(payload, **kwargs):
        calls.append((payload, kwargs))
        return {"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "shared finding"}}
    monkeypatch.setattr(engine, "evaluate", evaluate)
    out = tool_policy.evaluate(_patch("*** Begin Patch\n*** Add File: a.txt\n+hello\n*** End Patch", str(tmp_path)))
    assert out == {"additionalContext": "shared finding"}
    assert calls[0][0]["tool_name"] == "Write"
    assert calls[0][1]["aggregate"] is True


def test_merge_preserves_real_deny_and_advisories():
    assert tool_policy._merge([{"hookSpecificOutput": {"permissionDecision": "allow", "additionalContext": "warn"}}], []) == {"additionalContext": "warn"}
    assert tool_policy._merge([[{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "floor"}}]], ["warn"])["permissionDecision"] == "deny"


def test_named_mcp_floor_does_not_scan_arbitrary_argument_text(monkeypatch):
    from coordinator_core.bash_guards import block_perforce_submit as p4
    monkeypatch.setattr(p4, "_armed", lambda: True)
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_submit", "tool_input": {}})
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_status", "tool_input": {"description": "submit changelist"}}) is None
    monkeypatch.setattr(p4, "_armed", lambda: False)
    assert p4.check_mcp({"tool_name": "mcp__example_game_repo__source_control_submit"}) is None


# --- multi-file apply_patch: one advisory carrying the patch's true totals -------------------------

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import nudge_em_code_dispatch as budget_hook
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-multifile"


def _obj(git, kind, payload):
    raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
    sha = hashlib.sha1(raw).hexdigest()
    path = git / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(raw))
    return sha


def _make_repo(tmp_path, files):
    """A loose-object repo whose HEAD commit holds `files` (flat names) as base content."""
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    body = b""
    for name, text in sorted(files.items()):
        (root / name).write_text(text)
        body += b"100644 " + name.encode() + b"\0" + bytes.fromhex(_obj(git, "blob", text.encode()))
    tree = _obj(git, "tree", body)
    sha = _obj(git, "commit", f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nm\n".encode())
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    return root, sha


def _lines(n, tag="l"):
    return "".join(f"{tag}{i}\n" for i in range(n))


def _add(name, n):
    return f"*** Add File: {name}\n" + "".join(f"+row {i}\n" for i in range(n))


def _update(name, removed, added):
    """Replace base lines l1..l<removed> of `name` with `added` new lines."""
    return (f"*** Update File: {name}\n@@\n" + "".join(f"-l{i}\n" for i in range(1, removed + 1))
            + "".join(f"+new{i}\n" for i in range(added)))


def _patch_of(*hunks):
    return "*** Begin Patch\n" + "".join(hunks) + "*** End Patch"


@pytest.fixture
def budget_env(tmp_path, monkeypatch):
    """Strict-latch-style isolation: product paths are in-repo, the session is unsized, level is warn."""
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    for target in (budget_hook, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (False, False))
    monkeypatch.setattr(budget_hook, "resolve_mode", lambda key, sid: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    root, sha = _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10)})
    return root, sha


def _call(root, text, sid=SID):
    return {"tool_name": "apply_patch", "tool_input": {"command": text}, "cwd": str(root), "session_id": sid}


def _advisory_texts(out):
    return [line for line in out.get("additionalContext", "").splitlines() if "XS budget spent" in line]


MULTI_FILE_PATCHES = [
    ("add-add", _patch_of(_add("bin/wordcount.py", 42), _add("tests/test_wordcount.py", 74)), "2/2 files, 116/30 lines"),
    ("update-update", _patch_of(_update("a.py", 5, 20), _update("b.py", 5, 20)), "2/2 files, 50/30 lines"),
    ("add-update", _patch_of(_add("bin/new.py", 20), _update("a.py", 5, 20)), "2/2 files, 45/30 lines"),
]


@pytest.mark.parametrize("name,text,totals", MULTI_FILE_PATCHES, ids=[p[0] for p in MULTI_FILE_PATCHES])
def test_multi_file_patch_draws_one_advisory_with_summed_totals(budget_env, name, text, totals):
    root, _ = budget_env
    for sid in (SID, SID + "-other"):
        for _ in range(2):
            texts = _advisory_texts(tool_policy.evaluate(_call(root, text, sid)))
            assert len(texts) == 1 and totals in texts[0], texts


def test_each_file_alone_in_budget_but_the_patch_is_over(budget_env):
    root, _ = budget_env
    alone = _patch_of(_update("a.py", 5, 20))
    assert _advisory_texts(tool_policy.evaluate(_call(root, alone, "solo"))) == []


def test_admitted_multi_file_patch_records_every_file_in_the_lane(budget_env):
    root, sha = budget_env
    assert xb.open_xs_lane(root, SID, sha)
    out = tool_policy.evaluate(_call(root, _patch_of(_add("p.py", 3), _add("q.py", 4))))
    assert _advisory_texts(out) == []
    assert sorted(xb.read_lane(root, SID)["paths"]) == ["p.py", "q.py"]
    (root / "p.py").write_text("".join(f"row {i}\n" for i in range(3)))
    (root / "q.py").write_text("".join(f"row {i}\n" for i in range(4)))
    again = _advisory_texts(tool_policy.evaluate(_call(root, _patch_of(_add("r.py", 1)))))
    assert len(again) == 1 and "3/2 files, 8/30 lines" in again[0]


def test_exempt_file_is_not_priced_into_the_patch(budget_env):
    root, _ = budget_env
    text = _patch_of(_add("notes.md", 100), _add("bin/one.py", 5), _add("bin/two.py", 5))
    assert _advisory_texts(tool_policy.evaluate(_call(root, text))) == []


def test_patch_events_share_the_whole_patch_projection(tmp_path):
    text = _patch_of(_add("x.py", 2), _add("y.py", 3))
    events, _ = patch_events(_patch(text, str(tmp_path)))
    assert len(events) == 2
    assert events[0]["patch_projections"] == events[1]["patch_projections"]
    assert [p["file_path"] for p in events[0]["patch_projections"]] == [str(tmp_path / "x.py"), str(tmp_path / "y.py")]
    single, _ = patch_events(_patch(_patch_of(_add("x.py", 2)), str(tmp_path)))
    assert "patch_projections" not in single[0]


def test_one_path_in_two_events_leads_once(budget_env, tmp_path):
    from coordinator_core.hooks import _em_write_budget as em_budget

    text = _patch_of(_add("x.py", 2), _add("x.py", 3), _add("y.py", 3))
    events, _ = patch_events(_patch(text, str(tmp_path)))
    verdicts = [em_budget._patch_batch(e, e["tool_input"]["file_path"])[0] for e in events]
    assert verdicts == [False, True, True]
