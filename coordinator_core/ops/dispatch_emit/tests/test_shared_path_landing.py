"""shared_path_landing: sibling-held product join, withheld paths, refusal text, contract keys."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import land_run_contract, peer_claim_contract, shared_path_landing as spl

UNLANDED = [
    ("C2", "a/x.py", "peer-held"),
    ("C1", "a/x.py", "peer-held"),
    ("C1", "b/y.py", "never-commit"),
    ("C3", "c/z.py", "peer-held"),
]


def test_sibling_held_product_keeps_only_peer_held_sorted_with_holders():
    held = spl.sibling_held_product(UNLANDED, {"a/x.py": ["s2", "s1"], "c/z.py": ["s3"]})
    assert held == [("C1", "a/x.py", ["s1", "s2"]), ("C2", "a/x.py", ["s1", "s2"]), ("C3", "c/z.py", ["s3"])]


def test_sibling_held_product_unknown_path_has_empty_holders_and_malformed_triple_raises():
    assert spl.sibling_held_product([("C1", "q", "peer-held")], {}) == [("C1", "q", [])]
    assert spl.sibling_held_product(None, None) == []
    with pytest.raises(ValueError):
        spl.sibling_held_product([("bad",)], {})


def test_withheld_product_paths_all_reasons_unique_sorted():
    assert spl.withheld_product_paths(UNLANDED) == ["a/x.py", "b/y.py", "c/z.py"]
    assert spl.withheld_product_paths([]) == []
    assert spl.withheld_product_paths(None) == []


def test_refusal_message_names_path_row_holder_and_both_routes(tmp_path):
    msg = spl.refusal_message([("C1", "a/x.py", ["s1"])], "ask-1", root=tmp_path)
    assert msg.startswith(spl.SHARED_PATH_SIBLING_HELD)
    assert "a/x.py (row C1) held by s1" in msg
    assert "Wait for the holder to land, then re-fire." in msg
    assert peer_claim_contract.WITHHOLD_CALL.format(run_id="ask-1") in msg


def test_refusal_message_without_root_or_holders():
    msg = spl.refusal_message([("C1", "a/x.py", [])], "ask-1")
    assert "held by a live sibling" in msg
    assert "re-fire." in msg


def test_reply_keys_gain_the_two_members():
    assert spl.LEDGER_WITHHELD_PRODUCT in land_run_contract.REPLY_KEYS
    assert "exit_criterion_not_stamped" in land_run_contract.REPLY_KEYS


# --- run-keyed refusal and the disjoint-hunk split -------------------------------------------------

import subprocess  # noqa: E402
import time  # noqa: E402

from coordinator_core.git import git_dir, git_objects  # noqa: E402
from coordinator_core.ops.dispatch_emit import halted_hunk_attribution as hha  # noqa: E402
from coordinator_core.ops.dispatch_emit import run_hunk_claims as rhc  # noqa: E402
from coordinator_core.ops.dispatch_emit import run_token  # noqa: E402
from coordinator_core.ops.dispatch_emit.own_hunk_stage import PathDiff  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_peer_hunk_gate import SELF, _declare, repo  # noqa: E402,F401

HPATH = "src/target.py"
SENIOR = "ask-20261024T000000-bbbbbb"
ME = "ask-20261025T000000-aaaaaa"
JUNIOR = "ask-20261026T000000-cccccc"


def _mint(root, run_id, *, claim=False):
    token = run_token.mint(root, run_id, f"{run_id}.mjs", session_id=SELF)
    if claim:
        assert run_token.redeem(root, run_id, token, f"{run_id}.mjs")


def _mint_pair(root, *run_ids, own):
    for run_id in run_ids:
        _declare(root, run_id, HPATH)
        _mint(root, run_id, claim=run_id == own)
        time.sleep(0.01)


def test_refusal_names_the_senior_run_not_the_shared_session(repo):
    _mint_pair(repo, SENIOR, ME, own=ME)
    msg = spl.refusal_message([("C1", HPATH, [SELF])], ME, root=repo)
    assert f"held by {SENIOR} (minted " in msg
    assert f"held by {SELF}" not in msg


def test_a_junior_is_never_named_as_holder_of_a_senior(repo):
    _mint_pair(repo, SENIOR, JUNIOR, own=SENIOR)
    msg = spl.refusal_message([("C1", HPATH, [SELF])], SENIOR, root=repo)
    assert JUNIOR not in msg
    assert f"held by {SELF}" in msg


def test_refusal_lists_senior_holders_oldest_first_and_keeps_a_touch_session(repo):
    older = "ask-20261023T000000-dddddd"
    _mint_pair(repo, older, SENIOR, ME, own=ME)
    msg = spl.refusal_message([("C1", HPATH, [SELF, "touch-sid"])], ME, root=repo)
    assert msg.index(older) < msg.index(SENIOR)
    assert "touch-sid" in msg


def _text(edits):
    return "".join(f"{edits.get(i, f'line{i}')}\n" for i in range(1, 11))


@pytest.fixture
def world(tmp_path, monkeypatch):
    blobs: dict[str, bytes] = {}
    baselines: dict[str, dict] = {}
    claims: dict = {"value": {HPATH: [(1.0, SENIOR, "sid-senior")]}}

    def no_spawn(*a, **k):
        raise AssertionError("hunk_landable must not spawn")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    monkeypatch.setattr(git_dir, "resolve_git_common_dir", lambda root: tmp_path / ".git")
    monkeypatch.setattr(git_objects, "read_object", lambda cd, sha: ("blob", blobs[sha]) if sha in blobs else None)
    monkeypatch.setattr(hha, "_baseline", lambda root, run: baselines.get(run))
    monkeypatch.setattr(spl, "_senior_claims", lambda root, run_id, paths: claims["value"])

    class World:
        root = tmp_path
        claim = claims

        @staticmethod
        def tree(edits):
            (tmp_path / "src").mkdir(exist_ok=True)
            (tmp_path / HPATH).write_bytes(_text(edits).encode())

        @staticmethod
        def baseline(run, edits):
            sha = f"{len(blobs):040x}"
            blobs[sha] = _text(edits).encode()
            baselines[run] = {HPATH: sha}

        @staticmethod
        def tree_bytes(data):
            (tmp_path / "src").mkdir(exist_ok=True)
            (tmp_path / HPATH).write_bytes(data)

        @staticmethod
        def baseline_bytes(run, data):
            sha = f"{len(blobs):040x}"
            blobs[sha] = data
            baselines[run] = {HPATH: sha}

    return World


def _h(line, old_len=1, new_len=1):
    return rhc.Hunk(HPATH, line, old_len, line, new_len)


def _diff(*hunks):
    return {HPATH: PathDiff("0" * 40, b"", tuple(hunks))}


def test_disjoint_hunks_stage_only_the_own_hunk(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {2: "A"})
    world.baseline(SENIOR, {9: "B"})
    stage, still = spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(_h(2), _h(9)))
    assert stage == {HPATH: [_h(9)]}
    assert still == []


def test_overlapping_hunks_stay_held(world):
    world.tree({3: "m3", 4: "s4"})
    world.baseline(ME, {4: "s4"})
    world.baseline(SENIOR, {3: "m3"})
    mine = rhc.Hunk(HPATH, 3, 2, 3, 1)
    stage, still = spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(mine, _h(4)))
    assert stage == {}
    assert still == [HPATH]


def test_an_unattributed_hunk_holds_the_path(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {})
    world.baseline(SENIOR, {})
    stage, still = spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(_h(2), _h(9)))
    assert stage == {} and still == [HPATH]


def test_a_holder_no_senior_run_accounts_for_holds_and_unknown_claims_hold(world):
    world.tree({2: "A", 9: "B"})
    world.baseline(ME, {2: "A"})
    world.baseline(SENIOR, {9: "B"})
    held = {HPATH: ["sid-senior", "touch-sid"]}
    assert spl.hunk_landable(world.root, ME, held, _diff(_h(2), _h(9))) == ({}, [HPATH])
    world.claim["value"] = None
    assert spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(_h(2), _h(9))) == ({}, [HPATH])
    world.claim["value"] = {HPATH: [(1.0, SENIOR, "sid-senior")]}
    assert spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, {}) == ({}, [HPATH])


def _numbered(n, edits=()):
    return "".join(f"{'EDIT' if i in edits else 'line'}{i}\n" for i in range(1, n + 1)).encode()


def test_an_over_bound_senior_and_the_lander_editing_one_interior_hunk_stay_held(world):
    n = rhc.MAX_MATCHED_MIDDLE + 50
    mid = n // 2
    world.tree_bytes(_numbered(n, {1, mid, n}))
    world.baseline_bytes(SENIOR, _numbered(n, {mid}))  # senior delta: first and last line, one coarse span
    world.baseline_bytes(ME, _numbered(n, {1, n}))  # my delta: the interior line
    stage, still = spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(_h(mid)))
    assert stage == {} and still == [HPATH]


def _worst_bytes(middle):
    """Identical repeated lines (the quadratic case), edits at both ends, `middle` lines trimmed per side."""
    old = [b"head\n", *([b"    x\n"] * (middle - 2)), b"tail\n"]
    new = list(old)
    new[0], new[-1] = b"HEAD\n", b"TAIL\n"
    new[middle // 3:middle // 3 + 3] = [b"ins\n"] * 3
    return b"".join(old), b"".join(new)


def test_the_worst_case_through_hunk_landable_is_under_the_bar_in_process_time(world):
    old, new = _worst_bytes(rhc.MAX_MATCHED_MIDDLE)  # the largest middle still matched, for both claimants
    world.tree_bytes(new)
    world.baseline_bytes(ME, old)
    world.baseline_bytes(SENIOR, old)
    start = time.process_time()
    result = spl.hunk_landable(world.root, ME, {HPATH: ["sid-senior"]}, _diff(_h(1), _h(rhc.MAX_MATCHED_MIDDLE // 3 + 1)))
    spent = time.process_time() - start
    assert result == ({}, [HPATH])  # equal baselines: unattributed, held
    assert spent < 0.02, spent  # measured ~5ms for two claimants; DR-344 bar is 500ms
