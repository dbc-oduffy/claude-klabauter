"""A multi-ask child whose sizing read stalls defers its emit to the launcher; a retry reuses the minted parent."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_minted_parent_reuse import (  # noqa: F401
    _asks,
    _parents,
    _runs,
    _script,
    _submit,
    repo,
)
from coordinator_core.ops.dispatch_emit import minted_parent
from coordinator_core.ops.dispatch_emit.fan_launcher import launcher_rel


def _stall_first_child(monkeypatch) -> list:
    """`_load_sizing_docs` reads the first slice sizing it is asked for as `STALLED`, per fan-out."""
    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    real = mod._load_sizing_docs
    stalled: list = []

    def load(root, sizing_rels):
        docs = real(root, sizing_rels)
        if not stalled and sizing_rels:
            stalled.append(sizing_rels[0])
        if stalled and stalled[0] in docs:
            docs[stalled[0]] = STALLED
        return docs

    monkeypatch.setattr(mod, "_load_sizing_docs", load)
    return stalled


def _sizing_files(root: Path) -> set:
    return {p.name for p in (root / "state" / "sizings").glob("*.yaml")}


def test_a_stalled_child_sizing_read_defers_its_emit_and_still_writes_the_launcher(repo, monkeypatch):
    emitted: list = []
    inner = mod._default_emit
    monkeypatch.setattr(mod, "_default_emit", lambda params, root: emitted.append(params["reserved_run_id"]) or inner(params, root))
    stalled = _stall_first_child(monkeypatch)

    env = _submit(repo, _asks())

    script = _script(env)
    assert "Re-submit once" not in json.dumps(env)
    assert Path(script).is_file()
    text = Path(script).read_text(encoding="utf-8")
    runs = _runs(repo, _parents(repo)[0])
    assert stalled and emitted == []
    deferred = [e for e in json.loads(text.split("const EMITTED = ")[1].split(";\n")[0]) if e.get("deferred")]
    assert sorted(e["run_id"] for e in deferred) == sorted(runs)
    assert all((repo / e["path"]).is_file() for e in deferred)


def test_a_deferred_child_params_never_carry_the_defer_marker(repo, monkeypatch):
    _stall_first_child(monkeypatch)
    _script(_submit(repo, _asks()))
    for script in (repo / "scratch" / "warp").glob("*.workflow.mjs"):
        assert mod._DEFER_EMIT_KEY not in script.read_text(encoding="utf-8")


def test_a_resubmission_after_a_stall_reuses_the_parent_and_mints_no_new_sizing(repo, monkeypatch):
    _stall_first_child(monkeypatch)
    first = _script(_submit(repo, _asks()))
    sizings = _sizing_files(repo)

    monkeypatch.undo()
    second = _submit(repo, _asks())

    assert _script(second) == first
    assert _sizing_files(repo) == sizings
    assert len(_parents(repo)) == 1


def test_a_resubmission_over_a_parent_minted_with_no_launcher_writes_one_and_mints_nothing_new(repo):
    _submit(repo, _asks())
    parent = _parents(repo)[0]
    (repo / launcher_rel(parent)).unlink()
    (repo / (launcher_rel(parent) + ".emitted.json")).unlink(missing_ok=True)
    sizings = _sizing_files(repo)

    env = _submit(repo, _asks())

    assert Path(_script(env)) == repo / launcher_rel(parent)
    assert (repo / launcher_rel(parent)).is_file()
    assert _sizing_files(repo) == sizings
    assert _parents(repo) == [parent]
    assert "Re-submission of " + parent in json.dumps(env)


def test_a_parent_minted_with_no_launcher_whose_sizing_is_gone_is_not_reused(repo):
    _submit(repo, _asks())
    parent = _parents(repo)[0]
    (repo / launcher_rel(parent)).unlink()
    first_run = _runs(repo, parent)[0]
    hit = minted_parent.find_reusable(repo, minted_parent.asks_digest(_fan_params()), time.time())
    assert hit is not None and hit.launcher is None
    sizing = next(s for r, s in hit.ready if r == first_run)
    (repo / sizing).unlink()

    _submit(repo, _asks())

    assert len(_parents(repo)) == 2


def _fan_params() -> dict:
    from coordinator_core.hooks.tests.test_submit_engine_ask_minted_parent_reuse import SID

    return {"asks": [{k: v for k, v in a.items()} for a in _asks()], "session_id": SID}
