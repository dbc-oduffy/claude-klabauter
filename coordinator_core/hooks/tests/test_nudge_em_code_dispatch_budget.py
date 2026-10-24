"""XS-lane and residue-window budget branches of `nudge_em_code_dispatch.op`. Repos are hand-built
loose git objects; the guard is driven directly, not through the write-guard fan-in."""

from __future__ import annotations

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core import machine_resolver
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-budget"


def _sizing(monkeypatch, state) -> None:
    """The hook's op() and the leaf's budget path each read their own binding of the sizing state."""
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: state)


def _obj(git: Path, kind: str, payload: bytes) -> str:
    raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
    sha = hashlib.sha1(raw).hexdigest()
    p = git / "objects" / sha[:2] / sha[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zlib.compress(raw))
    return sha


def _make_repo(tmp_path: Path, files: dict) -> tuple[Path, str]:
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


def _lines(n: int, tag: str = "l") -> str:
    return "".join(f"{tag}{i}\n" for i in range(n))


@pytest.fixture
def env(tmp_path, monkeypatch):
    sys_tmp = tmp_path / "sys-tmp"
    sys_tmp.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(sys_tmp))
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "strict")
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)
    _sizing(monkeypatch, (True, False))
    root, sha = _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10), "c.py": _lines(10)})
    return root, sha


def _edit(root: Path, rel: str, old: str, new: str) -> dict:
    return {
        "tool_name": "Edit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / rel), "old_string": old, "new_string": new},
    }


def _apply(root: Path, rel: str, old: str, new: str) -> None:
    p = root / rel
    p.write_text(p.read_text().replace(old, new, 1))


