"""The ea06ca shape through `review.reverify` revert mode: six reds, four red at the run base and two
caused by a sibling's dirty file. Handed the base export and the failure text, the op classes the four
`pre_existing` and the two `foreign_red`, and `from_reverify` finds no blocker. Without either input the
same six stay blockers, so the classification rides on what the caller passes.

Trap: `land_run.foreign_halted_withheld` and the HEAD blob lookup are stubbed, as in the sibling-owned tests.
Trap: the first four tests hand-write the base export; the last two drive `reverify_base_measure.main` on a real
git fixture repo, including ea06ca's shape (ids absent at the base commit, so the base run exits 4)."""

from __future__ import annotations

import json
import subprocess

import pytest

_REAL_POPEN = subprocess.Popen  # the `world` fixture swaps Popen for a refusal; measuring needs the real one

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import SIBLING_FILE, _dirty_sibling

AT_BASE = [f"bench/tests/test_slow.py::test_red_{i}" for i in range(4)]
SIBLING_DIRT = [f"bench/tests/test_census.py::test_dirt[{i}]" for i in range(2)]
TRACE = f"bench/tests/test_census.py:9: in test_dirt\n    {SIBLING_FILE}:4: SyntaxError\n"
FAILING = [*AT_BASE, *SIBLING_DIRT]
EXPORT = "scratch/reverify-base-export-run.txt"


def _write_export(root, red, *, runner_exit=1, export="ok"):
    path = root / EXPORT
    path.parent.mkdir(parents=True, exist_ok=True)
    tail = {"baseline_export": {"export": export, "runner_exit": runner_exit, "passed_at_base": SIBLING_DIRT}}
    path.write_text("".join(f"FAILED {n} - AssertionError\n" for n in red) + json.dumps(tail) + "\n", encoding="utf-8")


def _revert(root, **kw):
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=FAILING, run_id="me", **kw
    )


def _output():
    return {n: TRACE for n in SIBLING_DIRT}


