from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import CrossRepoWriteError
from coordinator_core.ops.dispatch_emit.dirty_write_set import DirtyWriteSetError
from coordinator_core.ops.plan_chain.contract import Halt
from coordinator_core.ops.plan_chain.emit_leg import run


def _call(dispatch, tmp_path):
    return run("docs/plans/p.md", repo_root=tmp_path, child_session_id="sess-1",
               trail_dir="trail", chain_id="7", dispatch=dispatch)


def test_session_id_passed_and_result_returned(tmp_path):
    seen = {}

    def dispatch(msg):
        seen.update(msg)
        return {"result": {"path": msg["params"]["output_path"], "sha256": "ab" * 32}}

    got = _call(dispatch, tmp_path)
    assert got == (tmp_path / "trail" / "chain-7.execute.mjs", "ab" * 32)
    assert seen["method"] == "dispatch.emit"
    assert seen["params"]["session_id"] == "sess-1"
    assert "fire" not in seen["params"]


def test_dirty_write_set_halts(tmp_path):
    def dispatch(msg):
        raise DirtyWriteSetError("dirty")

    assert _call(dispatch, tmp_path) == Halt("execute", "dirty")


def test_cross_repo_halts(tmp_path):
    def dispatch(msg):
        raise CrossRepoWriteError("outside")

    assert _call(dispatch, tmp_path) == Halt("execute", "outside")


def test_error_reply_maps_by_message(tmp_path):
    r = _call(lambda m: {"error": {"message": "Internal error: DirtyWriteSetError: x"}}, tmp_path)
    assert isinstance(r, Halt) and r.halted_at == "execute" and "DirtyWriteSetError" in r.reason
    r = _call(lambda m: {"error": {"message": "CrossRepoWriteError: y"}}, tmp_path)
    assert isinstance(r, Halt) and "CrossRepoWriteError" in r.reason


def test_other_raise_and_empty_reply_halt(tmp_path):
    def boom(msg):
        raise RuntimeError("kaput")

    r = _call(boom, tmp_path)
    assert r.halted_at == "execute" and "kaput" in r.reason
    assert _call(lambda m: {"result": {}}, tmp_path).halted_at == "execute"