def _is_deny(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_r21_replay(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    for rel, old, new in (("a.py", "l1\n", "v1 = f(1)\n"), ("b.py", "l2\n", "v2 = f(2)\n")):
        assert op_result(root, rel, old, new) is None
        _apply(root, rel, old, new)
    third = mod.op(_edit(root, "c.py", "l3\n", "v3 = f(3)\n"))
    assert _is_deny(third)
    assert "XS budget spent" in _reason(third)
    assert xb.read_lane(root, SID)["state"] == "open"
    again = mod.op(_edit(root, "a.py", "l5\n", "v5 = f(5)\n"))
    assert not _is_deny(again)


def test_denied_oversized_edit_consumes_nothing_and_one_line_retry_is_admitted(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    big = "".join(f"x{i}\n" for i in range(42))
    denied = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(denied)
    reason = _reason(denied)
    assert "XS budget spent (projected): 1/2 files, 52/30 lines" in reason
    assert "Measured from landed" not in reason
    rec = xb.read_lane(root, SID)
    assert rec["state"] == "open" and rec["paths"] == []
    assert xb.measure(root, rec, str(root / "a.py"), (root / "a.py").read_text()) == (0, 0, False)
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None


def op_result(root, rel, old, new):
    return mod.op(_edit(root, rel, old, new))


def test_line_budget_denies_the_edit_that_crosses_it(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    big = "".join(f"x{i}\n" for i in range(21))
    result = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(result)
    assert xb.read_lane(root, SID)["state"] == "open"


def test_corrupt_record_still_denies(env):
    root, _ = env
    path = xb.lane_path(root, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert _is_deny(mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")))


def test_autonomous_does_not_exempt_the_budget(env, monkeypatch):
    root, sha = env
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: True)
    xb.open_xs_lane(root, SID, sha)
    for rel, old, new in (("a.py", "l1\n", "v1 = f(1)\n"), ("b.py", "l2\n", "v2 = f(2)\n")):
        mod.op(_edit(root, rel, old, new))
        _apply(root, rel, old, new)
    assert _is_deny(mod.op(_edit(root, "c.py", "l3\n", "v3 = f(3)\n")))


def test_unmeasurable_denies_and_leaves_lane_open(env, monkeypatch):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)

    def boom(*a, **k):
        raise RuntimeError("base unreadable")

    monkeypatch.setattr(xb, "measure", boom)
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result)
    assert "unmeasurable" in _reason(result)
    assert xb.read_lane(root, SID)["state"] == "open"


def test_multiedit_without_edits_list_is_unmeasurable_in_lane(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    payload = {
        "tool_name": "MultiEdit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / "a.py")},
    }
    result = mod.op(payload)
    assert _is_deny(result)
    assert "unmeasurable" in _reason(result)
    assert xb.read_lane(root, SID)["state"] == "open"


def test_no_lane_keeps_sized_advisory(env):
    root, _ = env
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert result is not None and not _is_deny(result)
    assert xb.read_lane(root, SID) is None


def test_escaped_edit_is_not_added_to_path_set(env, monkeypatch):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    monkeypatch.setattr(mod, "_take_escape", lambda *a, **k: True)
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None
    assert xb.read_lane(root, SID)["paths"] == []


@pytest.fixture
def unsized(env, monkeypatch):
    _sizing(monkeypatch, (False, False))
    return env


def test_unsized_one_line_edit_is_admitted_and_opens_lane(unsized):
    root, sha = unsized
    assert xb.read_lane(root, SID) is None
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None
    rec = xb.read_lane(root, SID)
    assert rec["kind"] == "xs" and rec["base_sha"] == sha and rec["state"] == "open"


def test_unsized_oversized_first_write_is_denied_and_leaves_no_lane(unsized):
    root, _ = unsized
    big = "".join(f"x{i}\n" for i in range(42))
    result = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(result) and "XS budget spent" in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_unsized_unmeasurable_first_write_is_denied_and_leaves_no_lane(unsized, monkeypatch):
    root, _ = unsized

    def boom(*a, **k):
        raise RuntimeError("base unreadable")

    monkeypatch.setattr(xb, "measure", boom)
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result) and "unmeasurable" in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_discard_failure_never_changes_the_verdict(unsized, monkeypatch):
    root, _ = unsized

    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(xb, "discard_fresh_xs_lane", boom)
    big = "".join(f"x{i}\n" for i in range(42))
    assert _is_deny(mod.op(_edit(root, "a.py", _lines(10), big)))


def test_refusal_on_a_preexisting_lane_still_records_the_deny(env):
    root, sha = env
    assert xb.open_xs_lane(root, SID, sha)
    big = "".join(f"x{i}\n" for i in range(42))
    assert _is_deny(mod.op(_edit(root, "a.py", _lines(10), big)))
    rec = xb.read_lane(root, SID)
    assert rec is not None and xb.deny_count(rec) == 1


def test_unsized_third_file_is_denied_with_resize_ask(unsized):
    root, _ = unsized
    for rel, old, new in (("a.py", "l1\n", "v1 = f(1)\n"), ("b.py", "l2\n", "v2 = f(2)\n")):
        assert mod.op(_edit(root, rel, old, new)) is None
        _apply(root, rel, old, new)
    third = mod.op(_edit(root, "c.py", "l3\n", "v3 = f(3)\n"))
    assert _is_deny(third)
    assert "XS budget spent" in _reason(third) and "Size S or larger" in _reason(third)


def test_unsized_cumulative_lines_past_30_is_denied(unsized):
    root, _ = unsized
    first = "".join(f"x{i}\n" for i in range(11))
    assert mod.op(_edit(root, "a.py", _lines(10), first)) is None
    (root / "a.py").write_text(first)
    second = "".join(f"y{i}\n" for i in range(20))
    result = mod.op(_edit(root, "b.py", _lines(10), second))
    assert _is_deny(result)
    assert "XS budget spent" in _reason(result)


def test_sized_session_without_lane_keeps_head_decision(env):
    root, _ = env
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert result is not None and not _is_deny(result)
    assert "route through the warp run" in _reason_ctx(result)
    assert xb.read_lane(root, SID) is None


def test_above_xs_sizing_unfired_is_denied(unsized, monkeypatch):
    root, _ = unsized
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    _sizing(monkeypatch, (True, False))
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result)
    assert xb.read_lane(root, SID) is None


def _reason_ctx(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


@pytest.fixture
def landed(env, monkeypatch):
    root, sha = env
    from coordinator_core import warp_live
    from coordinator_core.hooks import flag_em_poll_in_flight as fl

    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: ["run-1"])
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: None)
    return root, sha


def test_landed_run_edit_is_denied_post_fire_and_opens_no_lane(landed):
    root, _ = landed
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result)
    assert "already fired engine-ask" in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_landed_over_budget_edit_gets_the_post_fire_deny_not_the_residue_text(landed):
    root, _ = landed
    big = "".join(f"x{i}\n" for i in range(40))
    result = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(result)
    assert "already fired engine-ask" in _reason(result)
    assert "Residue budget spent" not in _reason(result)
    assert _is_deny(mod.op(_edit(root, "b.py", "l1\n", "v1 = f(1)\n")))


