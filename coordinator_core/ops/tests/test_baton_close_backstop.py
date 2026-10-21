"""Tests for deliverable.baton_close_backstop: divergence/residue split, read-only, one spawn."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.baton_close_evidence_chain as chain
import coordinator_core.ops.baton_close_evidence_plans as plans
import coordinator_core.ops.baton_close_evidence_records as records
import coordinator_core.ops.baton_close_predicate as predicate
from coordinator_core import ipc
from coordinator_core.ops import baton_close_backstop as op
from coordinator_core.telemetry import spawn_counter
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _baton(root: Path, rel: str, **fields: str) -> None:
    fields.setdefault("status", "open")
    fields.setdefault("deployment_state", "ready_to_fire")
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    _write(root, rel, f"---\n{body}---\n\nbody\n")


def _run_record(root: Path, name: str, files: tuple[str, ...], baton: str) -> None:
    for f in files:
        _write(root, f"state/warp/{name}/{f}", "{}")
    _write(root, f"state/warp/{name}/run.json", "{}")
    _write(root, f"state/warp/{name}/run-baton.json", json.dumps({"baton": baton}))


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if ".git" in path.relative_to(root).parts or not path.is_file():
            continue
        out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _h(name: str) -> str:
    return f"state/handoffs/{name}.md"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _write(tmp_path, "README.md", "init\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "init")
    _git(tmp_path, "branch", "feature/merged-only")

    _write(tmp_path, "state/sizings/sz.yaml", "status: shipped\n")
    _write(tmp_path, "state/sizings/cf.yaml", "status: shipped\n")
    _write(tmp_path, "state/sizings/pr.yaml", "status: shipped\n")
    _write(tmp_path, "state/sizings/dc.yaml", "status: declined\n")

    # one baton per evidence class
    _baton(tmp_path, _h("sizing-spinoff"), kind="spinoff", sizing_object="state/sizings/sz.yaml")
    _baton(tmp_path, _h("run-committed"))
    _run_record(tmp_path, "r-commit", ("run-outcome.json",), _h("run-committed"))
    _baton(tmp_path, _h("run-halted"))
    _run_record(tmp_path, "r-halt", ("halted.json",), _h("run-halted"))
    _baton(tmp_path, _h("plan-implemented"))
    _write(
        tmp_path,
        "docs/plans/p.md",
        f'---\nstatus: implemented\npm_brief: {{source: x, ref: "{_h("plan-implemented")}"}}\n---\n\nbody\n',
    )
    _baton(tmp_path, _h("successor-claimed"), status="claimed", deployment_state="in_flight")
    _baton(tmp_path, _h("successor-unclaimed"))
    _write(
        tmp_path,
        "archive/handoffs/2026-10/succ.md",
        "---\npredecessor: state/handoffs/successor-claimed.md\ndeployment_state: shipped\n---\n\nbody\n",
    )
    _write(
        tmp_path,
        "archive/handoffs/2026-10/succ2.md",
        "---\npredecessor: state/handoffs/successor-unclaimed.md\ndeployment_state: shipped\n---\n\nbody\n",
    )
    _baton(tmp_path, _h("merged-only"), deployment_state="in_flight", branch="feature/merged-only")
    _baton(tmp_path, _h("conflict"), sizing_object="state/sizings/cf.yaml")
    _run_record(tmp_path, "r-conflict", ("halted.json",), _h("conflict"))
    _baton(tmp_path, _h("declined"), sizing_object="state/sizings/dc.yaml")
    _baton(tmp_path, _h("predicate-refused"), deployment_state="paused", sizing_object="state/sizings/pr.yaml")
    _baton(tmp_path, _h("no-evidence"))
    return tmp_path


def _call(repo: Path, **params: str) -> dict:
    return asyncio.run(op._handler(params, repo))


def _by_path(items: list[dict]) -> dict[str, dict]:
    return {i["handoff_path"]: i for i in items}


def test_divergence_residue_split_and_files_untouched(repo: Path) -> None:
    before = _snapshot(repo)
    result = _call(repo)
    assert _snapshot(repo) == before

    assert result["exit_code"] == 0
    assert result["schema_version"] == predicate.SCHEMA_VERSION == 1
    assert result["op"] == "deliverable.baton_close_backstop"
    assert result["legs"] == {"merged_branch": "ran"}
    assert result["scan_incomplete"] is False

    div = _by_path(result["divergences"])
    res = _by_path(result["residue"])

    assert set(div) == {
        _h("sizing-spinoff"),
        _h("run-committed"),
        _h("plan-implemented"),
        _h("successor-claimed"),
    }
    assert div[_h("sizing-spinoff")]["proposes"] == "shipped"
    assert div[_h("successor-claimed")]["proposes"] == "continued"
    for entry in div.values():
        assert entry["evidence"]

    assert set(res) == {
        _h("run-halted"),
        _h("successor-unclaimed"),
        _h("merged-only"),
        _h("conflict"),
        _h("declined"),
        _h("predicate-refused"),
    }
    assert res[_h("run-halted")]["reason"].startswith("still live")
    assert res[_h("conflict")]["reason"].startswith("conflicting evidence")
    assert res[_h("successor-unclaimed")]["reason"] == "weak evidence only: SUCCESSOR_UNCLAIMED"
    assert res[_h("merged-only")]["reason"] == "weak evidence only: MERGED_BRANCH"
    assert res[_h("declined")]["reason"] == "weak evidence only: SIZING_DECLINED"
    assert res[_h("predicate-refused")]["reason"].startswith("predicate refused")
    for entry in res.values():
        assert entry["reason"]

    assert result["no_evidence"] == 1
    assert _h("no-evidence") not in div and _h("no-evidence") not in res
    assert result["batons_scanned"] == 11


def test_terminal_batons_are_not_scanned(repo: Path) -> None:
    _baton(repo, _h("already-shipped"), deployment_state="shipped", sizing_object="state/sizings/sz.yaml")
    result = _call(repo)
    paths = set(_by_path(result["divergences"])) | set(_by_path(result["residue"]))
    assert _h("already-shipped") not in paths
    assert result["batons_scanned"] == 11


def test_unreadable_baton_is_residue(repo: Path) -> None:
    _write(repo, _h("broken"), "no frontmatter here\n")
    res = _by_path(_call(repo)["residue"])
    assert res[_h("broken")]["reason"] == "unreadable frontmatter"


def test_handoff_param_restricts_report(repo: Path) -> None:
    result = _call(repo, handoff=_h("run-committed"))
    assert [d["handoff_path"] for d in result["divergences"]] == [_h("run-committed")]
    assert result["residue"] == []
    assert result["batons_scanned"] == 1

    only_residue = _call(repo, handoff=_h("run-halted"))
    assert only_residue["divergences"] == []
    assert [r["handoff_path"] for r in only_residue["residue"]] == [_h("run-halted")]


def test_whole_call_spawns_at_most_one_process(repo: Path) -> None:
    before = spawn_counter.spawn_count()
    _call(repo)
    assert spawn_counter.spawn_count() - before <= 1


def test_no_main_ref_skips_merged_leg_without_spawn(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-b", "scratch-root")
    _write(tmp_path, "state/handoffs/a.md", "---\nstatus: open\ndeployment_state: in_flight\n---\n\nbody\n")
    before = spawn_counter.spawn_count()
    result = _call(tmp_path)
    assert result["legs"]["merged_branch"].startswith("skipped")
    assert spawn_counter.spawn_count() - before == 0
    assert result["no_evidence"] == 1


def test_missing_repo_root_is_exit_one() -> None:
    assert asyncio.run(op._handler({}, None))["exit_code"] == 1


def test_evidence_kind_literals_match_predicate_constants() -> None:
    expected = {
        records: {
            "_SIZING_TERMINAL": predicate.SIZING_TERMINAL,
            "_SIZING_DECLINED": predicate.SIZING_DECLINED,
            "_SIZING_MISSING": predicate.SIZING_MISSING,
            "_RUN_COMMITTED": predicate.RUN_COMMITTED,
            "_RUN_HALTED": predicate.RUN_HALTED,
        },
        plans: {
            "PLAN_IMPLEMENTED": predicate.PLAN_IMPLEMENTED,
            "PLAN_SPINE_DONE": predicate.PLAN_SPINE_DONE,
        },
    }
    for module, names in expected.items():
        for attr, value in names.items():
            assert getattr(module, attr) == value
    source = Path(chain.__file__).read_text(encoding="utf-8")
    for kind in (
        predicate.SUCCESSOR_CLAIMED,
        predicate.SUCCESSOR_UNCLAIMED,
        predicate.SUCCESSOR_LIVE,
        predicate.MERGED_BRANCH,
    ):
        assert f'"{kind}"' in source


def test_op_resolves_through_dispatch_registry() -> None:
    handler = ipc._REGISTRY.get("deliverable.baton_close_backstop") or ipc._lazy_import_and_lookup(
        "deliverable.baton_close_backstop"
    )
    assert handler is op._handler


def test_evidence_descriptor_exempts_only_leg_d() -> None:
    legs = op._EVIDENCE_HANDOFF_KIND.predicate_legs
    assert legs["d"].applies is False and legs["d"].reason
    assert all(legs[k].applies for k in ("a", "b", "c"))
