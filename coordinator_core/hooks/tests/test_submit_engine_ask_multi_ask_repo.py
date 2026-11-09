"""A multi-ask engine-ask carries each entry's repo (else the top-level one) to its run: entries partition
by resolved repo, each repo fans out once, and nothing is written outside the named repos."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ASKS_ITEM_KEYS


def _repo(base: Path, name: str) -> Path:
    root = base / name
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _listing(*roots: Path) -> list[str]:
    log_dir = Path(mod.REFUSAL_LOG_REL).parts[0]
    return sorted(
        str(p) for r in roots for p in r.rglob("*") if ".git" not in p.parts and log_dir not in p.relative_to(r).parts
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _p: True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    a, b, c = _repo(tmp_path, "a"), _repo(tmp_path, "b"), _repo(tmp_path, "c")
    registered = {str(a.resolve()), str(b.resolve())}
    from coordinator_core.bash_guards import _write_bump_applicability as wb

    monkeypatch.setattr(wb, "target_is_registered_repo", lambda t, env=None: str(Path(t).resolve()) in registered)
    return a, b, c


class _Fanout:
    """Writes one ready sizing per entry under the root it is called for, as the real writer does."""

    def __init__(self):
        self.calls: list[tuple[dict, Path]] = []

    def __call__(self, params: dict, root: Path) -> dict:
        n = len(self.calls)
        self.calls.append((params, root))
        parent_id = f"ask-parent-20261105T00000{n}-aaaaa{n}"
        records = []
        for i, item in enumerate(params["asks"]):
            run_id = f"r{n}x{i}"
            rel = f"state/sizings/{run_id}.yaml"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(f"em_analysis:\n  ask_parent: {parent_id} {run_id}\n", encoding="utf-8")
            records.append({"run_id": run_id, "label": item.get("label") or run_id, "after": [], "slice_sizing": rel,
                            "status": "ready"})
        parent = root / contract.parent_record_rel(parent_id)
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(
            json.dumps({"schema": "ask-parent", "version": 1, "parent_id": parent_id, "runs": records}), encoding="utf-8"
        )
        return {"parent_id": parent_id, "runs": [{"run_id": r["run_id"], "label": r["label"]} for r in records],
                "writer_root": str(root)}


class _Launch:
    def __init__(self):
        self.calls: list[tuple[Path, list[str]]] = []

    def __call__(self, root, parent_id, children, session_id):
        self.calls.append((Path(root), [c.run_id for c in children]))
        script = Path(root) / "scratch" / "warp" / f"{parent_id}.launcher.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("// launcher\n", encoding="utf-8")
        return script


def _payload(session: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-multi-repo",
        "transcript_path": str(session / "transcript.jsonl"),
        "cwd": str(session),
        "env": {"CLAUDE_PROJECT_DIR": str(session)},
    }


def _run(session: Path, args: dict):
    fan, launch = _Fanout(), _Launch()
    out = mod.evaluate(_payload(session, args), emit=lambda *_a: {}, fanout=fan, launch=launch)
    return out, fan, launch


def _decision(out: dict) -> str:
    return (out.get("hookSpecificOutput") or {}).get("permissionDecision")


def _context(out: dict) -> str:
    return out["hookSpecificOutput"]["additionalContext"]


def _reason(out: dict) -> str:
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_asks_item_keys_carry_repo_before_writes():
    assert "repo" in ASKS_ITEM_KEYS and ASKS_ITEM_KEYS[-2:] == ("writes", "stop_after")


def test_entries_naming_two_repos_fan_out_once_per_repo_and_write_only_there(repos):
    a, b, c = repos
    before_c = _listing(c)
    args = {
        "asks": [{"ask": "in a", "label": "ea", "repo": str(a)}, {"ask": "in b", "label": "eb", "repo": str(b)}],
        "cross_repo_approved": True,
    }
    out, fan, launch = _run(c, args)
    assert _decision(out) == "allow", out
    assert [root.resolve() for _p, root in fan.calls] == [a.resolve(), b.resolve()]
    assert [[i["ask"] for i in p["asks"]] for p, _r in fan.calls] == [["in a"], ["in b"]]
    assert all("repo" not in i for p, _r in fan.calls for i in p["asks"])
    assert {Path(r).resolve(): ids for r, ids in launch.calls} == {a.resolve(): ["r0x0"], b.resolve(): ["r1x0"]}
    assert list((a / "state" / "sizings").glob("*.yaml")) and list((b / "state" / "sizings").glob("*.yaml"))
    assert not list((a / "state" / "sizings").glob("r1*")) and not list((b / "state" / "sizings").glob("r0*"))
    assert _listing(c) == before_c
    entry = Path(out["hookSpecificOutput"]["updatedInput"]["scriptPath"])
    assert a.resolve() in entry.resolve().parents
    body = entry.read_text(encoding="utf-8")
    assert all(str(p) in body for p in (a, b)) and "r0x0" in _context(out) and "r1x0" in _context(out)


def test_two_entries_each_in_their_own_repo_both_fire(repos):
    a, b, c = repos
    out, fan, launch = _run(c, {"asks": [{"ask": "x", "repo": str(a)}, {"ask": "y", "repo": str(b)}],
                                "cross_repo_approved": True})
    assert _decision(out) == "allow" and len(fan.calls) == 2 and len(launch.calls) == 2
    assert "r0x0" in _context(out) and "r1x0" in _context(out)


def test_a_top_level_repo_beside_asks_starts_every_entry_there(repos):
    a, _b, c = repos
    before_c = _listing(c)
    out, fan, _launch = _run(c, {"asks": [{"ask": "one"}, {"ask": "two"}], "repo": str(a), "cross_repo_approved": True})
    assert _decision(out) == "allow", out
    assert [root.resolve() for _p, root in fan.calls] == [a.resolve()]
    assert [i["ask"] for i in fan.calls[0][0]["asks"]] == ["one", "two"]
    assert _listing(c) == before_c


def test_entries_naming_one_repo_collapse_to_the_single_root_path(repos):
    a, _b, c = repos
    out, fan, _launch = _run(c, {"asks": [{"ask": "one", "repo": str(a)}, {"ask": "two", "repo": str(a)}],
                                 "cross_repo_approved": True})
    assert _decision(out) == "allow", out
    assert len(fan.calls) == 1 and fan.calls[0][1].resolve() == a.resolve()
    assert all("repo" not in i for i in fan.calls[0][0]["asks"])


def test_an_entry_repo_overrides_the_top_level_repo(repos):
    a, b, c = repos
    out, fan, _launch = _run(c, {"asks": [{"ask": "in a"}, {"ask": "in b", "repo": str(b)}], "repo": str(a),
                                 "cross_repo_approved": True})
    assert _decision(out) == "allow", out
    assert {root.resolve(): [i["ask"] for i in p["asks"]] for p, root in fan.calls} == {
        a.resolve(): ["in a"], b.resolve(): ["in b"]}


def test_an_unregistered_foreign_repo_is_refused_and_nothing_is_written(repos, tmp_path):
    a, _b, c = repos
    stranger = _repo(tmp_path, "stranger")
    before = _listing(a, c, stranger)
    out, fan, launch = _run(c, {"asks": [{"ask": "x", "repo": str(a)}, {"ask": "y", "repo": str(stranger)}]})
    assert _decision(out) == "deny"
    assert str(stranger) in _reason(out) and "cross_repo_approved" in _reason(out)
    assert fan.calls == [] and launch.calls == []
    assert _listing(a, c, stranger) == before


def test_an_after_edge_across_repos_is_refused_naming_both_entries(repos):
    a, b, c = repos
    before = _listing(a, b, c)
    args = {"asks": [{"ask": "x", "label": "first", "repo": str(a)},
                     {"ask": "y", "repo": str(b), "after": ["first"]}], "cross_repo_approved": True}
    out, fan, _launch = _run(c, args)
    assert _decision(out) == "deny"
    assert "asks[1]" in _reason(out) and "asks[0]" in _reason(out) and "after" in _reason(out)
    assert fan.calls == [] and _listing(a, b, c) == before


def test_an_after_edge_inside_one_repo_is_reindexed_per_group(repos):
    a, b, c = repos
    args = {"asks": [{"ask": "x", "label": "first", "repo": str(a)}, {"ask": "z", "repo": str(b)},
                     {"ask": "y", "repo": str(a), "after": ["first"]}], "cross_repo_approved": True}
    _out, fan, _launch = _run(c, args)
    by_root = {root.resolve(): p["asks"] for p, root in fan.calls}
    assert by_root[a.resolve()][1]["after"] == [0]


def test_a_join_beside_entries_in_two_repos_is_refused(repos):
    a, b, c = repos
    args = {"asks": [{"ask": "x", "repo": str(a)}, {"ask": "y", "repo": str(b)}],
            "join": {"ask": "merge", "inputs": [{"run_id": "r"}]}, "cross_repo_approved": True}
    before = _listing(a, b, c)
    out, fan, _launch = _run(c, args)
    assert _decision(out) == "deny" and "join" in _reason(out)
    assert fan.calls == [] and _listing(a, b, c) == before


def test_a_missing_entry_context_in_its_own_repo_is_refused_before_any_fan_out(repos):
    a, b, c = repos
    (a / "note.md").write_text("n\n", encoding="utf-8")
    args = {"asks": [{"ask": "x", "repo": str(a), "context": ["note.md"]},
                     {"ask": "y", "repo": str(b), "context": ["note.md"]}], "cross_repo_approved": True}
    out, fan, _launch = _run(c, args)
    assert _decision(out) == "deny" and "asks[1].context" in _reason(out)
    assert fan.calls == []


def test_a_non_string_entry_repo_is_refused(repos):
    _a, _b, c = repos
    out, fan, _launch = _run(c, {"asks": [{"ask": "x", "repo": 5}, {"ask": "y"}]})
    assert _decision(out) == "deny" and "asks[0].repo" in _reason(out) and fan.calls == []


def test_a_second_group_fan_out_failure_names_the_first_groups_written_parent(repos):
    a, b, c = repos
    inner = _Fanout()

    def fan(params, root):
        if len(inner.calls) == 1:
            raise RuntimeError("second repo refused")
        return inner(params, root)

    args = {
        "asks": [{"ask": "in a", "label": "ea", "repo": str(a)}, {"ask": "in b", "label": "eb", "repo": str(b)}],
        "cross_repo_approved": True,
    }
    launch = _Launch()
    out = mod.evaluate(_payload(c, args), emit=lambda *_a: {}, fanout=fan, launch=launch)
    assert _decision(out) == "deny"
    assert "second repo refused" in _reason(out)
    assert "ask-parent-20261105T000000-aaaaa0" in _reason(out)
    assert [ids for _r, ids in launch.calls] == [["r0x0"]]