def test_live_run_is_denied_post_fire_too(landed, monkeypatch):
    root, _ = landed
    from coordinator_core import warp_live

    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: "run-1")
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result)
    assert "already fired engine-ask" in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_after_fire_read_failure_is_no_signal(landed, monkeypatch):
    root, _ = landed
    from coordinator_core.hooks import flag_em_poll_in_flight as fl

    def boom(sid):
        raise OSError("run index unreadable")

    monkeypatch.setattr(fl, "session_workflow_run_ids", boom)
    assert mod._post_fire_deny(SID) is None
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert "already fired engine-ask" not in str(result)


def test_post_fire_deny_stands_down_without_a_session_or_a_fire(landed, monkeypatch):
    from coordinator_core.hooks import flag_em_poll_in_flight as fl

    assert mod._post_fire_deny("") is None
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: [])
    assert mod._post_fire_deny(SID) is None


# Captured at import, before the `env` fixture stubs the seam out.
_REAL_SEAM = mod._is_bootstrap_or_out_of_repo


def _declare_general_work_root(monkeypatch, value):
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", _REAL_SEAM)
    monkeypatch.setattr(
        machine_resolver,
        "registry_get",
        lambda key: value if key == "coordinator.general_work_root" else None,
    )


def _over_budget_edit(root):
    big = "".join(f"x{i}\n" for i in range(40))
    return mod.op(_edit(root, "a.py", _lines(10), big))


def test_general_work_root_target_is_not_denied(landed, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, str(root.parent))
    assert not _is_deny(_over_budget_edit(root))
    assert xb.read_lane(root, SID) is None


def test_checkout_outside_general_work_root_still_denied(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    other = tmp_path / "elsewhere"
    other.mkdir()
    _declare_general_work_root(monkeypatch, str(other))
    assert _is_deny(_over_budget_edit(root))


def test_unreadable_general_work_root_still_denied(landed, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, None)

    def _boom(key):
        raise OSError("registry unreadable")

    monkeypatch.setattr(machine_resolver, "registry_get", _boom)
    assert _is_deny(_over_budget_edit(root))


@pytest.mark.parametrize("unset", [None, ""])
def test_unset_general_work_root_still_denied(landed, monkeypatch, unset):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, unset)
    assert _is_deny(_over_budget_edit(root))


def _general_work_session(tmp_path, monkeypatch, payload):
    gw = tmp_path / "general-work"
    gw.mkdir()
    _declare_general_work_root(monkeypatch, str(gw))
    payload["cwd"] = str(gw)
    return payload


def test_general_work_session_editing_foreign_checkout_is_denied_with_memo_pointer(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    big = "".join(f"x{i}\n" for i in range(40))
    payload = _general_work_session(tmp_path, monkeypatch, _edit(root, "a.py", _lines(10), big))
    result = mod.op(payload)
    assert _is_deny(result)
    reason = _reason(result)
    assert str(root) in reason
    assert "cross-repo-memo draft" in reason
    assert "SendMessage" in reason
    assert xb.read_lane(root, SID) is None


def test_general_work_session_edit_outside_every_checkout_is_not_denied(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    _sizing(monkeypatch, (False, False))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "tool.go").write_text("a\n")
    payload = {
        "tool_name": "Edit",
        "session_id": SID,
        "tool_input": {"file_path": str(plain / "tool.go"), "old_string": "a", "new_string": "bb"},
    }
    payload = _general_work_session(tmp_path, monkeypatch, payload)
    assert mod.op(payload) is None


def _insert(after: str, n: int, tag: str) -> tuple[str, str]:
    """(old, new) of an Edit that is a pure insertion of n lines after `after`."""
    return after, after + _lines(n, tag)


def test_unsized_retry_is_admitted_only_within_remaining_budget(unsized):
    root, _ = unsized

    old, new = _insert("l1\n", 20, "p")
    assert mod.op(_edit(root, "a.py", old, new)) is None
    _apply(root, "a.py", old, new)
    rec = xb.read_lane(root, SID)
    assert rec["state"] == "open"
    paths_before = list(rec["paths"])
    assert paths_before

    old, new = _insert("l5\n", 11, "q")
    denied = mod.op(_edit(root, "a.py", old, new))
    assert _is_deny(denied)
    assert "XS budget spent (projected)" in _reason(denied)
    assert "Measured from landed" not in _reason(denied)
    assert xb.read_lane(root, SID)["paths"] == paths_before

    old, new = _insert("l3\n", 1, "r")
    assert mod.op(_edit(root, "a.py", old, new)) is None
    _apply(root, "a.py", old, new)

    old, new = _insert("l1\n", 10, "s")
    over = mod.op(_edit(root, "b.py", old, new))
    assert _is_deny(over)
    assert "Measured from landed" not in _reason(over)
    assert "closed" in _reason(over)

    old, new = _insert("l1\n", 9, "t")
    assert _is_deny(mod.op(_edit(root, "b.py", old, new)))
    assert xb.read_lane(root, SID)["state"] == "open"


def test_unsized_write_to_existing_file_is_priced_against_base(unsized):
    root, _ = unsized
    content = _lines(10) + _lines(31, "w")
    payload = {
        "tool_name": "Write",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / "a.py"), "content": content},
    }
    result = mod.op(payload)
    assert _is_deny(result)
    assert "XS budget spent (projected)" in _reason(result)


