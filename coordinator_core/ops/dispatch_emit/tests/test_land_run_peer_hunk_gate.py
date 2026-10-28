"""dispatch.land_run refuses a declared path a live peer run also declares, and a path with an unresolved out-of-ask finding."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


_prod_land = prod._land


def _land(root, **extra) -> dict:
    return _prod_land(root, **{"inline_review": reviewed(), **extra})

PATH = "pkg/execute_review.py"
SIBLING = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-7cc61e"


def _sibling_via_plan_spine(root, withheld: dict | None = None) -> None:
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    marker = {"status": "halted"}
    if withheld:
        marker["run_artifacts"] = {"withheld": withheld}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    prod._write(root, "state/sizings/b.yaml", 'plan: "docs/plans/b.md"\n')
    prod._write(
        root, "docs/plans/b.md",
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + PATH + "\n```\n",
    )


def _prov(out: dict, reason: str) -> list:
    return [e for e in out.get("provenance") or [] if e["reason"] == reason]


def _land_dirty(root) -> tuple:
    prod._author(root, PATH, "x = 1\n")
    prod._declare(root, PATH)
    prod._record_dir(root)
    head = prod._git(["rev-parse", "HEAD"], root).strip()
    return _land(root), head


def test_halted_sibling_declaring_the_path_only_in_its_plan_spine_and_withholding_nothing_lands(repo):
    _sibling_via_plan_spine(repo)

    out, _head = _land_dirty(repo)

    assert out.get("committed") is True, out
    assert out.get("refused") is None, out
    assert PATH in prod._tree(repo)


SIZING = "state/sizings/s.yaml"
OWN_SIZING = "state/sizings/own.yaml"


def _aged(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _sizing(root, rel: str, status: str = "open", deliverable: str = "d1") -> None:
    prod._write(root, rel, f'status: {status}\ndeliverable_id: {deliverable}\nplan: "docs/plans/b.md"\n')
    prod._write(
        root, "docs/plans/b.md",
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + PATH + "\n```\n",
    )


STALE_BASELINE = "77141b7873847452257858e90a47b57945c203b5"
LIVE_BASELINE = None  # PATH is absent at HEAD: the baseline HEAD still holds


def _holder(root, run_id: str, marker: str | None, *, sizing: str = SIZING, plan: str | None = None, token=False, manifest=True, withheld=None):
    """A peer run declaring PATH through its manifest (or, without one, its sizing's plan spine), with an
    optional halt marker and fresh token. The manifest carries a `write_baseline` for PATH (a
    pre-stage fingerprint the tree no longer matches), the evidence the peer wrote there."""
    from coordinator_core.ops.dispatch_emit import kept_work, run_token

    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if manifest:
        (rdir / "manifest.json").write_text(
            json.dumps({
                "rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [],
                "write_baseline": {PATH: LIVE_BASELINE},
            }), encoding="utf-8"
        )
        # A real holder owns the dirt on PATH: its kept.json fingerprints the bytes `_land_dirty` authors.
        # Without it the shared baseline (PATH absent at HEAD) makes the dirt the landing run's own.
        kept_work.write(rdir, {"C1": {"paths": {PATH: kept_work.blob_sha(b"x = 1\n")}}}, None)
    if marker:
        body = {"status": marker, "sizing_rel": sizing, **({"plan": plan} if plan else {})}
        if withheld:
            body["run_artifacts"] = {"withheld": withheld}
        (rdir / "halted.json").write_text(json.dumps(body), encoding="utf-8")
    if token:
        run_token.mint(root, run_id, "run-a.mjs", session_id=prod.SELF)
    return rdir


def _land_with_own_sizing(root, sizing: str) -> dict:
    prod._author(root, PATH, "x = 1\n")
    prod._declare(root, PATH)
    prod._record_dir(root)
    run_json = root / "state" / "warp" / prod.RUN / "run.json"
    run_json.write_text(json.dumps({"run_id": prod.RUN, "sizing_rel": sizing}), encoding="utf-8")
    return _land(root)


def test_halted_peer_with_superseded_sizing_lands(repo):
    _sizing(repo, SIZING, "superseded")
    _holder(repo, _aged(60, "aaaaaa"), "halted")

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out


def test_halted_peer_on_another_sizing_that_withheld_nothing_lands(repo):
    """The halted peer's manifest carries a write_baseline for PATH, as in
    state/warp/ask-20261024T044300-6137b0/manifest.json, so it holds PATH until a successor lands it."""
    _sizing(repo, SIZING)
    _sizing(repo, OWN_SIZING, deliverable="other")
    holder = _aged(60, "aaaaaa")
    _holder(repo, holder, "halted")

    out = _land_with_own_sizing(repo, OWN_SIZING)
    assert out["committed"] is True, out
    assert out.get("refused") is None, out


def test_plan_landing_lands_past_the_halted_peer_and_marks_it_landed(repo):
    _sizing(repo, SIZING)
    holder = _aged(60, "aaaaaa")
    rdir = _holder(repo, holder, "halted", plan=prod.PLAN)

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert [o["run_id"] for o in out["origin_landed"]] == [holder], out
    marker = json.loads((rdir / "halted.json").read_text(encoding="utf-8"))
    assert marker["status"] == "landed"
    assert out["sha"] in json.dumps(marker)


def test_plan_landing_in_two_commits_under_two_runs_releases_each_path_and_leaves_the_marker_halted(repo):
    """The 13c09d shape: halted at ledger-check, its two declared paths landed by later runs of the same plan.
    Each commit covers only part of the manifest, so no single landing marks the origin landed."""
    path2 = "pkg/second.py"
    _sizing(repo, SIZING)
    holder = _aged(60, "aaaaaa")
    rdir = _holder(repo, holder, "halted", plan=prod.PLAN)
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "stage": "ledger-check", "sizing_rel": SIZING, "plan": prod.PLAN}),
        encoding="utf-8",
    )
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH, path2]}], "doc_writes": []}), encoding="utf-8"
    )

    prod._author(repo, PATH, "x = 1\n")
    prod._declare(repo, PATH)
    prod._record_dir(repo)
    first = _land(repo)
    assert first["committed"] is True, first
    assert json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"

    other = _aged(30, "dddddd")
    prod._author(repo, path2, "y = 1\n")
    prod._declare(repo, path2)
    prod._write(repo, f"{prod.RECORD_DIR_ROOT}/{other}/run.json", json.dumps({"run_id": other}))
    prod._write(repo, f"{prod.RECORD_DIR_ROOT}/{other}/ask.md", "ask\n")
    second = _land(repo, run_id=other)
    assert second.get("refused") is None, second
    assert second["committed"] is True, second

    assert json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"


def test_halted_peer_on_a_different_plan_is_provenance_and_is_not_marked_landed(repo):
    _sizing(repo, SIZING)
    # a halted peer holds only what it withheld or left dirty
    rdir = _holder(repo, _aged(60, "aaaaaa"), "halted", plan="docs/plans/other.md", withheld={PATH: "peer-held"})

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert _prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED), out
    assert json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"


def test_resumed_peer_with_an_unlanded_successor_lands_and_is_provenance(repo):
    _sizing(repo, SIZING)
    first, second = _aged(400, "aaaaaa"), _aged(200, "bbbbbb")
    _holder(repo, first, "resumed", token=True, manifest=False)
    _holder(repo, second, "halted", manifest=False, withheld={PATH: "peer-held"})

    held, _head = _land_dirty(repo)
    assert held["committed"] is True, held
    assert second in str(_prov(held, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), held


def test_halted_peer_whose_halt_record_omits_a_declared_dirty_path_lands(repo):
    """The 6137b0 shape (state/warp/ask-20261024T044300-6137b0/manifest.json): the manifest declares PATH
    with a write_baseline fingerprint the tree no longer matches, the halt record withheld nothing, PATH is dirty."""
    _sizing(repo, SIZING)
    holder = _aged(60, "aaaaaa")
    rdir = _holder(repo, holder, "halted", plan="docs/plans/other.md")

    out, head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)
    assert json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"


def test_halted_peer_whose_baseline_head_has_moved_past_claims_nothing_on_a_dirty_declared_path(repo):
    """Moved base: the baseline differs from the tree because a landing moved HEAD, not because the peer edited."""
    _halted_manifest_sibling(repo, baseline=STALE_BASELINE)

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out


def test_halted_peer_declaring_the_path_in_its_manifest_but_leaving_it_clean_claims_nothing(repo):
    from coordinator_core.ops.dispatch_emit import sibling_claims

    _sizing(repo, SIZING)
    _holder(repo, _aged(60, "aaaaaa"), "halted", plan="docs/plans/other.md")

    assert sibling_claims.sibling_run_claims(repo, prod.RUN, [PATH]) == {}


def test_live_peer_unlanded_on_a_superseded_sizing_lands_with_provenance(repo):
    _sizing(repo, SIZING, "superseded")
    live = _aged(60, "cccccc")
    _holder(repo, live, None, token=True, manifest=False)

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert live in str(_prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out


def test_two_live_holders_on_two_declared_paths_are_both_named_in_the_provenance(repo):
    from coordinator_core.ops.dispatch_emit import run_token

    path2 = "pkg/other.py"
    holders = {PATH: _aged(60, "dddddd"), path2: _aged(61, "eeeeee")}
    for n, (path, run_id) in enumerate(holders.items()):
        rdir = repo / "state" / "warp" / run_id
        rdir.mkdir(parents=True)
        sizing = f"state/sizings/h{n}.yaml"
        (rdir / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
        prod._write(repo, sizing, f'plan: "docs/plans/h{n}.md"\n')
        prod._write(
            repo, f"docs/plans/h{n}.md",
            "# H\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + path + "\n```\n",
        )
        run_token.mint(repo, run_id, "run-a.mjs", session_id=prod.SELF)
    prod._author(repo, PATH, "x = 1\n")
    prod._author(repo, path2, "y = 1\n")
    prod._declare(repo, PATH, path2)
    prod._record_dir(repo)

    out = _land(repo)

    assert out["committed"] is True, out
    text = str(_prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED))
    for needle in (*holders, *holders.values()):
        assert needle in text, text


def test_path_a_halted_sibling_declares_only_in_its_plan_spine_lands_naming_it_in_provenance(repo):
    _sibling_via_plan_spine(repo, withheld={PATH: "peer-held"})

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert SIBLING in str(_prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out
    assert PATH not in prod._tree(repo)


def test_halted_sibling_whose_halt_record_withholds_the_path_lands_naming_it_in_provenance(repo):
    _sibling_via_plan_spine(repo, withheld={PATH: "peer-held"})

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert SIBLING in str(_prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)), out
    assert PATH not in prod._tree(repo)


def test_an_unbacked_shipped_claim_lands_and_is_recorded_as_provenance(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import shipped_claims

    summary = {"unbacked": [{"path": PATH}]}
    monkeypatch.setattr(
        shipped_claims, "shipped_gate",
        lambda *a, **k: (f"{shipped_claims.SHIPPED_CLAIM_REFUSED}: {PATH} claimed shipped, not landing", summary),
    )

    from coordinator_core.ops.dispatch_emit.ask_contract import (
        ASK_MANIFEST_MARKER, RECORD_DIR_ROOT, RUN_DIR_ROOT, StageManifest,
    )
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    prod._author(repo, PATH, "x = 1\n")
    marker = f"{RUN_DIR_ROOT}/{prod.RUN}/commit-request.txt"
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="x1", paths=(PATH,)),), plan_path=None)
    prod._write(repo, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{prod.RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    prod._write(repo, f"{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._declare(repo, PATH)
    prod._write(repo, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json\n")

    out = _land(repo)

    assert out["committed"] is True, out
    assert out.get("refused") is None, out
    assert [e["reason"] for e in out["provenance"]] == [shipped_claims.SHIPPED_CLAIM_REFUSED], out
    assert out["shipped_claims"] == summary
    assert PATH in prod._tree(repo)


def test_a_destructive_or_secret_reason_still_refuses_through_the_same_split():
    from coordinator_core.ops.dispatch_emit import landing_integrity as li

    secret = li.Finding("a.py", "secret-in-diff", "token")
    claim = li.Finding("b.py", peer_hunk_gate.PEER_HUNKS_CO_DECLARED, "peer")

    refusing, provenance = li.split_claim_provenance([secret, claim])

    assert refusing == [secret]
    assert provenance == [claim]


def test_same_path_with_the_sibling_gone_lands(repo):
    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)


def test_unresolved_out_of_ask_finding_on_the_path_lands_with_provenance(repo):
    share = repo / ".coordinator-local" / "subagent-share" / prod.SELF
    share.mkdir(parents=True)
    (share / f"{prod.RUN}.review-wave-bookkeeping.md").write_text(
        "---\nintegrated_from: [\"rev-1\"]\n---\n\nbody\n", encoding="utf-8"
    )
    row = json.dumps([{
        "id": "finding-1", "file": PATH, "out_of_ask": True,
        "summary": "failing_detail changes in execute_review.py belong to another run",
    }])
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\n"
        f"Out-of-ask hunks.\n\n## Findings Ledger\n\n```json\n{row}\n```\n",
        encoding="utf-8",
    )

    out, head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert "rev-1" in str(_prov(out, peer_hunk_gate.OUT_OF_ASK_HUNKS)), out


def _halted_manifest_sibling(root, *, wrote: bool = True, baseline=LIVE_BASELINE) -> None:
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({
            "rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": [],
            **({"write_baseline": {PATH: baseline}} if wrote else {}),
        }), encoding="utf-8"
    )


def test_halted_peer_with_no_blob_and_no_baseline_claims_nothing_on_a_dirty_declared_path(repo):
    """Counterpart of the state/warp/ask-20261024T044300-6137b0/manifest.json shape without its write_baseline."""
    _halted_manifest_sibling(repo, wrote=False)

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out


HELD = "pkg/held.py"


def test_halted_peer_with_no_landing_commit_lands_and_stays_unreleased(repo):
    _sizing(repo, SIZING)
    holder = _aged(60, "aaaaaa")
    rdir = _holder(repo, holder, "halted", plan="docs/plans/other.md", withheld={HELD: "peer-held"})

    out, head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)
    assert "released" not in json.loads((rdir / "halted.json").read_text(encoding="utf-8"))


DELIVERY = "docs/delivery/shared.md"


def _blob(root, text: str) -> str:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text, capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()


def test_a_sibling_appending_to_a_per_run_artifact_does_not_block_the_landing(repo, monkeypatch):
    """Both runs append to docs/delivery/shared.md; this run lands only its own lines. The hold itself is
    stubbed (the claim readers are covered by test_peer_hunk_gate); settle and the blob commit are real."""
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda root, run, paths, *a, **k: [(DELIVERY, SIBLING)],
    )
    prod._write(repo, DELIVERY, "head\n")
    prod._git(["add", "."], repo)
    prod._git(["commit", "-q", "-m", "delivery"], repo)
    mine = "mine1\nmine2\n"
    theirs = "theirs1\n"
    prod._write(repo, f"state/warp/{prod.RUN}/manifest.json", json.dumps({
        "rows": [{"id": "C1", "writes": [DELIVERY]}], "write_baseline": {DELIVERY: _blob(repo, "head\n")},
    }))
    prod._write(repo, f"state/warp/{SIBLING}/manifest.json", json.dumps({
        "rows": [{"id": "C1", "writes": [DELIVERY]}],
        "write_baseline": {DELIVERY: _blob(repo, "head\n" + mine)},
    }))
    prod._author(repo, DELIVERY, "head\n" + mine + theirs)
    prod._declare(repo, DELIVERY)
    prod._record_dir(repo)

    import subprocess

    from coordinator_core.ops.dispatch_emit import co_declared_settle

    spawns = {"n": 0}
    real_run, real_popen = subprocess.run, subprocess.Popen
    real_settle, real_blob = co_declared_settle.settle, co_declared_settle.settled_blob

    def _counted(fn):
        def inner(*a, **k):
            def run(*ra, **rk):
                spawns["n"] += 1
                return real_run(*ra, **rk)

            def popen(*pa, **pk):
                spawns["n"] += 1
                return real_popen(*pa, **pk)

            monkeypatch.setattr(subprocess, "run", run)
            monkeypatch.setattr(subprocess, "Popen", popen)
            try:
                return fn(*a, **k)
            finally:
                monkeypatch.setattr(subprocess, "run", real_run)
                monkeypatch.setattr(subprocess, "Popen", real_popen)
        return inner

    monkeypatch.setattr(co_declared_settle, "settle", _counted(real_settle))
    monkeypatch.setattr(co_declared_settle, "settled_blob", _counted(real_blob))

    out = _land(repo)

    assert spawns["n"] == 0, "settle and the own-hunk blob are in-process: no git spawn"
    assert out.get("committed") is True, out
    assert prod._git(["show", "HEAD:" + DELIVERY], repo) == "head\n" + mine
    assert (repo / DELIVERY).read_text(encoding="utf-8") == "head\n" + mine + theirs


def test_a_non_artifact_path_the_sibling_holds_with_no_same_parent_record_lands_with_provenance(repo, monkeypatch):
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda root, run, paths, *a, **k: [("docs/other.md", SIBLING)],
    )

    prod._author(repo, "docs/other.md", "x\n")
    prod._declare(repo, "docs/other.md")
    prod._record_dir(repo)
    out = _land(repo)

    assert out["committed"] is True, out
    assert [e["path"] for e in _prov(out, peer_hunk_gate.PEER_HUNKS_CO_DECLARED)] == ["docs/other.md"], out
