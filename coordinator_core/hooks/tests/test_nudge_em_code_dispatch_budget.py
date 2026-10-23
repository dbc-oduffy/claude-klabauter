"""XS-lane and residue-window budget branches of `nudge_em_code_dispatch.op`. Repos are hand-built
loose git objects; the guard is driven directly, not through the write-guard fan-in."""

from __future__ import annotations

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-budget"


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
    monkeypatch.setattr(mod, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "strict")
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (True, False))
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
    assert xb.read_lane(root, SID)["state"] == "spent"
    again = mod.op(_edit(root, "a.py", "l5\n", "v5 = f(5)\n"))
    assert _is_deny(again)


def op_result(root, rel, old, new):
    return mod.op(_edit(root, rel, old, new))


def test_line_budget_denies_the_edit_that_crosses_it(env):
    root, sha = env
    xb.open_xs_lane(root, SID, sha)
    big = "".join(f"x{i}\n" for i in range(21))
    result = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(result)
    assert xb.read_lane(root, SID)["state"] == "spent"


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
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (False, False))
    return env


def test_unsized_one_line_edit_is_admitted_and_opens_lane(unsized):
    root, sha = unsized
    assert xb.read_lane(root, SID) is None
    assert mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n")) is None
    rec = xb.read_lane(root, SID)
    assert rec["kind"] == "xs" and rec["base_sha"] == sha and rec["state"] == "open"


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


def test_above_xs_sizing_keeps_head_decision(unsized, monkeypatch):
    root, _ = unsized
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (True, False))
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert result is not None and not _is_deny(result)
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


def test_residue_under_budget_advises_with_count(landed):
    root, _ = landed
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert result is not None and not _is_deny(result)
    assert "residue 1/2 files, 2/30 lines" in result["hookSpecificOutput"]["additionalContext"]
    assert xb.read_lane(root, SID)["kind"] == "residue"


def test_residue_over_budget_denies_naming_typed_executor(landed, monkeypatch):
    # A redirected tmp root can itself sit under a `coordinator/` directory, which would
    # select the coordinator-domain override over the extension map this test pins.
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    big = "".join(f"x{i}\n" for i in range(40))
    result = mod.op(_edit(root, "a.py", _lines(10), big))
    assert _is_deny(result)
    assert "python-executor" in _reason(result)
    assert xb.read_lane(root, SID)["state"] == "spent"
    assert _is_deny(mod.op(_edit(root, "b.py", "l1\n", "v1 = f(1)\n")))


def test_live_run_means_not_landed(landed, monkeypatch):
    root, _ = landed
    from coordinator_core import warp_live

    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: "run-1")
    result = mod.op(_edit(root, "a.py", "l1\n", "v1 = f(1)\n"))
    assert result is not None and not _is_deny(result)
    assert xb.read_lane(root, SID) is None


# Captured at import, before the `env` fixture stubs the seam out.
_REAL_SEAM = mod._is_bootstrap_or_out_of_repo


def _declare_general_work_root(monkeypatch, value):
    monkeypatch.setattr(mod, "_is_bootstrap_or_out_of_repo", _REAL_SEAM)
    monkeypatch.setattr(
        mod.machine_resolver,
        "registry_get",
        lambda key: value if key == "coordinator.general_work_root" else None,
    )


def _over_budget_edit(root):
    big = "".join(f"x{i}\n" for i in range(40))
    return mod.op(_edit(root, "a.py", _lines(10), big))


def test_general_work_root_target_is_not_denied(landed, monkeypatch):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, str(root.parent))
    assert not _is_deny(_over_budget_edit(root))
    assert xb.read_lane(root, SID) is None


def test_checkout_outside_general_work_root_still_denied(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    other = tmp_path / "elsewhere"
    other.mkdir()
    _declare_general_work_root(monkeypatch, str(other))
    assert _is_deny(_over_budget_edit(root))


def test_unreadable_general_work_root_still_denied(landed, monkeypatch):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, None)

    def _boom(key):
        raise OSError("registry unreadable")

    monkeypatch.setattr(mod.machine_resolver, "registry_get", _boom)
    assert _is_deny(_over_budget_edit(root))


@pytest.mark.parametrize("unset", [None, ""])
def test_unset_general_work_root_still_denied(landed, monkeypatch, unset):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
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
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    big = "".join(f"x{i}\n" for i in range(40))
    payload = _general_work_session(tmp_path, monkeypatch, _edit(root, "a.py", _lines(10), big))
    result = mod.op(payload)
    assert _is_deny(result)
    reason = _reason(result)
    assert str(root) in reason
    assert "cross-repo-memo draft" in reason
    assert xb.read_lane(root, SID) is None


def test_general_work_session_edit_outside_every_checkout_is_not_denied(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (False, False))
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


def test_non_general_work_session_is_not_given_the_memo_pointer(landed, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_COORDINATOR_PATH_MARKERS", [])
    root, _ = landed
    _declare_general_work_root(monkeypatch, str(tmp_path / "general-work"))
    result = _over_budget_edit(root)
    assert _is_deny(result)
    assert "cross-repo-memo draft" not in _reason(result)