def test_non_general_work_session_is_not_given_the_memo_pointer(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, str(tmp_path / "general-work"))
    result = _over_budget_edit(root)
    assert _is_deny(result)
    assert "cross-repo-memo draft" not in _reason(result)


def _shell(root, rel, projected, unprojectable=None, **extra):
    payload = {"session_id": SID, "cwd": str(root), **extra}
    return mod.shell_write_budget(payload, SID, str(root / rel), projected, unprojectable)


def test_shell_write_in_budget_is_admitted_and_recorded(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    assert _shell(root, "a.py", _lines(10).replace("l1\n", "v1 = f(1)\nv1b = 2\n")) is None
    assert xb.read_lane(root, SID)["paths"] == ["a.py"]


def test_shell_write_over_budget_is_denied(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    result = _shell(root, "a.py", "".join(f"x{i}\n" for i in range(42)))
    assert _is_deny(result)
    assert "XS budget spent (projected):" in _reason(result)
    assert xb.read_lane(root, SID)["paths"] == []


def test_shell_write_unprojectable_denied_in_lane_only(env):
    root, sha = env
    assert _shell(root, "a.py", None, "interpolated body") is None
    xb.open_xs_lane(root, SID, sha)
    result = _shell(root, "a.py", None, "interpolated body")
    assert _is_deny(result)
    assert "Budget unmeasurable (interpolated body). Make the change as one plain Edit per file." in _reason(result)


def test_shell_write_exemptions_return_none(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    big = "".join(f"x{i}\n" for i in range(42))
    assert _shell(root, "a.py", big, agent_id="sub") is None
    assert _shell(root, "notes.md", big) is None
    assert xb.read_lane(root, SID)["paths"] == []


def test_shell_write_s_sized_unfired_is_denied(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    assert _is_deny(_shell(root, "a.py", "".join(f"x{i}\n" for i in range(42))))


def _big(n: int) -> str:
    return "".join(f"x{i}\n" for i in range(n))


def test_measured_clause_is_absent_from_xs_and_residue_denies(env, monkeypatch):
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope

    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    xs = mod.op(_edit(root, "a.py", _lines(10), _big(42)))
    assert _is_deny(xs)
    assert "Measured from landed" not in _reason(xs)
    assert "XS budget spent (projected): 1/2 files, 52/30 lines. Size S or larger. " in _reason(xs)
    assert measure_envelope(xs).prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    monkeypatch.setattr(xb, "read_lane", lambda r, s: {"kind": "residue", "state": "open", "base_sha": sha, "paths": []})
    res = mod.op(_edit(root, "a.py", _lines(10), _big(42)))
    assert _is_deny(res)
    assert "Measured from landed" not in _reason(res)
    assert "Residue budget spent (projected)" in _reason(res)


def test_golf_second_projected_deny_closes_hand_edits(env):
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope

    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    first = mod.op(_edit(root, "a.py", _lines(10), _big(47)))
    assert _is_deny(first) and "Size S or larger" in _reason(first)
    assert xb.deny_count(xb.read_lane(root, SID)) == 1
    second = mod.op(_edit(root, "a.py", _lines(10), _big(84)))
    assert _is_deny(second)
    assert "Size S or larger" not in _reason(second)
    assert "Workflow" in _reason(second) and f"git diff {sha[:12]}" in _reason(second)
    assert measure_envelope(second).prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    third = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(third) and f"git diff {sha[:12]}" in _reason(third)
    assert xb.read_lane(root, SID)["paths"] == []


def test_golf_one_deny_then_one_line_retry_is_admitted(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    assert _is_deny(mod.op(_edit(root, "a.py", _lines(10), _big(47))))
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None


def test_golf_closes_shell_writes_too(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    assert "Size S or larger" in _reason(_shell(root, "a.py", _big(42)))
    closed = _shell(root, "a.py", _big(60))
    assert _is_deny(closed) and "Size S or larger" not in _reason(closed)
    assert _is_deny(_shell(root, "a.py", _lines(10).replace("l1\n", "v1 = f(1)\n")))


def test_golf_residue_record_is_never_closed_by_denies(env, monkeypatch):
    root, sha = env
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    assert xb._create_exclusive(
        root, SID, {"kind": "residue", "base_sha": sha, "state": "open", "paths": [], "denies": 5}
    )
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    over = mod.op(_edit(root, "a.py", _lines(10), _big(42)))
    assert _is_deny(over) and "Residue budget spent" in _reason(over)
    assert not _is_deny(mod.op(_edit(root, "b.py", "l1\n", "v1 = f(1)\n")))


def test_golf_corrupt_record_returns_corrupt_deny_not_closed_text(env):
    root, _ = env
    path = xb.lane_path(root, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result)
    assert "Size S or larger" in _reason(result) and "closed" not in _reason(result)


def test_golf_unmeasurable_denies_are_not_counted(env, monkeypatch):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    real = xb.measure

    def boom(*a, **k):
        raise RuntimeError("base unreadable")

    monkeypatch.setattr(xb, "measure", boom)
    for _ in range(3):
        assert "unmeasurable" in _reason(mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")))
    monkeypatch.setattr(xb, "measure", real)
    assert xb.deny_count(xb.read_lane(root, SID)) == 0
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None


@pytest.fixture
def landed_unmarked(env, monkeypatch):
    root, sha = env
    from coordinator_core import warp_live
    from coordinator_core.hooks import flag_em_poll_in_flight as fl

    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: False)
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: ["run-1"])
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: None)
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    return root, sha


def test_landed_edit_without_s_plus_touch_is_denied_post_fire(landed_unmarked):
    root, sha = landed_unmarked
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result) and "already fired engine-ask" in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_landed_edit_is_denied_post_fire_despite_a_pre_existing_xs_lane(landed_unmarked):
    root, sha = landed_unmarked
    xb.open_xs_lane(root, SID, "f" * 40)
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert _is_deny(result) and "already fired engine-ask" in _reason(result)
    assert xb.read_lane(root, SID)["kind"] == "xs"


def test_landed_residue_shell_write_is_budgeted(landed_unmarked):
    root, _ = landed_unmarked
    result = _shell(root, "a.py", _big(42))
    assert _is_deny(result) and "Residue budget spent" in _reason(result)


def test_landed_shell_write_supersedes_a_pre_existing_xs_lane(landed_unmarked):
    root, sha = landed_unmarked
    xb.open_xs_lane(root, SID, "f" * 40)
    xb.record_deny(root, SID)
    assert not _is_deny(_shell(root, "a.py", "v1 = f(1)\n"))
    rec = xb.read_lane(root, SID)
    assert rec["kind"] == "residue" and rec["base_sha"] == sha
    assert xb.deny_count(rec) == 0


def test_shell_write_escape_is_consumed_once(env, monkeypatch):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    calls = []

    def once(*a, **k):
        calls.append(1)
        return len(calls) == 1

    monkeypatch.setattr(leaf, "_take_escape", once)
    big = "".join(f"x{i}\n" for i in range(42))
    assert _shell(root, "a.py", big) is None
    assert _is_deny(_shell(root, "a.py", big))
