"""The unfired-S+ deny of `nudge_em_code_dispatch`: a session sized S or larger whose engine run
has not fired is denied a product hand edit at strict, advised at warn, across the Edit and
shell legs. Repos are hand-built loose git objects; the guard is driven directly."""

from __future__ import annotations

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core import warp_live
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight as fl
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.session import engine_route_state as ers
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-unfired"


def _patch_both(monkeypatch, name, value) -> None:
    """The hook's op() and the leaf's budget path each read their own binding of the name."""
    for target in (mod, leaf):
        monkeypatch.setattr(target, name, value)


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
    _patch_both(monkeypatch, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "strict")
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)
    _patch_both(monkeypatch, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: True)
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: [])
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: None)
    (tmp_path / "scratch").mkdir()
    return _make_repo(tmp_path, {"a.py": _lines(10), "notes.md": "n\n"})


def _edit(root: Path, rel: str = "a.py") -> dict:
    return {
        "tool_name": "Edit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(root / rel), "old_string": "l1\n", "new_string": "v1 = f(1)\n"},
    }


def _shell(root: Path, rel: str = "a.py"):
    payload = {"session_id": SID, "cwd": str(root)}
    return mod.shell_write_budget(payload, SID, str(root / rel), _lines(10).replace("l1\n", "v1 = f(1)\n"), None)


def _is_deny(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _reason(result) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _ctx(result) -> str:
    return result["hookSpecificOutput"]["additionalContext"]


def test_strict_edit_unfired_s_plus_is_denied_with_fire_step(env):
    root, _ = env
    result = mod.op(_edit(root))
    assert _is_deny(result)
    assert mod._ROUTE_UNFIRED_DENY in _reason(result)
    assert mod.fire_step(mod._SESSION_SIZING) in _reason(result)


def test_strict_shell_write_unfired_s_plus_is_denied(env):
    root, _ = env
    result = _shell(root)
    assert _is_deny(result)
    assert mod._ROUTE_UNFIRED_DENY in _reason(result)


def test_warn_unfired_s_plus_advises_without_deny(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    result = mod.op(_edit(root))
    assert result is not None and not _is_deny(result)
    assert mod._ROUTE_UNFIRED_DENY in _ctx(result)


def test_halted_phase_is_not_a_deny(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_HALTED))
    assert not _is_deny(mod.op(_edit(root)))


def test_autonomous_still_denies_unfired_s_plus(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: True)
    result = mod.op(_edit(root))
    assert _is_deny(result)
    assert mod._ROUTE_UNFIRED_DENY in _reason(result)


def test_live_run_gets_the_post_fire_deny_not_the_unfired_text(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: ["run-1"])
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda *a, **k: "ask-x")
    result = mod.op(_edit(root))
    assert _is_deny(result)
    assert "already fired engine-ask" in _reason(result)
    assert mod._ROUTE_UNFIRED_DENY not in _reason(result)


def test_landed_run_gets_the_post_fire_deny_not_the_unfired_text(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: ["run-1"])
    monkeypatch.setattr(leaf, "_COORDINATOR_PATH_MARKERS", [])
    result = mod.op(_edit(root))
    assert _is_deny(result)
    assert "already fired engine-ask" in _reason(result)
    assert mod._ROUTE_UNFIRED_DENY not in _reason(result)
    assert xb.read_lane(root, SID) is None


def test_unsized_xs_edit_in_budget_is_admitted(env, monkeypatch):
    root, _ = env
    monkeypatch.setattr(xb, "touched_s_or_larger", lambda sid, repo_root: False)
    _patch_both(monkeypatch, "_session_sizing_state", lambda payload, sid: (False, False))
    assert mod.op(_edit(root)) is None
    assert xb.read_lane(root, SID)["kind"] == "xs"


def test_plan_routed_unfired_edit_gets_plan_route_advisory_not_the_deny(env, monkeypatch):
    root, _ = env
    _patch_both(monkeypatch, "_session_sizing_state", lambda payload, sid: (True, True))
    result = mod.op(_edit(root))
    assert result is not None and not _is_deny(result)
    assert mod._PLAN_ROUTE_ADVISORY in _ctx(result)
    assert mod._ROUTE_UNFIRED_DENY not in _ctx(result)


def test_escape_sentinel_admits_unfired_s_plus_edit(env, monkeypatch):
    root, _ = env
    assert _is_deny(mod.op(_edit(root)))
    _patch_both(monkeypatch, "_take_escape", lambda *a, **k: True)
    assert mod.op(_edit(root)) is None
    assert _shell(root) is None


def test_phase_probe_raising_fails_open(env, monkeypatch):
    root, _ = env

    def boom(*a, **k):
        raise RuntimeError("probe broke")

    monkeypatch.setattr(ers, "session_route_phase", boom)
    assert not _is_deny(mod.op(_edit(root)))
    assert not _is_deny(_shell(root))


def test_doc_extension_is_exempt(env):
    root, _ = env
    assert mod.op(_edit(root, "notes.md")) is None


def test_scratch_path_is_exempt(env, tmp_path):
    root, _ = env
    payload = _edit(root)
    payload["tool_input"]["file_path"] = str(tmp_path / "scratch" / "probe.txt")
    assert mod.op(payload) is None