def test_base_red_is_pre_existing_and_sibling_dirt_is_foreign_red_and_nothing_blocks(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    _write_export(root, AT_BASE)
    out = _revert(root, base_export_path=EXPORT, failing_output=_output())
    assert out["error"] is None
    assert out["pre_existing"] == sorted(AT_BASE)
    assert sorted(out["foreign_red"]) == sorted(SIBLING_DIRT)
    assert out["unattributed"] == [] and out["unmeasured"] == [] and out["reverted"] == []
    assert from_reverify({**out, "failing": FAILING}) == []


def test_without_the_base_export_or_failure_text_the_same_reds_block(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _revert(root)
    assert out["pre_existing"] == [] and out["foreign_red"] == {}
    assert sorted(b.node_id for b in from_reverify({**out, "failing": FAILING})) == sorted(FAILING)


def test_a_base_red_id_stays_pre_existing_when_the_sibling_dirt_names_no_path(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    _write_export(root, AT_BASE)
    out = _revert(root, base_export_path=EXPORT, failing_output={n: "AssertionError: 41 <= 37" for n in SIBLING_DIRT})
    assert out["pre_existing"] == sorted(AT_BASE)
    assert out["foreign_red"] == {} and sorted(out["unattributed"]) == sorted(SIBLING_DIRT)


def test_an_unusable_base_export_leaves_every_red_unmeasured_not_pre_existing(world, monkeypatch):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    _write_export(root, AT_BASE, runner_exit=2)
    out = _revert(root, base_export_path=EXPORT, failing_output=_output())
    assert out["base_unmeasured"] is True and out["pre_existing"] == []
    assert sorted(out["unmeasured"]) == sorted(AT_BASE)


def test_measure_output_with_head_pass_feeds_review_reverify_pre_existing(world, monkeypatch):
    from coordinator_core.ops import reverify_base_measure as rbm

    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    calls = []

    def fake_run(sha, ids, cwd):
        calls.append((sha, list(ids)))
        if sha == "base":
            return "ok", 1, [f"FAILED {n} - AssertionError" for n in AT_BASE], []
        return "ok", 0, [], []

    monkeypatch.setattr(rbm, "_run", fake_run)
    monkeypatch.chdir(root)
    assert rbm.main(["--sha", "base", "--head-sha", "head", "--out", EXPORT, "--", *FAILING]) == 0
    assert calls[1] == ("head", SIBLING_DIRT)
    out = _revert(root, base_export_path=EXPORT, failing_output=_output())
    assert out["pre_existing"] == sorted(AT_BASE)
    assert sorted(out["foreign_red"]) == sorted(SIBLING_DIRT)


@pytest.mark.parametrize("prefixes", [(), ("g1", "g2")])
def test_reverify_sidecar_schema_and_prompt_carry_the_classed_red_sets(prefixes):
    from coordinator_core.ops.review_mint.execute_review import _REVERIFY_SCHEMA, _reverify_phase

    props = _REVERIFY_SCHEMA["properties"]
    for name in ("sibling_owned", "foreign", "renamed_node"):
        assert props[name] == {"type": "array", "items": {"type": "string"}} and name not in _REVERIFY_SCHEMA["required"]
    assert props["foreign_red"]["type"] == "object"
    block = _reverify_phase(
        base_ref="b" * 40, prep_slice_id="prep1", repo_root=None, plan_path="docs/plans/example.md",
        run_base_sha="b" * 40, prompt_head="head", host_degraded=False,
        sc=lambda call, *_: call, group_prefixes=prefixes,
    )[1]
    assert "`foreign_red`" in block and "`sibling_owned`" in block
    assert '"failing_output":{<node id>:<its failing_detail message>}' in block


_SLOW = "".join(f"def test_red_{i}():\n    assert False\n\n\n" for i in range(4))
_CENSUS = "import pytest\n\n\n@pytest.mark.parametrize('i', [0, 1])\ndef test_dirt(i):\n    assert True\n"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(repo, files, message):
    for rel, body in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _measure_export(tmp_path, monkeypatch, *, ids_at_base):
    """Run `reverify_base_measure.main` in a real fixture repo and return the export file's text.
    With `ids_at_base` False the six ids first appear in the head commit, so the base run exits 4."""
    from coordinator_core.ops import reverify_base_measure as rbm

    refusal = subprocess.Popen
    repo = tmp_path.parent / (tmp_path.name + "-fixture")
    repo.mkdir()
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    try:
        _git(repo, "init", "-q")
        tests = {"bench/tests/test_slow.py": _SLOW, "bench/tests/test_census.py": _CENSUS}
        base = _commit(repo, tests if ids_at_base else {"README.md": "x\n"}, "base")
        head = base if ids_at_base else _commit(repo, tests, "add the six")
        monkeypatch.chdir(repo)
        assert rbm.main(["--sha", base, "--head-sha", head, "--out", "measured.txt", "--", *FAILING]) == 0
    finally:
        monkeypatch.setattr(subprocess, "Popen", refusal)
    return (repo / "measured.txt").read_text(encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.parametrize("ids_at_base", [True, False], ids=["ids-at-base", "ids-added-after-base"])
def test_measure_on_a_real_git_fixture_feeds_review_reverify_pre_existing_and_foreign(
    world, monkeypatch, tmp_path, ids_at_base
):
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    text = _measure_export(tmp_path, monkeypatch, ids_at_base=ids_at_base)
    assert sorted(ln[len("FAILED "):].split(" - ")[0] for ln in text.splitlines() if ln.startswith("FAILED ")) == sorted(AT_BASE)
    (root / EXPORT).parent.mkdir(parents=True, exist_ok=True)
    (root / EXPORT).write_text(text, encoding="utf-8")
    out = _revert(root, base_export_path=EXPORT, failing_output=_output())
    assert out["error"] is None and out["base_unmeasured"] is False
    assert out["pre_existing"] == sorted(AT_BASE)
    assert sorted(out["foreign_red"]) == sorted(SIBLING_DIRT)
    assert out["unattributed"] == [] and out["unmeasured"] == []
    assert from_reverify({**out, "failing": FAILING}) == []
